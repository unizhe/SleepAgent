"""PostgreSQL committed-view adapter for the stable ``/api/v1`` router.

The legacy :class:`sleepagent.sleep_api.service.SleepApiRuntime` remains useful
for isolated domain tests.  Production composition uses this adapter instead:
every request resolves current PostgreSQL authority and every query is scoped
to committed, publishable rows in one explicit Unit of Work.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from sleepagent.backend_persistence import (
    PostgresAuthorityStore,
    ResolvedActorAuthority,
    build_product_authenticator,
)
from sleepagent.backend_settings import SleepBackendSettings
from sleepagent.product_api.contracts import ProductRole
from sleepagent.product_api.service import ProductApiError
from sleepagent.persistence.uow import UnitOfWorkFactory, UowScope
from sleepagent.sleep_api.auth import (
    AuthenticatedActorContext,
    AuthoritativeRoleBinding,
    SleepApiAuthenticator,
    SleepApiSecurityError,
    VerifiedActorIdentity,
)
from sleepagent.sleep_api.contracts import (
    AcceptedOperationResponse,
    CurrentRiskResponse,
    CurrentRiskSourceScope,
    EventPollResponse,
    ExternalDomainEvent,
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
    RoleViewResponse,
)
from sleepagent.sleep_api.service import (
    OpaquePageCursorCodec,
    SleepApiApplicationError,
)
from sleepagent.sleep_domain.contracts import CurrentRisk


UTC = timezone.utc
PUBLIC_PURPOSE = "read_sleep"
EVENT_SCHEMA_GENERATION = "sleep-domain-events-v2"


@dataclass(frozen=True)
class PostgresAuthenticatedActorContext(AuthenticatedActorContext):
    authority: ResolvedActorAuthority


class PostgresPublicAuthenticator:
    """Request-bound JWS authentication plus current database authority."""

    def __init__(
        self,
        identity_authenticator: SleepApiAuthenticator,
        authority: PostgresAuthorityStore,
        *,
        purpose: str = PUBLIC_PURPOSE,
    ) -> None:
        self.identity_authenticator = identity_authenticator
        self.authority = authority
        self.purpose = purpose

    def verify_identity(self, *args: Any, **kwargs: Any) -> VerifiedActorIdentity:
        return self.identity_authenticator.verify_identity(*args, **kwargs)

    def authenticate(
        self,
        *args: Any,
        required_scopes: frozenset[str],
        **kwargs: Any,
    ) -> PostgresAuthenticatedActorContext:
        identity = self.identity_authenticator.verify_identity(
            *args,
            required_scopes=required_scopes,
            **kwargs,
        )
        return self.authorize_identity(identity, required_scopes=required_scopes)

    def authorize_identity(
        self,
        identity: VerifiedActorIdentity,
        *,
        required_scopes: frozenset[str],
    ) -> PostgresAuthenticatedActorContext:
        if (
            identity.service_principal.principal_id
            != self.authority.settings.service_principal_id
        ):
            raise SleepApiSecurityError(
                PublicErrorCode.AUTHORIZATION_DENIED,
                "The service credential is outside this deployment grant.",
                status_code=403,
            )
        authority_role = (
            ProductRole.FAMILY
            if identity.claims.role == PublicActorRole.CAREGIVER
            else ProductRole(identity.claims.role.value)
        )
        try:
            resolved = self.authority.resolve(
                actor_id=identity.claims.actor_id,
                subject_id=identity.claims.subject_id,
                role=authority_role,
                purpose=self.purpose,
            )
        except ProductApiError as exc:
            raise SleepApiSecurityError(
                PublicErrorCode.AUTHORIZATION_DENIED,
                "No unique active authority binding grants this request.",
                status_code=403,
            ) from exc
        effective = frozenset(identity.claims.scope).intersection(
            resolved.effective_scopes
        )
        if not required_scopes.issubset(effective):
            raise SleepApiSecurityError(
                PublicErrorCode.AUTHORIZATION_DENIED,
                "The authoritative binding does not grant the required scope.",
                status_code=403,
            )
        binding = AuthoritativeRoleBinding(
            authorization_id=resolved.binding_id,
            actor_id=identity.claims.actor_id,
            subject_id=identity.claims.subject_id,
            role=identity.claims.role,
            scopes=effective,
            authorization_epoch=resolved.authorization_epoch,
        )
        return PostgresAuthenticatedActorContext(
            service_principal=identity.service_principal,
            claims=identity.claims,
            binding=binding,
            correlation_id=identity.correlation_id,
            authority=resolved,
        )


class PostgresSleepApiRuntime:
    """Duck-typed runtime consumed by the existing stable v1 router."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory[Any],
        authenticator: PostgresPublicAuthenticator,
        cursor_key: bytes,
        purpose: str = PUBLIC_PURPOSE,
        now_factory: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self.uow_factory = uow_factory
        self.authenticator = authenticator
        self.cursor_codec = OpaquePageCursorCodec(cursor_key)
        self.purpose = purpose
        self.now_factory = now_factory

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

    def get_lifecycle(
        self,
        context: AuthenticatedActorContext,
        *,
        subject_id: str,
    ) -> LifecycleResponse:
        typed = self._context(context, subject_id)
        with self.uow_factory.begin(self._scope(typed)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT snapshot.state, snapshot.active_night_episode_id,
                           snapshot.updated_at, quality.assessment_json
                    FROM public.backend_monitoring_snapshots_v2 AS snapshot
                    LEFT JOIN public.sleep_domain_night_episodes AS episode
                      ON episode.namespace_id = snapshot.namespace_id
                     AND episode.data_mode = snapshot.data_mode
                     AND episode.night_episode_id = snapshot.active_night_episode_id
                     AND episode.subject_id = snapshot.subject_id
                    LEFT JOIN public.sleep_domain_current_quality AS quality
                      ON quality.namespace_id = snapshot.namespace_id
                     AND quality.data_mode = snapshot.data_mode
                     AND quality.night_episode_id = snapshot.active_night_episode_id
                     AND quality.subject_id = snapshot.subject_id
                     AND quality.assessment_json #>>
                           '{source_scope,night_episode_revision_id}' =
                         episode.current_revision_id
                    WHERE snapshot.namespace_id = %s
                      AND snapshot.data_mode = %s
                      AND snapshot.namespace_generation = %s
                      AND snapshot.subject_id = %s
                      AND COALESCE(snapshot.run_id, '') = COALESCE(%s, '')
                      AND COALESCE(snapshot.arm_id, '') = COALESCE(%s, '')
                    """,
                    self._authority_params(typed),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if row is None:
            raise _data_insufficient(
                "No committed lifecycle projection is available."
            )
        quality = None if row[3] is None else _json_value(row[3])
        state = str(row[0])
        return LifecycleResponse(
            subject_id=subject_id,
            service_state=(
                PublicServiceState.ACTIVE
                if state == "active"
                else PublicServiceState.DORMANT
            ),
            monitoring_state=PublicMonitoringState(state),
            active_night_episode_id=None if row[1] is None else str(row[1]),
            data_quality_state=(
                None if quality is None else str(quality.get("quality_state"))
            ),
            data_sufficiency=(
                None if quality is None else str(quality.get("data_sufficiency"))
            ),
            committed_at=row[2],
        )

    def get_current_risk(
        self,
        context: AuthenticatedActorContext,
        *,
        subject_id: str,
    ) -> CurrentRiskResponse:
        typed = self._context(context, subject_id)
        with self.uow_factory.begin(self._scope(typed)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT risk.risk_json
                    FROM public.sleep_domain_night_episodes AS episode
                    JOIN public.sleep_domain_current_risk AS risk
                      ON risk.namespace_id = episode.namespace_id
                     AND risk.data_mode = episode.data_mode
                     AND risk.night_episode_id = episode.night_episode_id
                     AND risk.subject_id = episode.subject_id
                    WHERE episode.namespace_id = %s
                      AND episode.data_mode = %s
                      AND episode.namespace_generation = %s
                      AND episode.subject_id = %s
                      AND COALESCE(episode.run_id, '') = COALESCE(%s, '')
                      AND COALESCE(episode.arm_id, '') = COALESCE(%s, '')
                      AND episode.protocol_version >= 2
                      AND episode.date_state = 'finalized'
                      AND episode.date_conflict = FALSE
                      AND risk.risk_json #>>
                            '{source_scope,night_episode_revision_id}' =
                          episode.current_revision_id
                    ORDER BY episode.episode_local_date DESC,
                             episode.updated_at DESC,
                             episode.night_episode_id DESC
                    LIMIT 1
                    """,
                    self._authority_params(typed),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if row is None:
            raise _data_insufficient(
                "No committed current-risk projection is available."
            )
        risk = CurrentRisk.model_validate(_json_value(row[0]))
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
        typed = self._context(context, subject_id)
        marker_date: str | None = None
        marker_updated: str | None = None
        marker_id: str | None = None
        if cursor is not None:
            payload = self.cursor_codec.decode(cursor)
            if payload.get("authority") != self._cursor_authority(typed):
                raise SleepApiApplicationError(
                    PublicErrorCode.CURSOR_RESYNC_REQUIRED,
                    "Authority changed; restart NightEpisode pagination.",
                    status_code=409,
                )
            try:
                marker_date = str(payload["episode_local_date"])
                marker_updated = str(payload["updated_at"])
                marker_id = str(payload["night_episode_id"])
            except KeyError as exc:
                raise SleepApiApplicationError(
                    PublicErrorCode.INVALID_REQUEST,
                    "The pagination cursor is invalid.",
                    status_code=400,
                ) from exc
        with self.uow_factory.begin(self._scope(typed)) as uow:
            db_cursor = uow.connection.cursor()
            try:
                db_cursor.execute(
                    _EPISODE_SELECT
                    + """
                    WHERE episode.namespace_id = %s
                      AND episode.data_mode = %s
                      AND episode.namespace_generation = %s
                      AND episode.subject_id = %s
                      AND COALESCE(episode.run_id, '') = COALESCE(%s, '')
                      AND COALESCE(episode.arm_id, '') = COALESCE(%s, '')
                      AND episode.protocol_version >= 2
                      AND episode.date_state = 'finalized'
                      AND episode.date_conflict = FALSE
                      AND (%s::date IS NULL OR
                        (episode.episode_local_date, episode.updated_at,
                         episode.night_episode_id) <
                        (%s::date, %s::timestamptz, %s))
                    ORDER BY episode.episode_local_date DESC,
                             episode.updated_at DESC,
                             episode.night_episode_id DESC
                    LIMIT %s
                    """,
                    (
                        *self._authority_params(typed),
                        marker_date,
                        marker_date,
                        marker_updated,
                        marker_id,
                        limit + 1,
                    ),
                )
                rows = tuple(db_cursor.fetchall())
            finally:
                db_cursor.close()
            uow.commit()
        visible = rows[:limit]
        summaries = tuple(_episode_summary(row) for row in visible)
        next_cursor = None
        if len(rows) > limit and visible:
            last = visible[-1]
            next_cursor = self.cursor_codec.encode(
                {
                    "authority": self._cursor_authority(typed),
                    "episode_local_date": last[2].isoformat(),
                    "updated_at": last[10].isoformat(),
                    "night_episode_id": str(last[0]),
                }
            )
        return NightEpisodePageResponse(
            items=summaries,
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
        typed = self._context(context, subject_id)
        with self.uow_factory.begin(self._scope(typed)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    _EPISODE_SELECT
                    + """
                    WHERE episode.night_episode_id = %s
                      AND episode.namespace_id = %s
                      AND episode.data_mode = %s
                      AND episode.namespace_generation = %s
                      AND episode.subject_id = %s
                      AND COALESCE(episode.run_id, '') = COALESCE(%s, '')
                      AND COALESCE(episode.arm_id, '') = COALESCE(%s, '')
                      AND episode.protocol_version >= 2
                      AND episode.date_state = 'finalized'
                      AND episode.date_conflict = FALSE
                    """,
                    (night_episode_id, *self._authority_params(typed)),
                )
                episode_row = cursor.fetchone()
                revision_row = None
                if episode_row is not None:
                    selected_revision = revision_id or str(episode_row[9])
                    cursor.execute(
                        """
                        SELECT revision.night_episode_revision_id,
                               revision.night_episode_id, revision.subject_id,
                               revision.revision_number,
                               revision.parent_revision_id,
                               revision.revision_json, revision.created_at,
                               revision.episode_local_date,
                               revision.assignment_basis,
                               revision.bed_local_date,
                               revision.wake_local_date,
                               revision.date_confidence,
                               revision.assignment_estimated
                        FROM public.sleep_domain_night_episode_revisions AS revision
                        WHERE revision.night_episode_revision_id = %s
                          AND revision.night_episode_id = %s
                          AND revision.namespace_id = %s
                          AND revision.data_mode = %s
                          AND revision.namespace_generation = %s
                          AND revision.subject_id = %s
                          AND revision.protocol_version >= 2
                          AND revision.date_state = 'finalized'
                          AND revision.date_conflict = FALSE
                        """,
                        (
                            selected_revision,
                            night_episode_id,
                            typed.authority.namespace_id,
                            typed.authority.data_mode,
                            typed.authority.namespace_generation,
                            subject_id,
                        ),
                    )
                    revision_row = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if episode_row is None:
            raise _not_found("NightEpisode not found.")
        if revision_id is not None and revision_row is None:
            raise _not_found("NightEpisode revision not found.")
        return NightEpisodeResponse(
            summary=_episode_summary(episode_row),
            revision=(
                None if revision_row is None else _revision_snapshot(revision_row)
            ),
        )

    def get_role_view(
        self,
        context: AuthenticatedActorContext,
        *,
        subject_id: str,
        night_episode_id: str,
        revision_id: str | None,
    ) -> RoleViewResponse:
        typed = self._context(context, subject_id)
        stored_role = (
            "family"
            if typed.claims.role == PublicActorRole.CAREGIVER
            else typed.claims.role.value
        )
        with self.uow_factory.begin(self._scope(typed)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT view.night_episode_revision_id, view.status,
                           view.view_json, view.generated_at,
                           view.authorization_epoch
                    FROM public.sleep_domain_analysis_role_views AS view
                    JOIN public.sleep_domain_night_episodes AS episode
                      ON episode.night_episode_id = view.night_episode_id
                     AND episode.namespace_id = view.namespace_id
                     AND episode.data_mode = view.data_mode
                    WHERE view.namespace_id = %s AND view.data_mode = %s
                      AND view.namespace_generation = %s
                      AND view.subject_id = %s
                      AND COALESCE(view.run_id, '') = COALESCE(%s, '')
                      AND COALESCE(view.arm_id, '') = COALESCE(%s, '')
                      AND view.night_episode_id = %s AND view.role = %s
                      AND (
                        (%s IS NULL AND view.night_episode_revision_id =
                          episode.current_revision_id)
                        OR (%s IS NOT NULL AND
                          view.night_episode_revision_id = %s)
                      )
                      AND view.protocol_version >= 2
                      AND view.authorization_epoch = %s
                      AND view.privacy_epoch = %s
                      AND view.retrieval_policy_epoch = %s
                      AND episode.date_state = 'finalized'
                      AND episode.date_conflict = FALSE
                    ORDER BY view.generated_at DESC, view.role_view_id DESC
                    LIMIT 1
                    """,
                    (
                        *self._authority_params(typed),
                        night_episode_id,
                        stored_role,
                        revision_id,
                        revision_id,
                        revision_id,
                        typed.authority.authorization_epoch,
                        typed.authority.privacy_epoch,
                        typed.authority.retrieval_policy_epoch,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if row is None:
            raise SleepApiApplicationError(
                PublicErrorCode.RESULT_PENDING,
                "The authorized view is not committed yet.",
                status_code=409,
                retryable=True,
            )
        payload = _json_value(row[2])
        content = payload.get("content")
        if content is not None and not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, sort_keys=True)
        return RoleViewResponse(
            subject_id=subject_id,
            night_episode_id=night_episode_id,
            night_episode_revision_id=str(row[0]),
            role=typed.claims.role,
            status=str(row[1]),
            execution_mode=str(payload.get("execution_mode", "intelligent")),
            content=content,
            context_notice=(
                None
                if payload.get("context_notice") is None
                else str(payload["context_notice"])
            ),
            failure_codes=tuple(str(item) for item in payload.get("failure_codes", ())),
            generated_at=row[3],
            authorization_epoch=int(row[4]),
        )

    def get_operation(
        self,
        context: AuthenticatedActorContext,
        *,
        operation_id: str,
    ) -> OperationStatusResponse:
        typed = self._context(context, context.claims.subject_id)
        with self.uow_factory.begin(self._scope(typed)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT operation_id, operation_type, subject_id, status,
                           attempt_count, operation_json, created_at, updated_at
                    FROM public.sleep_domain_operations
                    WHERE operation_id = %s AND namespace_id = %s
                      AND data_mode = %s AND namespace_generation = %s
                      AND subject_id = %s
                      AND COALESCE(run_id, '') = COALESCE(%s, '')
                      AND COALESCE(arm_id, '') = COALESCE(%s, '')
                      AND origin_kind = 'user' AND service_principal_id = %s
                      AND actor_id = %s AND protocol_version >= 2
                    """,
                    (
                        operation_id,
                        *self._authority_params(typed),
                        typed.service_principal.principal_id,
                        typed.claims.actor_id,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if row is None:
            raise _not_found("Operation not found.")
        payload = _json_value(row[5])
        result = payload.get("result")
        result = result if isinstance(result, Mapping) else {}
        return OperationStatusResponse(
            operation_id=str(row[0]),
            operation_type=str(row[1]),
            subject_id=str(row[2]),
            status=_operation_status(str(row[3])),
            attempt=int(row[4]),
            result_resource_id=(
                None
                if result.get("result_resource_id") is None
                else str(result["result_resource_id"])
            ),
            error_code=(
                None
                if result.get("error_code") is None
                else str(result["error_code"])
            ),
            correlation_id=str(payload.get("correlation_id", row[0])),
            created_at=row[6],
            updated_at=row[7],
        )

    def submit_command(self, *args: Any, **kwargs: Any) -> AcceptedOperationResponse:
        del args, kwargs
        raise SleepApiApplicationError(
            PublicErrorCode.PROVIDER_UNAVAILABLE,
            "Public command persistence is not installed in the B3 read adapter.",
            status_code=503,
            retryable=True,
        )

    def poll_events(
        self,
        identity: VerifiedActorIdentity,
        *,
        subject_id: str,
        cursor: str | None,
        limit: int,
    ) -> EventPollResponse:
        context = self.authenticator.authorize_identity(
            identity,
            required_scopes=frozenset({"sleep:events:read"}),
        )
        typed = self._context(context, subject_id)
        after_offset = 0
        now = self.now_factory()
        if cursor is not None:
            payload = self.cursor_codec.decode(cursor)
            if payload.get("authority") != self._cursor_authority(typed):
                raise SleepApiApplicationError(
                    PublicErrorCode.CURSOR_RESYNC_REQUIRED,
                    "Authority changed; reload committed snapshots.",
                    status_code=409,
                )
            try:
                if payload.get("kind") != "sleep_event_cursor.pg.v1":
                    raise ValueError("event cursor kind mismatch")
                if (
                    payload.get("event_schema_generation")
                    != EVENT_SCHEMA_GENERATION
                ):
                    raise ValueError("event cursor schema generation mismatch")
                after_offset = int(payload["delivery_offset"])
                expires_at = datetime.fromisoformat(str(payload["expires_at"]))
                if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                    raise ValueError("event cursor expiry is timezone-naive")
            except (KeyError, TypeError, ValueError) as exc:
                raise SleepApiApplicationError(
                    PublicErrorCode.CURSOR_RESYNC_REQUIRED,
                    "The event cursor is invalid; reload committed snapshots.",
                    status_code=409,
                ) from exc
            if expires_at <= now:
                raise SleepApiApplicationError(
                    PublicErrorCode.CURSOR_RESYNC_REQUIRED,
                    "The event cursor expired; reload committed snapshots.",
                    status_code=409,
                )
        with self.uow_factory.begin(self._scope(typed)) as uow:
            db_cursor = uow.connection.cursor()
            try:
                db_cursor.execute(
                    """
                    SELECT delivery_offset, event_id, event_type,
                           aggregate_id, aggregate_version,
                           per_aggregate_sequence, subject_id, operation_id,
                           event_json, created_at
                    FROM public.sleep_domain_domain_outbox
                    WHERE namespace_id = %s AND data_mode = %s
                      AND namespace_generation = %s AND subject_id = %s
                      AND COALESCE(run_id, '') = COALESCE(%s, '')
                      AND COALESCE(arm_id, '') = COALESCE(%s, '')
                      AND protocol_version >= 2 AND status = 'committed'
                      AND delivery_offset > %s
                    ORDER BY delivery_offset
                    LIMIT %s
                    """,
                    (*self._authority_params(typed), after_offset, limit + 1),
                )
                rows = tuple(db_cursor.fetchall())
            finally:
                db_cursor.close()
            uow.commit()
        visible = rows[:limit]
        events = tuple(_public_event(row) for row in visible)
        next_offset = after_offset if not visible else int(visible[-1][0])
        expires_at = now + timedelta(hours=24)
        next_cursor = self.cursor_codec.encode(
            {
                "kind": "sleep_event_cursor.pg.v1",
                "authority": self._cursor_authority(typed),
                "delivery_offset": next_offset,
                "expires_at": expires_at.isoformat(),
                "event_schema_generation": EVENT_SCHEMA_GENERATION,
            }
        )
        return EventPollResponse(
            event_schema_generation=EVENT_SCHEMA_GENERATION,
            events=events,
            next_cursor=next_cursor,
            cursor_expires_at=expires_at,
            has_more=len(rows) > limit,
        )

    def _context(
        self,
        context: AuthenticatedActorContext,
        subject_id: str,
    ) -> PostgresAuthenticatedActorContext:
        self.require_subject(context, subject_id)
        if not isinstance(context, PostgresAuthenticatedActorContext):
            raise SleepApiApplicationError(
                PublicErrorCode.AUTHORIZATION_UNAVAILABLE,
                "PostgreSQL authority context is unavailable.",
                status_code=503,
                retryable=True,
            )
        return context

    def _scope(self, context: PostgresAuthenticatedActorContext) -> UowScope:
        authority = context.authority
        role = (
            "family"
            if context.claims.role == PublicActorRole.CAREGIVER
            else context.claims.role.value
        )
        return UowScope(
            namespace_id=authority.namespace_id,
            namespace_generation=authority.namespace_generation,
            data_mode=authority.data_mode,  # type: ignore[arg-type]
            run_id=authority.run_id,
            arm_id=authority.arm_id,
            process_role="api",
            purpose=self.purpose,
            service_principal_id=context.service_principal.principal_id,
            subject_id=context.claims.subject_id,
            actor_id=context.claims.actor_id,
            actor_role=role,  # type: ignore[arg-type]
            authorization_epoch=authority.authorization_epoch,
            privacy_epoch=authority.privacy_epoch,
            retrieval_policy_epoch=authority.retrieval_policy_epoch,
        )

    @staticmethod
    def _authority_params(
        context: PostgresAuthenticatedActorContext,
    ) -> tuple[Any, ...]:
        authority = context.authority
        return (
            authority.namespace_id,
            authority.data_mode,
            authority.namespace_generation,
            context.claims.subject_id,
            authority.run_id,
            authority.arm_id,
        )

    @staticmethod
    def _cursor_authority(
        context: PostgresAuthenticatedActorContext,
    ) -> dict[str, Any]:
        authority = context.authority
        return {
            "namespace_id": authority.namespace_id,
            "namespace_generation": authority.namespace_generation,
            "data_mode": authority.data_mode,
            "run_id": authority.run_id,
            "arm_id": authority.arm_id,
            "service_principal_id": context.service_principal.principal_id,
            "actor_id": context.claims.actor_id,
            "subject_id": context.claims.subject_id,
            "role": context.claims.role.value,
            "authorization_epoch": authority.authorization_epoch,
            "privacy_epoch": authority.privacy_epoch,
            "retrieval_policy_epoch": authority.retrieval_policy_epoch,
        }


def build_postgres_sleep_api_runtime(
    settings: SleepBackendSettings,
    uow_factory: UnitOfWorkFactory[Any],
    *,
    cursor_key: bytes,
) -> PostgresSleepApiRuntime:
    identity = build_product_authenticator(settings, uow_factory)
    authority = PostgresAuthorityStore(settings, uow_factory)
    return PostgresSleepApiRuntime(
        uow_factory=uow_factory,
        authenticator=PostgresPublicAuthenticator(identity, authority),
        cursor_key=cursor_key,
    )


_EPISODE_SELECT = """
    SELECT episode.night_episode_id, episode.subject_id,
           episode.episode_local_date, episode.timezone_name,
           episode.collection_start_at, episode.wake_at,
           episode.deterministic_close_deadline_at, episode.state,
           episode.current_revision_number, episode.current_revision_id,
           episode.updated_at, episode.bed_local_date,
           episode.wake_local_date, episode.assignment_basis,
           episode.date_confidence, episode.assignment_estimated,
           COALESCE(
             (episode.episode_json->>'legacy_local_sleep_date')::date,
             episode.bed_local_date, episode.episode_local_date
           ) AS legacy_local_sleep_date,
           quality.assessment_json, episode.created_at
    FROM public.sleep_domain_night_episodes AS episode
    LEFT JOIN public.sleep_domain_current_quality AS quality
      ON quality.namespace_id = episode.namespace_id
     AND quality.data_mode = episode.data_mode
     AND quality.night_episode_id = episode.night_episode_id
     AND quality.subject_id = episode.subject_id
     AND quality.assessment_json #>>
           '{source_scope,night_episode_revision_id}' =
         episode.current_revision_id
"""


def _episode_summary(row: Any) -> NightEpisodeSummary:
    quality = None if row[17] is None else _json_value(row[17])
    return NightEpisodeSummary(
        night_episode_id=str(row[0]),
        subject_id=str(row[1]),
        local_sleep_date=row[16],
        episode_local_date=row[2],
        assignment_basis=str(row[13]),
        bed_local_date=row[11],
        wake_local_date=row[12],
        date_confidence=str(row[14]),
        assignment_estimated=bool(row[15]),
        timezone_name=str(row[3]),
        collection_start_at=row[4],
        collection_end_at=row[5] or row[6],
        lifecycle_state=str(row[7]),
        data_sufficiency=(
            "report_pending"
            if quality is None
            else str(quality.get("data_sufficiency", "unknown"))
        ),
        quality_flags=(
            ()
            if quality is None
            else tuple(str(item) for item in quality.get("reason_codes", ()))
        ),
        current_revision_number=(None if row[8] is None else int(row[8])),
        current_revision_id=None if row[9] is None else str(row[9]),
        committed_at=row[10],
    )


def _revision_snapshot(row: Any) -> NightEpisodeRevisionSnapshot:
    payload = _json_value(row[5])
    return NightEpisodeRevisionSnapshot(
        night_episode_id=str(row[1]),
        night_episode_revision_id=str(row[0]),
        subject_id=str(row[2]),
        revision_number=int(row[3]),
        parent_revision_id=None if row[4] is None else str(row[4]),
        revision_cause=str(payload.get("revision_cause", "normalized_observation")),
        data_sufficiency=str(payload.get("data_sufficiency", "report_pending")),
        quality_flags=tuple(
            str(item) for item in payload.get("quality_flags", ())
        ),
        episode_local_date=row[7],
        assignment_basis=None if row[8] is None else str(row[8]),
        bed_local_date=row[9],
        wake_local_date=row[10],
        date_confidence=None if row[11] is None else str(row[11]),
        assignment_estimated=row[12],
        created_at=row[6],
    )


def _public_event(row: Any) -> ExternalDomainEvent:
    payload = _json_value(row[8])
    occurred = payload.get("event_occurred_at") or row[9]
    if isinstance(occurred, str):
        occurred = datetime.fromisoformat(occurred)
    safe_payload: dict[str, str | int | float | bool | None] = {}
    for key in (
        "episode_local_date",
        "assignment_basis",
        "date_state",
        "risk_state",
        "urgent",
        "model_invocation_count",
        "synthetic_non_release",
    ):
        value = payload.get(key)
        if value is None or isinstance(value, (str, int, float, bool)):
            safe_payload[key] = value
    return ExternalDomainEvent(
        event_id=str(row[1]),
        event_type=str(row[2]),
        event_version="2",
        aggregate_id=str(row[3]),
        aggregate_version=int(row[4]),
        per_aggregate_sequence=int(row[5]),
        subject_id=str(row[6]),
        night_episode_id=(
            None
            if payload.get("night_episode_id") is None
            else str(payload["night_episode_id"])
        ),
        night_episode_revision_id=(
            None
            if payload.get("night_episode_revision_id") is None
            else str(payload["night_episode_revision_id"])
        ),
        operation_id=None if row[7] is None else str(row[7]),
        occurred_at=occurred,
        persisted_at=row[9],
        payload=safe_payload,
    )


def _operation_status(value: str) -> PublicOperationStatus:
    if value in {"pending", "retry"}:
        return PublicOperationStatus.PENDING
    if value == "running":
        return PublicOperationStatus.RUNNING
    if value == "succeeded":
        return PublicOperationStatus.SUCCEEDED
    if value == "cancelled":
        return PublicOperationStatus.CANCELLED
    return PublicOperationStatus.FAILED


def _json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8")
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise SleepApiApplicationError(
            PublicErrorCode.INTERNAL_ERROR,
            "Committed projection JSON is invalid.",
            status_code=500,
        )
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
    "PUBLIC_PURPOSE",
    "PostgresAuthenticatedActorContext",
    "PostgresPublicAuthenticator",
    "PostgresSleepApiRuntime",
    "build_postgres_sleep_api_runtime",
]
