"""Committed-projection queries and restart-safe asynchronous API commands."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sleepagent.sleep_api.auth import (
    AuthenticatedActorContext,
    AuthoritativeRoleBindingResolver,
    SleepApiAuthenticator,
    SleepApiSecurityError,
    VerifiedActorIdentity,
)
from sleepagent.sleep_api.contracts import (
    AcceptedOperationResponse,
    CurrentRiskSourceScope,
    CurrentRiskResponse,
    EventPollResponse,
    LifecycleResponse,
    NightEpisodePageResponse,
    NightEpisodeResponse,
    NightEpisodeRevisionSnapshot,
    NightEpisodeSummary,
    OperationStatusResponse,
    PageMetadata,
    PublicActorRole,
    PublicErrorCode,
    PublicMonitoringState,
    PublicOperationStatus,
    PublicServiceState,
    RevocationTombstone,
    RoleViewResponse,
)
from sleepagent.sleep_api.events import EventProjectionContext, project_domain_event
from sleepagent.sleep_api.persistence import (
    EventCursorSession,
    FeedbackRecord,
    OperationCommand,
    SleepApiPersistence,
)
from sleepagent.sleep_domain import (
    AnalysisRole,
    DataMode,
    DeviceBinding,
    DeviceBindingStatus,
    DomainNamespace,
    IdempotencyConflictError,
    LifecycleTrigger,
    LifecycleTriggerKind,
    LifecycleTriggerSource,
    MonitoringState,
    NightEpisode,
    NightEpisodeRevision,
    NightEpisodeService,
    Operation,
    OperationStatus,
    SleepDomainRepository,
)
from sleepagent.sleep_domain.lifecycle import (
    InvalidLifecycleTransitionError,
    LifecycleAuthorizationError,
    LifecycleBusyError,
)


UTC = timezone.utc
OPERATION_PREFIX = "sleep_api."


COMMAND_SCOPE = {
    "sleep_api.monitoring.activate.v1": "sleep:monitoring:write",
    "sleep_api.monitoring.deactivate.v1": "sleep:monitoring:write",
    "sleep_api.feedback.elder.v1": "sleep:feedback:self",
    "sleep_api.feedback.family.v1": "sleep:feedback:family",
    "sleep_api.reanalysis.v1": "sleep:reanalysis:write",
}


class SleepApiApplicationError(RuntimeError):
    def __init__(
        self,
        code: PublicErrorCode,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
        details: Mapping[str, str] | None = None,
        tombstone: RevocationTombstone | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.details = dict(details or {})
        self.tombstone = tombstone


class OpaquePageCursorCodec:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("page cursor secret must contain at least 32 bytes")
        self._key = hashlib.sha256(secret).digest()

    def encode(self, payload: Mapping[str, Any]) -> str:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(self._key).encrypt(
            nonce,
            raw,
            b"sleep-api-opaque-cursor.v1",
        )
        return _b64url_encode(nonce + encrypted)

    def decode(self, cursor: str) -> dict[str, Any]:
        try:
            encrypted = _b64url_decode(cursor)
            if len(encrypted) < 29:
                raise ValueError("cursor ciphertext is too short")
            raw = AESGCM(self._key).decrypt(
                encrypted[:12],
                encrypted[12:],
                b"sleep-api-opaque-cursor.v1",
            )
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("cursor payload is not an object")
            return payload
        except (InvalidTag, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise SleepApiApplicationError(
                PublicErrorCode.INVALID_REQUEST,
                "The pagination cursor is invalid.",
                status_code=400,
            ) from exc


class AuthorizationEpochRoleViewCache:
    def __init__(self, *, maximum_entries: int = 4096) -> None:
        self._maximum_entries = maximum_entries
        self._lock = threading.RLock()
        self._entries: dict[tuple[Any, ...], RoleViewResponse] = {}

    @staticmethod
    def key(
        *,
        subject_id: str,
        night_episode_revision_id: str,
        role: PublicActorRole,
        scopes: frozenset[str],
        authorization_epoch: int,
    ) -> tuple[Any, ...]:
        return (
            subject_id,
            night_episode_revision_id,
            role.value,
            tuple(sorted(scopes)),
            authorization_epoch,
        )

    def get(self, key: tuple[Any, ...]) -> RoleViewResponse | None:
        with self._lock:
            return self._entries.get(key)

    def put(self, key: tuple[Any, ...], value: RoleViewResponse) -> None:
        with self._lock:
            subject_id, _, role, _, epoch = key
            stale = [
                existing
                for existing in self._entries
                if existing[0] == subject_id
                and existing[2] == role
                and existing[4] != epoch
            ]
            for existing in stale:
                self._entries.pop(existing, None)
            if len(self._entries) >= self._maximum_entries:
                self._entries.pop(next(iter(self._entries)))
            self._entries[key] = value


@dataclass(frozen=True)
class SleepApiRuntime:
    namespace: DomainNamespace
    repository: SleepDomainRepository
    api_persistence: SleepApiPersistence
    episode_service: NightEpisodeService
    authority: AuthoritativeRoleBindingResolver
    cursor_codec: OpaquePageCursorCodec
    role_view_cache: AuthorizationEpochRoleViewCache
    authenticator: SleepApiAuthenticator
    now_factory: Any = lambda: datetime.now(tz=UTC)
    event_cursor_ttl: timedelta = timedelta(hours=24)
    event_schema_generation: str = "sleep-domain-events-v1"

    def require_subject(
        self,
        context: AuthenticatedActorContext,
        subject_id: str,
    ) -> None:
        if context.claims.subject_id != subject_id:
            raise SleepApiApplicationError(
                PublicErrorCode.AUTHORIZATION_DENIED,
                "The requested subject is outside the actor assertion.",
                status_code=403,
            )

    def poll_events(
        self,
        identity: VerifiedActorIdentity,
        *,
        subject_id: str,
        cursor: str | None,
        limit: int,
    ) -> EventPollResponse:
        if identity.claims.subject_id != subject_id:
            raise SleepApiApplicationError(
                PublicErrorCode.AUTHORIZATION_DENIED,
                "The requested subject is outside the actor assertion.",
                status_code=403,
            )
        now = self.now_factory()
        cursor_id: str | None = None
        delivery_offset = 0
        session: EventCursorSession | None = None
        if cursor is not None:
            payload = self._decode_event_cursor(cursor)
            cursor_id = str(payload["cursor_id"])
            delivery_offset = int(payload["delivery_offset"])
            session = self.api_persistence.get_event_cursor_session(
                self.namespace,
                cursor_id=cursor_id,
            )
            if session is None:
                self._cursor_resync("cursor_unknown")
            self._require_cursor_identity(session, identity, subject_id)
            if session.revoked_at is not None:
                record = self.api_persistence.get_event_cursor_tombstone(
                    self.namespace,
                    cursor_id=session.cursor_id,
                )
                self._cursor_resync(
                    "cursor_revoked",
                    tombstone=(
                        None
                        if record is None
                        else RevocationTombstone(
                            event_id=record.event_id,
                            aggregate_id=record.cursor_id,
                            subject_id=record.subject_id,
                            effective_at=record.effective_at,
                        )
                    ),
                )

        try:
            context = self.authenticator.authorize_identity(
                identity,
                required_scopes=frozenset({"sleep:events:read"}),
            )
        except SleepApiSecurityError as exc:
            if exc.code != PublicErrorCode.AUTHORIZATION_DENIED:
                raise
            if cursor_id is None or session is None:
                raise
            tombstone = self._revoke_cursor(
                session,
                now=now,
                reason_code="authorization_revoked",
            )
            self._cursor_resync(
                "authorization_revoked",
                tombstone=tombstone,
            )
        if session is not None and (
            session.expires_at < now
            or session.event_schema_generation != self.event_schema_generation
            or payload["event_schema_generation"]
            != self.event_schema_generation
        ):
            self._cursor_resync("cursor_expired_or_schema_changed")
        effective_scopes = frozenset(
            set(context.claims.scope).intersection(context.binding.scopes)
        )
        scope_hash = _scope_hash(effective_scopes)
        if session is None:
            cursor_id = uuid4().hex
            session = EventCursorSession(
                cursor_id=cursor_id,
                namespace=self.namespace,
                consumer_service_id=context.service_principal.principal_id,
                actor_id=context.claims.actor_id,
                subject_id=subject_id,
                actor_role=context.claims.role,
                scope_projection_sha256=scope_hash,
                authorization_epoch=context.binding.authorization_epoch,
                event_schema_generation=self.event_schema_generation,
                issued_at=now,
                expires_at=now + self.event_cursor_ttl,
                last_used_at=now,
            )
            self.api_persistence.create_event_cursor_session(session)
        elif (
            session.scope_projection_sha256 != scope_hash
            or session.authorization_epoch != context.binding.authorization_epoch
            or session.actor_role != context.claims.role
        ):
            tombstone = self._revoke_cursor(
                session,
                now=now,
                reason_code="authorization_projection_changed",
            )
            self._cursor_resync(
                "authorization_projection_changed",
                tombstone=tombstone,
            )

        projected = []
        scanned = 0
        scan_offset = delivery_offset
        projection_context = EventProjectionContext(
            role=context.claims.role,
            scopes=effective_scopes,
        )
        while len(projected) < limit and scanned < 1000:
            raw_events = self.api_persistence.list_domain_events_after(
                self.namespace,
                subject_id=subject_id,
                delivery_offset=scan_offset,
                limit=min(100, 1000 - scanned),
            )
            if not raw_events:
                break
            for event in raw_events:
                scan_offset = event.delivery_offset
                scanned += 1
                public_event = project_domain_event(event, projection_context)
                if public_event is not None:
                    projected.append(public_event)
                    if len(projected) >= limit:
                        break
            if len(raw_events) < min(100, 1000 - (scanned - len(raw_events))):
                break
        self.api_persistence.touch_event_cursor_session(
            self.namespace,
            cursor_id=session.cursor_id,
            used_at=now,
        )
        return EventPollResponse(
            event_schema_generation=self.event_schema_generation,
            events=tuple(projected),
            next_cursor=self.cursor_codec.encode(
                {
                    "kind": "sleep_event_cursor.v1",
                    "cursor_id": session.cursor_id,
                    "delivery_offset": scan_offset,
                    "event_schema_generation": self.event_schema_generation,
                }
            ),
            cursor_expires_at=session.expires_at,
            has_more=self.api_persistence.has_domain_events_after(
                self.namespace,
                subject_id=subject_id,
                delivery_offset=scan_offset,
            ),
        )

    def _decode_event_cursor(self, cursor: str) -> dict[str, Any]:
        try:
            payload = self.cursor_codec.decode(cursor)
            if (
                payload.get("kind") != "sleep_event_cursor.v1"
                or not isinstance(payload.get("cursor_id"), str)
                or not isinstance(payload.get("delivery_offset"), int)
                or payload["delivery_offset"] < 0
                or not isinstance(payload.get("event_schema_generation"), str)
            ):
                raise ValueError("event cursor payload mismatch")
            return payload
        except (SleepApiApplicationError, ValueError, KeyError, TypeError) as exc:
            raise SleepApiApplicationError(
                PublicErrorCode.CURSOR_RESYNC_REQUIRED,
                "The event cursor is invalid; reload snapshots and obtain a new cursor.",
                status_code=409,
                details={"reason": "cursor_invalid"},
            ) from exc

    def _require_cursor_identity(
        self,
        session: EventCursorSession,
        identity: VerifiedActorIdentity,
        subject_id: str,
    ) -> None:
        if (
            session.consumer_service_id
            != identity.service_principal.principal_id
            or session.actor_id != identity.claims.actor_id
            or session.subject_id != subject_id
            or session.actor_role != identity.claims.role
        ):
            self._cursor_resync("cursor_binding_mismatch")

    def _revoke_cursor(
        self,
        session: EventCursorSession,
        *,
        now: datetime,
        reason_code: str,
    ) -> RevocationTombstone:
        event_id = hashlib.sha256(
            f"cursor-tombstone:{session.cursor_id}".encode("utf-8")
        ).hexdigest()
        record = self.api_persistence.revoke_event_cursor(
            self.namespace,
            cursor_id=session.cursor_id,
            event_id=event_id,
            subject_id=session.subject_id,
            revoked_at=now,
            reason_code=reason_code,
        )
        return RevocationTombstone(
            event_id=record.event_id,
            aggregate_id=record.cursor_id,
            subject_id=record.subject_id,
            effective_at=record.effective_at,
        )

    @staticmethod
    def _cursor_resync(
        reason: str,
        *,
        tombstone: RevocationTombstone | None = None,
    ) -> None:
        raise SleepApiApplicationError(
            PublicErrorCode.CURSOR_RESYNC_REQUIRED,
            "Reload current snapshots and obtain a new event cursor.",
            status_code=409,
            details={"reason": reason},
            tombstone=tombstone,
        )

    def get_lifecycle(
        self,
        context: AuthenticatedActorContext,
        *,
        subject_id: str,
    ) -> LifecycleResponse:
        self.require_subject(context, subject_id)
        snapshot = self.repository.get_monitoring_snapshot(
            self.namespace,
            subject_id=subject_id,
        )
        if snapshot is None:
            raise _data_insufficient("No committed lifecycle projection is available.")
        followups = self.repository.list_care_followups(
            self.namespace,
            subject_id=subject_id,
        )
        open_followups = tuple(
            item
            for item in followups
            if item.state.value not in {"completed", "ended"}
        )
        service_state = (
            PublicServiceState.ACTIVE
            if snapshot.state == MonitoringState.ACTIVE
            else (
                PublicServiceState.FOLLOW_UP
                if open_followups
                else PublicServiceState.DORMANT
            )
        )
        episode_id = snapshot.active_night_episode_id
        quality = (
            None
            if episode_id is None
            else self.repository.get_current_quality(
                self.namespace,
                night_episode_id=episode_id,
            )
        )
        return LifecycleResponse(
            subject_id=subject_id,
            service_state=service_state,
            monitoring_state=PublicMonitoringState(snapshot.state.value),
            active_night_episode_id=episode_id,
            data_quality_state=None if quality is None else quality.quality_state.value,
            data_sufficiency=(
                None if quality is None else quality.data_sufficiency.value
            ),
            committed_at=snapshot.updated_at,
        )

    def get_current_risk(
        self,
        context: AuthenticatedActorContext,
        *,
        subject_id: str,
    ) -> CurrentRiskResponse:
        self.require_subject(context, subject_id)
        episode = self._latest_episode(subject_id)
        risk = self.repository.get_current_risk(
            self.namespace,
            night_episode_id=episode.night_episode_id,
        )
        if risk is None:
            raise _data_insufficient("No committed current-risk projection is available.")
        return CurrentRiskResponse(
            subject_id=subject_id,
            night_episode_id=risk.night_episode_id,
            risk_state=risk.risk_state.value,
            data_sufficiency=risk.data_sufficiency.value,
            source_scope=CurrentRiskSourceScope(
                night_episode_revision_id=(
                    risk.source_scope.night_episode_revision_id
                ),
                observation_types=tuple(
                    item.value for item in risk.source_scope.observation_types
                ),
                observation_count=len(risk.source_scope.observation_ids),
                device_count=len(risk.source_scope.device_binding_ids),
                window_start_at=risk.source_scope.window_start_at,
                window_end_at=risk.source_scope.window_end_at,
            ),
            policy_version=risk.policy_version,
            observed_at=risk.observed_at,
            reason_codes=risk.reason_codes,
            health_escalation_allowed=risk.health_escalation_allowed,
            committed_at=risk.updated_at,
        )

    def list_night_episodes(
        self,
        context: AuthenticatedActorContext,
        *,
        subject_id: str,
        limit: int,
        cursor: str | None,
    ) -> NightEpisodePageResponse:
        self.require_subject(context, subject_id)
        episodes = sorted(
            self.repository.list_night_episodes(
                self.namespace,
                subject_id=subject_id,
            ),
            key=lambda item: (item.created_at, item.night_episode_id),
            reverse=True,
        )
        start = 0
        if cursor:
            payload = self.cursor_codec.decode(cursor)
            expected = {
                "v": 1,
                "subject_id": subject_id,
                "service_principal_id": (
                    context.service_principal.principal_id
                ),
                "actor_id": context.claims.actor_id,
                "role": context.claims.role.value,
                "scope_hash": _scope_hash(frozenset(context.claims.scope)),
                "authorization_epoch": context.binding.authorization_epoch,
            }
            if any(payload.get(key) != value for key, value in expected.items()):
                raise SleepApiApplicationError(
                    PublicErrorCode.AUTHORIZATION_DENIED,
                    "The pagination cursor is outside the current authorization.",
                    status_code=403,
                )
            marker = (str(payload.get("created_at")), str(payload.get("episode_id")))
            for index, episode in enumerate(episodes):
                candidate = (
                    episode.created_at.astimezone(UTC).isoformat(),
                    episode.night_episode_id,
                )
                if candidate == marker:
                    start = index + 1
                    break
            else:
                raise SleepApiApplicationError(
                    PublicErrorCode.INVALID_REQUEST,
                    "The pagination cursor no longer identifies a committed page.",
                    status_code=400,
                )
        selected = episodes[start : start + limit]
        next_cursor: str | None = None
        if start + limit < len(episodes) and selected:
            last = selected[-1]
            next_cursor = self.cursor_codec.encode(
                {
                    "v": 1,
                    "subject_id": subject_id,
                    "service_principal_id": (
                        context.service_principal.principal_id
                    ),
                    "actor_id": context.claims.actor_id,
                    "role": context.claims.role.value,
                    "scope_hash": _scope_hash(frozenset(context.claims.scope)),
                    "authorization_epoch": context.binding.authorization_epoch,
                    "created_at": last.created_at.astimezone(UTC).isoformat(),
                    "episode_id": last.night_episode_id,
                }
            )
        return NightEpisodePageResponse(
            items=tuple(self._summary(item) for item in selected),
            page=PageMetadata(limit=limit, next_cursor=next_cursor),
        )

    def get_night_episode(
        self,
        context: AuthenticatedActorContext,
        *,
        subject_id: str,
        night_episode_id: str,
        revision_id: str | None,
    ) -> NightEpisodeResponse:
        self.require_subject(context, subject_id)
        episode = self._episode(subject_id, night_episode_id)
        selected_revision_id = revision_id or episode.current_night_episode_revision_id
        revision: NightEpisodeRevision | None = None
        if selected_revision_id is not None:
            revision = self.repository.get_night_episode_revision(
                self.namespace,
                night_episode_revision_id=selected_revision_id,
            )
            if (
                revision is None
                or revision.night_episode_id != episode.night_episode_id
                or revision.subject_id != subject_id
            ):
                raise _not_found("NightEpisode revision not found.")
        return NightEpisodeResponse(
            summary=self._summary(episode),
            revision=None if revision is None else self._revision(revision),
        )

    def get_role_view(
        self,
        context: AuthenticatedActorContext,
        *,
        subject_id: str,
        night_episode_id: str,
        revision_id: str | None,
    ) -> RoleViewResponse:
        self.require_subject(context, subject_id)
        episode = self._episode(subject_id, night_episode_id)
        selected_revision_id = revision_id or episode.current_night_episode_revision_id
        if selected_revision_id is None:
            raise _data_insufficient("No committed NightEpisode revision is available.")
        revision = self.repository.get_night_episode_revision(
            self.namespace,
            night_episode_revision_id=selected_revision_id,
        )
        if revision is None or revision.night_episode_id != night_episode_id:
            raise _not_found("NightEpisode revision not found.")
        key = self.role_view_cache.key(
            subject_id=subject_id,
            night_episode_revision_id=selected_revision_id,
            role=context.claims.role,
            scopes=(
                frozenset(context.claims.scope) & context.binding.scopes
            ),
            authorization_epoch=context.binding.authorization_epoch,
        )
        cached = self.role_view_cache.get(key)
        if cached is not None:
            return cached
        analyses = self.repository.list_analysis_revisions(
            self.namespace,
            night_episode_revision_id=selected_revision_id,
        )
        if not analyses:
            raise SleepApiApplicationError(
                PublicErrorCode.RESULT_PENDING,
                "The authorized view is not committed yet.",
                status_code=409,
                retryable=True,
            )
        analysis = analyses[-1]
        projected_role = (
            AnalysisRole.FAMILY
            if context.claims.role
            in {PublicActorRole.FAMILY, PublicActorRole.CAREGIVER}
            else AnalysisRole(context.claims.role.value)
        )
        view = self.repository.get_analysis_role_view(
            self.namespace,
            analysis_revision_id=analysis.analysis_revision_id,
            role=projected_role,
        )
        if view is None:
            raise SleepApiApplicationError(
                PublicErrorCode.RESULT_PENDING,
                "The authorized view is not committed yet.",
                status_code=409,
                retryable=True,
            )
        response = RoleViewResponse(
            subject_id=subject_id,
            night_episode_id=night_episode_id,
            night_episode_revision_id=selected_revision_id,
            role=context.claims.role,
            status=view.status.value,
            execution_mode=view.execution_mode,
            content=view.content,
            context_notice=view.context_notice,
            failure_codes=_public_view_failure_codes(view.status.value),
            generated_at=view.generated_at,
            authorization_epoch=context.binding.authorization_epoch,
        )
        self.role_view_cache.put(key, response)
        return response

    def get_operation(
        self,
        context: AuthenticatedActorContext,
        *,
        operation_id: str,
    ) -> OperationStatusResponse:
        operation = self.repository.get_operation(
            self.namespace,
            operation_id=operation_id,
        )
        if operation is None:
            raise _not_found("Operation not found.")
        if (
            operation.service_principal_id
            != context.service_principal.principal_id
            or operation.actor_id != context.claims.actor_id
            or operation.subject_id != context.claims.subject_id
        ):
            raise _not_found("Operation not found.")
        return _operation_response(operation)

    def submit_command(
        self,
        context: AuthenticatedActorContext,
        *,
        operation_type: str,
        route_template: str,
        target_resource_id: str,
        idempotency_key: str,
        request_payload: Mapping[str, Any],
    ) -> AcceptedOperationResponse:
        if operation_type not in COMMAND_SCOPE:
            raise ValueError("unknown public command")
        now = self.now_factory()
        operation = Operation(
            operation_id=f"op-{uuid4()}",
            data_mode=self.namespace.data_mode,
            operation_type=operation_type,
            subject_id=context.claims.subject_id,
            service_principal_id=context.service_principal.principal_id,
            actor_id=context.claims.actor_id,
            target_resource_id=target_resource_id,
            idempotency_key=idempotency_key,
            request_sha256=_canonical_request_hash(request_payload),
            status=OperationStatus.PENDING,
            correlation_id=context.correlation_id,
            created_at=now,
            updated_at=now,
        )
        command = OperationCommand(
            operation_id=operation.operation_id,
            namespace=self.namespace,
            route_template=route_template,
            actor_role=context.claims.role,
            authorization_id=context.binding.authorization_id,
            authorization_epoch=context.binding.authorization_epoch,
            request=dict(request_payload),
            created_at=now,
        )
        try:
            stored, _ = self.api_persistence.create_command_operation(
                operation,
                command,
            )
        except IdempotencyConflictError as exc:
            raise SleepApiApplicationError(
                PublicErrorCode.IDEMPOTENCY_CONFLICT,
                "The Idempotency-Key was already used with a different request.",
                status_code=409,
            ) from exc
        return AcceptedOperationResponse(
            operation_id=stored.operation_id,
            status=stored.status.value,
            status_url=f"/api/v1/operations/{stored.operation_id}",
            correlation_id=stored.correlation_id,
        )

    def _latest_episode(self, subject_id: str) -> NightEpisode:
        episodes = self.repository.list_night_episodes(
            self.namespace,
            subject_id=subject_id,
        )
        if not episodes:
            raise _data_insufficient("No committed NightEpisode is available.")
        return max(episodes, key=lambda item: (item.created_at, item.night_episode_id))

    def _episode(self, subject_id: str, night_episode_id: str) -> NightEpisode:
        episode = self.repository.get_night_episode(
            self.namespace,
            night_episode_id=night_episode_id,
        )
        if episode is None or episode.subject_id != subject_id:
            raise _not_found("NightEpisode not found.")
        return episode

    def _summary(self, episode: NightEpisode) -> NightEpisodeSummary:
        pointer = self.repository.get_current_night_revision(
            self.namespace,
            night_episode_id=episode.night_episode_id,
        )
        return NightEpisodeSummary(
            night_episode_id=episode.night_episode_id,
            subject_id=episode.subject_id,
            local_sleep_date=episode.local_sleep_date,
            timezone_name=episode.timezone_name,
            collection_start_at=episode.collection_start_at,
            collection_end_at=episode.collection_end_at,
            lifecycle_state=episode.state.value,
            data_sufficiency=episode.data_sufficiency.value,
            quality_flags=episode.quality_flags,
            current_revision_number=pointer.current_revision_number,
            current_revision_id=pointer.current_revision_id,
            committed_at=episode.updated_at,
        )

    @staticmethod
    def _revision(
        revision: NightEpisodeRevision,
    ) -> NightEpisodeRevisionSnapshot:
        return NightEpisodeRevisionSnapshot(
            night_episode_id=revision.night_episode_id,
            night_episode_revision_id=revision.night_episode_revision_id,
            subject_id=revision.subject_id,
            revision_number=revision.revision_number,
            parent_revision_id=revision.parent_revision_id,
            revision_cause=revision.revision_cause.value,
            data_sufficiency=revision.data_sufficiency.value,
            quality_flags=revision.quality_flags,
            created_at=revision.created_at,
        )


class SleepApiOperationWorker:
    def __init__(
        self,
        runtime: SleepApiRuntime,
        *,
        worker_id: str = "sleep-api-worker",
        lease_duration: timedelta = timedelta(seconds=30),
        poll_interval_seconds: float = 0.2,
    ) -> None:
        self.runtime = runtime
        self.worker_id = worker_id
        self.lease_duration = lease_duration
        self.poll_interval_seconds = poll_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=self.worker_id,
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self.poll_interval_seconds)

    def run_once(self) -> bool:
        now = self.runtime.now_factory()
        leased = self.runtime.repository.lease_next_operation(
            self.runtime.namespace,
            operation_type_prefix=OPERATION_PREFIX,
            worker_id=self.worker_id,
            now=now,
            lease_duration=self.lease_duration,
        )
        if leased is None:
            return False
        operation = leased.operation
        result_resource_id: str | None = None
        error_code: str | None = None
        try:
            command = self.runtime.api_persistence.get_command(
                self.runtime.namespace,
                operation_id=operation.operation_id,
            )
            if command is None:
                raise SleepApiApplicationError(
                    PublicErrorCode.INTERNAL_ERROR,
                    "The persisted command payload is unavailable.",
                    status_code=500,
                )
            self._reauthorize(operation, command)
            result_resource_id = self._execute(operation, command)
            terminal_status = OperationStatus.SUCCEEDED
        except SleepApiApplicationError as exc:
            terminal_status = OperationStatus.FAILED
            error_code = exc.code.value
        except (InvalidLifecycleTransitionError, LifecycleAuthorizationError):
            terminal_status = OperationStatus.FAILED
            error_code = PublicErrorCode.INVALID_LIFECYCLE_TRANSITION.value
        except LifecycleBusyError:
            terminal_status = OperationStatus.FAILED
            error_code = PublicErrorCode.OPERATION_CONFLICT.value
        except KeyError:
            terminal_status = OperationStatus.FAILED
            error_code = PublicErrorCode.RESOURCE_NOT_FOUND.value
        except Exception:
            terminal_status = OperationStatus.FAILED
            error_code = PublicErrorCode.INTERNAL_ERROR.value
        completed_at = self.runtime.now_factory()
        terminal = operation.model_copy(
            update={
                "status": terminal_status,
                "lease_owner": None,
                "lease_expires_at": None,
                "result_resource_id": result_resource_id,
                "error_code": error_code,
                "updated_at": completed_at,
            }
        )
        changed = self.runtime.repository.compare_and_set_operation(
            self.runtime.namespace,
            terminal,
            expected_cas_version=leased.cas_version,
        )
        if not changed:
            raise SleepApiApplicationError(
                PublicErrorCode.OPERATION_LEASE_LOST,
                "The Operation lease changed before completion.",
                status_code=409,
            )
        return True

    def _reauthorize(
        self,
        operation: Operation,
        command: OperationCommand,
    ) -> None:
        now = self.runtime.now_factory()
        binding = self.runtime.authority.resolve(
            actor_id=operation.actor_id,
            subject_id=operation.subject_id,
            role=command.actor_role,
            now=now,
        )
        required_scope = COMMAND_SCOPE[operation.operation_type]
        if (
            binding is None
            or binding.authorization_id != command.authorization_id
            or binding.authorization_epoch != command.authorization_epoch
            or required_scope not in binding.scopes
        ):
            raise SleepApiApplicationError(
                PublicErrorCode.AUTHORIZATION_DENIED,
                "Command authorization was revoked before execution.",
                status_code=403,
            )

    def _execute(
        self,
        operation: Operation,
        command: OperationCommand,
    ) -> str:
        now = self.runtime.now_factory()
        payload = command.request
        if operation.operation_type == "sleep_api.monitoring.activate.v1":
            binding = self._binding_for_activation(operation, payload, now)
            trigger = self._trigger(
                operation,
                command,
                kind=LifecycleTriggerKind.ACTIVATE,
                source=LifecycleTriggerSource.AUTHORIZED_COMMAND,
                occurred_at=_request_time(payload, "occurred_at", now),
                received_at=now,
            )
            result = self.runtime.episode_service.process_trigger(
                self.runtime.namespace,
                trigger=trigger,
                device_binding_id=binding.device_binding_id,
            )
            return (
                result.episode.night_episode_id
                if result.episode is not None
                else operation.subject_id
            )
        if operation.operation_type == "sleep_api.monitoring.deactivate.v1":
            trigger = self._trigger(
                operation,
                command,
                kind=LifecycleTriggerKind.DEACTIVATE,
                source=LifecycleTriggerSource.AUTHORIZED_COMMAND,
                occurred_at=_request_time(payload, "occurred_at", now),
                received_at=now,
            )
            result = self.runtime.episode_service.process_trigger(
                self.runtime.namespace,
                trigger=trigger,
            )
            return (
                result.episode.night_episode_id
                if result.episode is not None
                else operation.subject_id
            )
        if operation.operation_type == "sleep_api.reanalysis.v1":
            episode = self.runtime.repository.get_night_episode(
                self.runtime.namespace,
                night_episode_id=operation.target_resource_id or "",
            )
            if episode is None or episode.subject_id != operation.subject_id:
                raise SleepApiApplicationError(
                    PublicErrorCode.RESOURCE_NOT_FOUND,
                    "NightEpisode not found.",
                    status_code=404,
                )
            trigger = self._trigger(
                operation,
                command,
                kind=LifecycleTriggerKind.REANALYZE,
                source=LifecycleTriggerSource.EXPLICIT_REANALYSIS,
                occurred_at=now,
                received_at=now,
            )
            result = self.runtime.episode_service.request_reanalysis(
                self.runtime.namespace,
                night_episode_id=operation.target_resource_id or "",
                trigger=trigger,
            )
            if result.revision is None:
                raise SleepApiApplicationError(
                    PublicErrorCode.INTERNAL_ERROR,
                    "The reanalysis command did not commit a revision.",
                    status_code=500,
                )
            return result.revision.night_episode_revision_id
        if operation.operation_type in {
            "sleep_api.feedback.elder.v1",
            "sleep_api.feedback.family.v1",
        }:
            return self._execute_feedback(operation, command, now)
        raise SleepApiApplicationError(
            PublicErrorCode.INVALID_REQUEST,
            "The persisted command type is unsupported.",
            status_code=400,
        )

    def _execute_feedback(
        self,
        operation: Operation,
        command: OperationCommand,
        now: datetime,
    ) -> str:
        payload = command.request
        role = command.actor_role
        provenance = {
            PublicActorRole.ELDER: "elder_self_report",
            PublicActorRole.FAMILY: "family_report",
            PublicActorRole.CAREGIVER: "caregiver_report",
        }.get(role)
        if provenance is None:
            raise SleepApiApplicationError(
                PublicErrorCode.AUTHORIZATION_DENIED,
                "This role cannot submit feedback.",
                status_code=403,
            )
        episode = self.runtime.repository.get_night_episode(
            self.runtime.namespace,
            night_episode_id=operation.target_resource_id or "",
        )
        if episode is None or episode.subject_id != operation.subject_id:
            raise SleepApiApplicationError(
                PublicErrorCode.RESOURCE_NOT_FOUND,
                "NightEpisode not found.",
                status_code=404,
            )
        event_at = _request_time(payload, "event_at", now)
        if event_at > now:
            raise SleepApiApplicationError(
                PublicErrorCode.INVALID_REQUEST,
                "Feedback event time cannot be in the future.",
                status_code=400,
            )
        feedback = FeedbackRecord(
            feedback_id=f"feedback-{operation.operation_id}",
            operation_id=operation.operation_id,
            namespace=self.runtime.namespace,
            subject_id=operation.subject_id,
            night_episode_id=operation.target_resource_id or "",
            actor_id=operation.actor_id,
            actor_role=role,
            authorization_id=command.authorization_id,
            authorization_epoch=command.authorization_epoch,
            provenance_category=provenance,
            event_at=event_at,
            received_at=now,
            source_text=(
                None
                if payload.get("source_text") is None
                else str(payload["source_text"])
            ),
            structured_answer=payload.get("structured_answer"),
        )
        self.runtime.api_persistence.save_feedback(feedback)
        trigger = self._trigger(
            operation,
            command,
            kind=LifecycleTriggerKind.FEEDBACK,
            source=LifecycleTriggerSource.AUTHORIZED_COMMAND,
            occurred_at=feedback.event_at,
            received_at=now,
        )
        result = self.runtime.episode_service.submit_feedback_revision(
            self.runtime.namespace,
            night_episode_id=feedback.night_episode_id,
            trigger=trigger,
        )
        if result.revision is None:
            raise SleepApiApplicationError(
                PublicErrorCode.INTERNAL_ERROR,
                "The feedback command did not commit a revision.",
                status_code=500,
            )
        self.runtime.api_persistence.link_feedback_revision(
            self.runtime.namespace,
            operation_id=operation.operation_id,
            night_episode_revision_id=result.revision.night_episode_revision_id,
        )
        return result.revision.night_episode_revision_id

    def _binding_for_activation(
        self,
        operation: Operation,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> DeviceBinding:
        binding_id = payload.get("device_binding_id")
        if binding_id:
            candidates = (
                self.runtime.repository.get_device_binding(
                    self.runtime.namespace,
                    device_binding_id=str(binding_id),
                ),
            )
        else:
            candidates = self.runtime.repository.list_subject_device_bindings(
                self.runtime.namespace,
                subject_id=operation.subject_id,
            )
        active = [
            binding
            for binding in candidates
            if binding is not None
            and binding.subject_id == operation.subject_id
            and binding.status == DeviceBindingStatus.ACTIVE
            and binding.effective_from <= now
            and (binding.effective_until is None or now < binding.effective_until)
        ]
        if len(active) != 1:
            raise SleepApiApplicationError(
                PublicErrorCode.BINDING_REQUIRED,
                "Exactly one active device binding is required.",
                status_code=409,
            )
        return active[0]

    @staticmethod
    def _trigger(
        operation: Operation,
        command: OperationCommand,
        *,
        kind: LifecycleTriggerKind,
        source: LifecycleTriggerSource,
        occurred_at: datetime,
        received_at: datetime,
    ) -> LifecycleTrigger:
        if occurred_at > received_at:
            raise SleepApiApplicationError(
                PublicErrorCode.INVALID_REQUEST,
                "Command event time cannot be in the future.",
                status_code=400,
            )
        return LifecycleTrigger(
            trigger_id=f"api-command:{operation.operation_id}",
            data_mode=operation.data_mode,
            subject_id=operation.subject_id,
            source=source,
            kind=kind,
            occurred_at=occurred_at,
            received_at=received_at,
            actor_id=operation.actor_id,
            authorization_id=command.authorization_id,
            correlation_id=operation.correlation_id,
        )


def _request_time(
    payload: Mapping[str, Any],
    field_name: str,
    default: datetime,
) -> datetime:
    value = payload.get(field_name)
    if value is None:
        return default
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SleepApiApplicationError(
            PublicErrorCode.INVALID_REQUEST,
            f"{field_name} must include a timezone offset.",
            status_code=400,
        )
    return parsed.astimezone(UTC)


def _operation_response(operation: Operation) -> OperationStatusResponse:
    return OperationStatusResponse(
        operation_id=operation.operation_id,
        operation_type=operation.operation_type.removeprefix(OPERATION_PREFIX),
        subject_id=operation.subject_id,
        status=PublicOperationStatus(operation.status.value),
        attempt=operation.attempt_count,
        result_resource_id=operation.result_resource_id,
        error_code=operation.error_code,
        correlation_id=operation.correlation_id,
        created_at=operation.created_at,
        updated_at=operation.updated_at,
    )


def _scope_hash(scopes: frozenset[str]) -> str:
    return hashlib.sha256("\n".join(sorted(scopes)).encode("utf-8")).hexdigest()


def _canonical_request_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _public_view_failure_codes(status: str) -> tuple[str, ...]:
    return {
        "ready": (),
        "pending": ("RESULT_PENDING",),
        "degraded": ("RESULT_DEGRADED",),
        "blocked": ("RESULT_BLOCKED",),
    }.get(status, ("RESULT_UNAVAILABLE",))


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if (
        not value
        or "=" in value
        or any(character.isspace() for character in value)
    ):
        raise ValueError("invalid base64url value")
    decoded = base64.b64decode(
        value + "=" * ((-len(value)) % 4),
        altchars=b"-_",
        validate=True,
    )
    if _b64url_encode(decoded) != value:
        raise ValueError("non-canonical base64url value")
    return decoded


def _not_found(message: str) -> SleepApiApplicationError:
    return SleepApiApplicationError(
        PublicErrorCode.RESOURCE_NOT_FOUND,
        message,
        status_code=404,
    )


def _data_insufficient(message: str) -> SleepApiApplicationError:
    return SleepApiApplicationError(
        PublicErrorCode.DATA_INSUFFICIENT,
        message,
        status_code=409,
    )


__all__ = [
    "AuthorizationEpochRoleViewCache",
    "OpaquePageCursorCodec",
    "SleepApiApplicationError",
    "SleepApiOperationWorker",
    "SleepApiRuntime",
]
