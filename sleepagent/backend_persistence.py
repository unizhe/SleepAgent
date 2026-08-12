"""PostgreSQL adapters for identity, Product API, and durable replay safety."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import ValidationError
from starlette.requests import Request

from sleepagent.backend_keys import BackendKeyProvider
from sleepagent.backend_settings import DeploymentMode, SleepBackendSettings
from sleepagent.product_api.contracts import (
    CareActionRecord,
    CareFollowupRecord,
    InteractionStatusResponse,
    ProductCareResponse,
    ProductRecordsResponse,
    ProductRole,
    ProductSleepTodayProjection,
    ProductTrendsResponse,
    PublicOperationState,
    SleepRecord,
    TrendPoint,
)
from sleepagent.product_api.service import (
    ProductApiError,
    ProductBackend,
    ProductIdentityResolver,
    ProductRequestContext,
)
from sleepagent.persistence.uow import (
    AuthorityResolutionScope,
    UnitOfWorkFactory,
    UowScope,
)
from sleepagent.sleep_api.auth import (
    ActorAssertionVerifier,
    ActorVerificationKey,
    FailClosedRoleBindingResolver,
    HttpsBearerServicePrincipalVerifier,
    RotatingServiceCredential,
    SleepApiAuthenticator,
    SleepApiSecurityError,
)
from sleepagent.sleep_api.contracts import PublicErrorCode
from sleepagent.sleep_domain.episode_v2 import EpisodeAssignmentBasis, UUID7Generator


UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class ResolvedActorAuthority:
    namespace_id: str
    data_mode: str
    namespace_generation: int
    run_id: str | None
    arm_id: str | None
    binding_id: str
    role: ProductRole
    effective_scopes: frozenset[str]
    authorization_epoch: int
    privacy_epoch: int
    retrieval_policy_epoch: int


class PostgresAuthorityStore:
    def __init__(
        self,
        settings: SleepBackendSettings,
        uow_factory: UnitOfWorkFactory[Any],
    ) -> None:
        self.settings = settings
        self.uow_factory = uow_factory

    def resolve(
        self,
        *,
        actor_id: str,
        subject_id: str,
        role: ProductRole,
        purpose: str,
    ) -> ResolvedActorAuthority:
        scope = AuthorityResolutionScope(
            data_mode=self.settings.data_mode.value,
            purpose=purpose,
            service_principal_id=self.settings.service_principal_id,
            actor_id=actor_id,
        )
        try:
            with self.uow_factory.begin(scope) as uow:
                cursor = uow.connection.cursor()
                try:
                    cursor.execute(
                        "SELECT * FROM public.sleepagent_resolve_actor_authority("
                        "%s, %s, %s, %s)",
                        (actor_id, subject_id, role.value, purpose),
                    )
                    row = cursor.fetchone()
                finally:
                    cursor.close()
                uow.commit()
        except Exception as exc:
            if getattr(exc, "sqlstate", None) != "P0001":
                raise ProductApiError(
                    "authorization_unavailable",
                    "The authority store is temporarily unavailable.",
                    status_code=503,
                    retryable=True,
                ) from exc
            raise ProductApiError(
                "authorization_denied",
                "No unique active authority binding grants this request.",
                status_code=403,
            ) from exc
        if row is None:
            raise ProductApiError(
                "authorization_denied",
                "No active authority binding grants this request.",
                status_code=403,
            )
        scopes_value = row[7]
        if isinstance(scopes_value, str):
            scopes_value = json.loads(scopes_value)
        return ResolvedActorAuthority(
            namespace_id=str(row[0]),
            data_mode=str(row[1]),
            namespace_generation=int(row[2]),
            run_id=None if row[3] is None else str(row[3]),
            arm_id=None if row[4] is None else str(row[4]),
            binding_id=str(row[5]),
            role=ProductRole(str(row[6])),
            effective_scopes=frozenset(str(item) for item in scopes_value),
            authorization_epoch=int(row[8]),
            privacy_epoch=int(row[9]),
            retrieval_policy_epoch=int(row[10]),
        )


class PostgresAssertionReplayStore:
    """Pre-authority, principal-bound nonce reservation using server time."""

    def __init__(
        self,
        settings: SleepBackendSettings,
        uow_factory: UnitOfWorkFactory[Any],
    ) -> None:
        self.settings = settings
        self.uow_factory = uow_factory

    def consume(
        self,
        *,
        issuer: str,
        assertion_id: str,
        nonce: str,
        expires_at: datetime,
        now: datetime,
    ) -> bool:
        del now  # ControlClock is PostgreSQL clock_timestamp() for this CAS.
        scope = AuthorityResolutionScope(
            data_mode=self.settings.data_mode.value,
            purpose="actor_assertion",
            service_principal_id=self.settings.service_principal_id,
            actor_id="pre-authority-assertion",
        )
        try:
            with self.uow_factory.begin(scope) as uow:
                cursor = uow.connection.cursor()
                try:
                    cursor.execute(
                        "SELECT public.sleepagent_consume_actor_assertion("
                        "%s, %s, %s, %s)",
                        (issuer, assertion_id, nonce, expires_at),
                    )
                    row = cursor.fetchone()
                finally:
                    cursor.close()
                uow.commit()
        except Exception as exc:
            raise SleepApiSecurityError(
                PublicErrorCode.AUTHORIZATION_UNAVAILABLE,
                "Actor assertion replay protection is unavailable.",
                status_code=503,
                retryable=True,
            ) from exc
        return bool(row and row[0])


class PostgresProductIdentityResolver(ProductIdentityResolver):
    def __init__(
        self,
        *,
        authenticator: SleepApiAuthenticator,
        authority: PostgresAuthorityStore,
    ) -> None:
        self.authenticator = authenticator
        self.authority = authority

    def resolve(
        self,
        request: Request,
        *,
        body: bytes,
        purpose: str,
    ) -> ProductRequestContext:
        try:
            identity = self.authenticator.verify_identity(
                request,
                body=body,
                required_scopes=frozenset(),
            )
        except SleepApiSecurityError as exc:
            raise ProductApiError(
                exc.code.value.lower(),
                str(exc),
                status_code=exc.status_code,
                retryable=exc.retryable,
            ) from exc
        if identity.service_principal.principal_id != self.authority.settings.service_principal_id:
            raise ProductApiError(
                "authorization_denied",
                "The service credential is outside this deployment grant.",
                status_code=403,
            )
        try:
            role = ProductRole(identity.claims.role.value)
        except ValueError as exc:
            raise ProductApiError(
                "authorization_denied",
                "The asserted role is not a Product role.",
                status_code=403,
            ) from exc
        resolved = self.authority.resolve(
            actor_id=identity.claims.actor_id,
            subject_id=identity.claims.subject_id,
            role=role,
            purpose=purpose,
        )
        claimed_epochs = (
            identity.claims.authorization_epoch,
            identity.claims.privacy_epoch,
            identity.claims.retrieval_policy_epoch,
        )
        current_epochs = (
            resolved.authorization_epoch,
            resolved.privacy_epoch,
            resolved.retrieval_policy_epoch,
        )
        if claimed_epochs != current_epochs:
            raise ProductApiError(
                "stale_actor_assertion",
                "The actor assertion governance epochs are stale.",
                status_code=403,
            )
        asserted = frozenset(identity.claims.scope)
        effective = resolved.effective_scopes.intersection(asserted)
        policy_sha256 = _sha256(
            {
                "schema_version": "authorization_policy.v1",
                "principal_id": identity.service_principal.principal_id,
                "binding_id": resolved.binding_id,
                "role": resolved.role.value,
                "effective_scopes": sorted(resolved.effective_scopes),
                "namespace_id": resolved.namespace_id,
                "namespace_generation": resolved.namespace_generation,
                "purpose": purpose,
                "authorization_epoch": resolved.authorization_epoch,
                "privacy_epoch": resolved.privacy_epoch,
                "retrieval_policy_epoch": resolved.retrieval_policy_epoch,
            }
        )
        return ProductRequestContext(
            service_principal_id=identity.service_principal.principal_id,
            actor_id=identity.claims.actor_id,
            binding_id=resolved.binding_id,
            subject_id=identity.claims.subject_id,
            role=resolved.role,
            effective_scopes=effective,
            namespace_id=resolved.namespace_id,
            namespace_generation=resolved.namespace_generation,
            data_mode=resolved.data_mode,
            run_id=resolved.run_id,
            arm_id=resolved.arm_id,
            purpose=purpose,
            authorization_epoch=resolved.authorization_epoch,
            privacy_epoch=resolved.privacy_epoch,
            retrieval_epoch=resolved.retrieval_policy_epoch,
            policy_sha256=policy_sha256,
        )


class _ProductCursorCodec:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("Product cursor key must contain 32 bytes")
        self._key = key

    def encode(self, payload: Mapping[str, Any]) -> str:
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._key).encrypt(
            nonce,
            _canonical_json(payload),
            b"sleepagent-product-cursor.v1",
        )
        return base64.urlsafe_b64encode(nonce + ciphertext).rstrip(b"=").decode()

    def decode(self, value: str) -> dict[str, Any]:
        try:
            padded = value + "=" * ((-len(value)) % 4)
            raw = base64.b64decode(padded, altchars=b"-_", validate=True)
            decoded = AESGCM(self._key).decrypt(
                raw[:12], raw[12:], b"sleepagent-product-cursor.v1"
            )
            payload = json.loads(decoded)
            if not isinstance(payload, dict):
                raise ValueError("cursor payload is not an object")
            return payload
        except (InvalidTag, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ProductApiError(
                "invalid_cursor",
                "The Product cursor is invalid or stale.",
                status_code=400,
            ) from exc


class PostgresProductBackend(ProductBackend):
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        cursor_key: bytes,
        id_generator: UUID7Generator | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.cursor_codec = _ProductCursorCodec(cursor_key)
        self.id_generator = id_generator or UUID7Generator()

    def get_today_projection(
        self,
        context: ProductRequestContext,
    ) -> ProductSleepTodayProjection | None:
        with self.uow_factory.begin(_uow_scope(context)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT view.public_today_json,
                           view.public_projection_sha256,
                           view.public_committed_at
                    FROM public.sleep_domain_analysis_role_views AS view
                    JOIN public.sleep_domain_analysis_revisions AS analysis
                      ON analysis.analysis_revision_id = view.analysis_revision_id
                     AND analysis.namespace_id = view.namespace_id
                     AND analysis.data_mode = view.data_mode
                     AND analysis.subject_id = view.subject_id
                     AND analysis.night_episode_id = view.night_episode_id
                     AND analysis.night_episode_revision_id =
                         view.night_episode_revision_id
                    JOIN public.sleep_domain_night_episodes AS episode
                      ON episode.night_episode_id = view.night_episode_id
                     AND episode.namespace_id = view.namespace_id
                     AND episode.data_mode = view.data_mode
                     AND episode.subject_id = view.subject_id
                     AND episode.namespace_generation =
                         view.namespace_generation
                     AND episode.current_revision_id =
                         view.night_episode_revision_id
                     AND episode.date_state = 'finalized'
                     AND episode.date_conflict = FALSE
                    WHERE view.protocol_version >= 2
                      AND view.public_schema_version =
                          'product_sleep_today.v1'
                      AND view.namespace_id = %s AND view.data_mode = %s
                      AND view.namespace_generation = %s
                      AND COALESCE(view.run_id, '') = COALESCE(%s, '')
                      AND COALESCE(view.arm_id, '') = COALESCE(%s, '')
                      AND view.subject_id = %s AND view.role = %s
                      AND view.authorization_epoch = %s
                      AND view.privacy_epoch = %s
                      AND view.retrieval_policy_epoch = %s
                      AND NOT EXISTS (
                        SELECT 1
                        FROM public.sleep_domain_analysis_revisions AS newer
                        WHERE newer.namespace_id = analysis.namespace_id
                          AND newer.data_mode = analysis.data_mode
                          AND newer.subject_id = analysis.subject_id
                          AND newer.night_episode_id = analysis.night_episode_id
                          AND newer.night_episode_revision_id =
                              analysis.night_episode_revision_id
                          AND newer.revision_number > analysis.revision_number
                      )
                    ORDER BY view.public_committed_at DESC,
                             view.role_view_id DESC
                    LIMIT 1
                    """,
                    (
                        context.namespace_id,
                        context.data_mode,
                        context.namespace_generation,
                        context.run_id,
                        context.arm_id,
                        context.subject_id,
                        context.role.value,
                        context.authorization_epoch,
                        context.privacy_epoch,
                        context.retrieval_epoch,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if row is None:
            return None
        payload = row[0]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise RuntimeError("public today projection is invalid JSON") from exc
        if not isinstance(payload, dict) or _sha256(payload) != str(row[1]):
            raise RuntimeError("public today projection integrity check failed")
        try:
            projection = ProductSleepTodayProjection.model_validate_json(
                _canonical_json(payload)
            )
        except ValidationError as exc:
            raise RuntimeError("public today projection contract is invalid") from exc
        if projection.committed_at != row[2]:
            raise RuntimeError("public today commit timestamp drifted")
        return projection

    def get_trends(
        self,
        context: ProductRequestContext,
        *,
        limit: int,
        cursor: str | None,
    ) -> ProductTrendsResponse:
        after_time, after_id = self._read_cursor(
            context, kind="trends", cursor=cursor
        )
        with self.uow_factory.begin(_uow_scope(context)) as uow:
            db_cursor = uow.connection.cursor()
            try:
                db_cursor.execute(
                    """
                    SELECT episode.night_episode_id,
                           episode.current_revision_id,
                           episode.episode_local_date,
                           episode.assignment_basis,
                           analysis.analysis_revision_id,
                           view.role_view_id,
                           view.status,
                           CASE WHEN episode.bed_at IS NULL
                                  OR episode.wake_at IS NULL THEN NULL
                                ELSE FLOOR(EXTRACT(EPOCH FROM
                                  (episode.wake_at - episode.bed_at)) / 60)::int
                           END,
                           view.public_committed_at
                    FROM public.sleep_domain_analysis_role_views AS view
                    JOIN public.sleep_domain_analysis_revisions AS analysis
                      ON analysis.analysis_revision_id = view.analysis_revision_id
                     AND analysis.namespace_id = view.namespace_id
                     AND analysis.data_mode = view.data_mode
                     AND analysis.subject_id = view.subject_id
                    JOIN public.sleep_domain_night_episodes AS episode
                      ON episode.night_episode_id = analysis.night_episode_id
                     AND episode.namespace_id = analysis.namespace_id
                     AND episode.data_mode = analysis.data_mode
                     AND episode.subject_id = analysis.subject_id
                     AND episode.current_revision_id =
                         analysis.night_episode_revision_id
                    WHERE view.protocol_version >= 2
                      AND view.public_schema_version = 'product_sleep_today.v1'
                      AND view.public_committed_at IS NOT NULL
                      AND view.namespace_id = %s AND view.data_mode = %s
                      AND view.namespace_generation = %s
                      AND COALESCE(view.run_id, '') = COALESCE(%s, '')
                      AND COALESCE(view.arm_id, '') = COALESCE(%s, '')
                      AND view.subject_id = %s AND view.role = %s
                      AND view.authorization_epoch = %s
                      AND view.privacy_epoch = %s
                      AND view.retrieval_policy_epoch = %s
                      AND episode.date_state = 'finalized'
                      AND episode.date_conflict = FALSE
                      AND NOT EXISTS (
                        SELECT 1
                        FROM public.sleep_domain_analysis_revisions AS newer
                        WHERE newer.namespace_id = analysis.namespace_id
                          AND newer.data_mode = analysis.data_mode
                          AND newer.subject_id = analysis.subject_id
                          AND newer.night_episode_revision_id =
                              analysis.night_episode_revision_id
                          AND newer.revision_number > analysis.revision_number
                      )
                      AND (%s::timestamptz IS NULL OR
                        (view.public_committed_at, view.role_view_id) < (%s, %s))
                    ORDER BY view.public_committed_at DESC, view.role_view_id DESC
                    LIMIT %s
                    """,
                    (
                        *_product_scope_params(context),
                        context.role.value,
                        context.authorization_epoch,
                        context.privacy_epoch,
                        context.retrieval_epoch,
                        after_time,
                        after_time,
                        after_id,
                        limit + 1,
                    ),
                )
                rows = db_cursor.fetchall()
            finally:
                db_cursor.close()
            uow.commit()
        visible = rows[:limit]
        items = tuple(
            TrendPoint(
                night_episode_id=str(row[0]),
                night_episode_revision_id=str(row[1]),
                episode_local_date=row[2],
                assignment_basis=EpisodeAssignmentBasis(str(row[3])),
                analysis_revision_id=str(row[4]),
                projection_id=str(row[5]),
                projection_state=str(row[6]),
                sleep_window_minutes=None if row[7] is None else int(row[7]),
                committed_at=row[8],
            )
            for row in visible
        )
        return ProductTrendsResponse(
            data_mode=context.data_mode,
            synthetic_non_release=context.data_mode == "replay",
            subject_ref=context.subject_id,
            role=context.role,
            items=items,
            next_cursor=self._next_read_cursor(
                context,
                kind="trends",
                rows=rows,
                visible=visible,
                limit=limit,
                time_index=8,
                id_index=5,
            ),
        )

    def get_records(
        self,
        context: ProductRequestContext,
        *,
        limit: int,
        cursor: str | None,
    ) -> ProductRecordsResponse:
        after_time, after_id = self._read_cursor(
            context, kind="records", cursor=cursor
        )
        with self.uow_factory.begin(_uow_scope(context)) as uow:
            db_cursor = uow.connection.cursor()
            try:
                db_cursor.execute(
                    """
                    SELECT analysis.analysis_revision_id,
                           analysis.revision_number,
                           analysis.parent_analysis_revision_id,
                           analysis.night_episode_id,
                           analysis.night_episode_revision_id,
                           view.role_view_id,
                           COALESCE(view.source_state_version, 1),
                           view.status,
                           (episode.current_revision_id =
                              analysis.night_episode_revision_id
                            AND NOT EXISTS (
                              SELECT 1
                              FROM public.sleep_domain_analysis_revisions AS newer
                              WHERE newer.namespace_id = analysis.namespace_id
                                AND newer.data_mode = analysis.data_mode
                                AND newer.subject_id = analysis.subject_id
                                AND newer.night_episode_revision_id =
                                    analysis.night_episode_revision_id
                                AND newer.revision_number >
                                    analysis.revision_number
                            )),
                           view.public_committed_at
                    FROM public.sleep_domain_analysis_role_views AS view
                    JOIN public.sleep_domain_analysis_revisions AS analysis
                      ON analysis.analysis_revision_id = view.analysis_revision_id
                     AND analysis.namespace_id = view.namespace_id
                     AND analysis.data_mode = view.data_mode
                     AND analysis.subject_id = view.subject_id
                    JOIN public.sleep_domain_night_episodes AS episode
                      ON episode.night_episode_id = analysis.night_episode_id
                     AND episode.namespace_id = analysis.namespace_id
                     AND episode.data_mode = analysis.data_mode
                     AND episode.subject_id = analysis.subject_id
                    WHERE view.protocol_version >= 2
                      AND view.public_schema_version = 'product_sleep_today.v1'
                      AND view.public_committed_at IS NOT NULL
                      AND view.namespace_id = %s AND view.data_mode = %s
                      AND view.namespace_generation = %s
                      AND COALESCE(view.run_id, '') = COALESCE(%s, '')
                      AND COALESCE(view.arm_id, '') = COALESCE(%s, '')
                      AND view.subject_id = %s AND view.role = %s
                      AND view.authorization_epoch = %s
                      AND view.privacy_epoch = %s
                      AND view.retrieval_policy_epoch = %s
                      AND (%s::timestamptz IS NULL OR
                        (view.public_committed_at, view.role_view_id) < (%s, %s))
                    ORDER BY view.public_committed_at DESC, view.role_view_id DESC
                    LIMIT %s
                    """,
                    (
                        *_product_scope_params(context),
                        context.role.value,
                        context.authorization_epoch,
                        context.privacy_epoch,
                        context.retrieval_epoch,
                        after_time,
                        after_time,
                        after_id,
                        limit + 1,
                    ),
                )
                rows = db_cursor.fetchall()
            finally:
                db_cursor.close()
            uow.commit()
        visible = rows[:limit]
        items = tuple(
            SleepRecord(
                analysis_revision_id=str(row[0]),
                analysis_revision_number=int(row[1]),
                parent_analysis_revision_id=(
                    None if row[2] is None else str(row[2])
                ),
                night_episode_id=str(row[3]),
                night_episode_revision_id=str(row[4]),
                projection_id=str(row[5]),
                projection_version=int(row[6]),
                projection_state=str(row[7]),
                is_current=bool(row[8]),
                committed_at=row[9],
            )
            for row in visible
        )
        return ProductRecordsResponse(
            data_mode=context.data_mode,
            synthetic_non_release=context.data_mode == "replay",
            subject_ref=context.subject_id,
            role=context.role,
            items=items,
            next_cursor=self._next_read_cursor(
                context,
                kind="records",
                rows=rows,
                visible=visible,
                limit=limit,
                time_index=9,
                id_index=5,
            ),
        )

    def get_care(
        self,
        context: ProductRequestContext,
        *,
        limit: int,
        cursor: str | None,
    ) -> ProductCareResponse:
        after_time, after_id = self._read_cursor(
            context, kind="care", cursor=cursor
        )
        with self.uow_factory.begin(_uow_scope(context)) as uow:
            db_cursor = uow.connection.cursor()
            try:
                db_cursor.execute(
                    """
                    WITH authorized_actions AS (
                      SELECT action.*, interaction.night_episode_id
                      FROM public.backend_care_actions_v2 AS action
                      JOIN public.backend_human_decisions_v2 AS decision
                        ON decision.human_decision_id = action.human_decision_id
                       AND decision.interaction_id = action.interaction_id
                       AND decision.namespace_id = action.namespace_id
                       AND decision.data_mode = action.data_mode
                       AND decision.subject_id = action.subject_id
                       AND decision.choice = 'confirm'
                      JOIN public.backend_product_interactions AS interaction
                        ON interaction.interaction_id = action.interaction_id
                       AND interaction.namespace_id = action.namespace_id
                       AND interaction.data_mode = action.data_mode
                       AND interaction.subject_id = action.subject_id
                      WHERE action.namespace_id = %s AND action.data_mode = %s
                        AND action.namespace_generation = %s
                        AND COALESCE(action.run_id, '') = COALESCE(%s, '')
                        AND COALESCE(action.arm_id, '') = COALESCE(%s, '')
                        AND action.subject_id = %s
                        AND decision.authorization_epoch = %s
                        AND decision.privacy_epoch = %s
                        AND decision.retrieval_policy_epoch = %s
                        AND action.action_json ->> 'action_kind' <> ''
                    ), records AS (
                      SELECT 'care_action'::text AS record_type,
                        action.care_action_id AS record_id,
                        action.interaction_id, action.state,
                        action.action_json ->> 'action_kind' AS action_kind,
                        action.source_analysis_revision_id,
                        action.confirmed_at, action.updated_at,
                        NULL::text AS night_episode_id,
                        action.confirmed_at AS sort_time
                      FROM authorized_actions AS action
                      UNION ALL
                      SELECT 'care_followup'::text,
                        'care-followup:' || followup.night_episode_id,
                        NULL::text, followup.state, NULL::text, NULL::text,
                        NULL::timestamptz, followup.updated_at,
                        followup.night_episode_id, followup.updated_at
                      FROM public.sleep_domain_care_followups AS followup
                      WHERE followup.state <> 'none'
                        AND EXISTS (
                          SELECT 1 FROM authorized_actions AS action
                          WHERE action.night_episode_id = followup.night_episode_id
                            AND action.namespace_id = followup.namespace_id
                            AND action.data_mode = followup.data_mode
                            AND action.namespace_generation =
                              followup.namespace_generation
                            AND COALESCE(action.run_id, '') =
                              COALESCE(followup.run_id, '')
                            AND COALESCE(action.arm_id, '') =
                              COALESCE(followup.arm_id, '')
                            AND action.subject_id = followup.subject_id
                        )
                    )
                    SELECT record_type, record_id, interaction_id, state,
                      action_kind, source_analysis_revision_id, confirmed_at,
                      updated_at, night_episode_id, sort_time
                    FROM records
                    WHERE (%s::timestamptz IS NULL OR
                      (sort_time, record_id) < (%s, %s))
                    ORDER BY sort_time DESC, record_id DESC
                    LIMIT %s
                    """,
                    (
                        *_product_scope_params(context),
                        context.authorization_epoch,
                        context.privacy_epoch,
                        context.retrieval_epoch,
                        after_time,
                        after_time,
                        after_id,
                        limit + 1,
                    ),
                )
                rows = db_cursor.fetchall()
            finally:
                db_cursor.close()
            uow.commit()
        visible = rows[:limit]
        items = tuple(_care_record(row) for row in visible)
        return ProductCareResponse(
            data_mode=context.data_mode,
            synthetic_non_release=context.data_mode == "replay",
            subject_ref=context.subject_id,
            role=context.role,
            items=items,
            next_cursor=self._next_read_cursor(
                context,
                kind="care",
                rows=rows,
                visible=visible,
                limit=limit,
                time_index=9,
                id_index=1,
            ),
        )

    def reserve_command(
        self,
        context: ProductRequestContext,
        *,
        route_template: str,
        command_type: str,
        idempotency_key: str,
        body_sha256: str,
        payload: Mapping[str, Any],
        target_id: str | None,
    ) -> str:
        queue_name = _command_queue(command_type)
        reservation_material = _sha256(
            {
                "service_principal_id": context.service_principal_id,
                "actor_id": context.actor_id,
                "route_template": route_template,
                "caller_idempotency_key": idempotency_key,
            }
        )
        with self.uow_factory.begin(_uow_scope(context)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (reservation_material,),
                )
                existing = self._existing_receipt(
                    cursor,
                    context=context,
                    route_template=route_template,
                    idempotency_key=idempotency_key,
                )
                if existing is not None:
                    if str(existing[1]) != body_sha256:
                        raise ProductApiError(
                            "idempotency_conflict",
                            "Idempotency-Key was already used with another body.",
                            status_code=409,
                        )
                    if (
                        str(existing[2]) != context.namespace_id
                        or str(existing[3]) != context.data_mode
                        or str(existing[4]) != context.subject_id
                    ):
                        raise ProductApiError(
                            "idempotency_scope_conflict",
                            "Idempotency-Key is already bound to another authority scope.",
                            status_code=409,
                        )
                    existing_snapshot = existing[5]
                    if isinstance(existing_snapshot, str):
                        existing_snapshot = json.loads(existing_snapshot)
                    if not isinstance(existing_snapshot, Mapping) or any(
                        existing_snapshot.get(name) != expected
                        for name, expected in (
                            ("binding_id", context.binding_id),
                            (
                                "authorization_epoch",
                                context.authorization_epoch,
                            ),
                            ("privacy_epoch", context.privacy_epoch),
                            (
                                "retrieval_policy_epoch",
                                context.retrieval_epoch,
                            ),
                            ("policy_sha256", context.policy_sha256),
                        )
                    ):
                        raise ProductApiError(
                            "idempotency_authority_conflict",
                            "Idempotency-Key is bound to an older authority snapshot.",
                            status_code=409,
                        )
                    uow.commit()
                    return str(existing[0])
                receipt_id = str(self.id_generator())
                snapshot = _authorization_snapshot(context)
                semantic_key = _sha256(
                    {
                        "namespace_id": context.namespace_id,
                        "namespace_generation": context.namespace_generation,
                        "run_id": context.run_id,
                        "arm_id": context.arm_id,
                        "subject_id": context.subject_id,
                        "command_type": command_type,
                        "target_id": target_id,
                        "payload": payload,
                        "policy_sha256": context.policy_sha256,
                    }
                )
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))",
                    (semantic_key,),
                )
                cursor.execute(
                    """
                    SELECT operation_id
                    FROM public.sleep_domain_operations
                    WHERE namespace_id = %s AND data_mode = %s
                      AND namespace_generation = %s
                      AND COALESCE(run_id, '') = COALESCE(%s, '')
                      AND COALESCE(arm_id, '') = COALESCE(%s, '')
                      AND operation_type = %s AND semantic_key = %s
                      AND protocol_version >= 2
                    """,
                    (
                        context.namespace_id,
                        context.data_mode,
                        context.namespace_generation,
                        context.run_id,
                        context.arm_id,
                        command_type,
                        semantic_key,
                    ),
                )
                semantic_existing = cursor.fetchone()
                operation_created = semantic_existing is None
                operation_id = (
                    str(self.id_generator())
                    if semantic_existing is None
                    else str(semantic_existing[0])
                )
                operation_json = {
                    "schema_version": "backend_operation.v2",
                    "command_type": command_type,
                    "payload": payload,
                    "target_id": target_id,
                    "authorization_snapshot": snapshot,
                }
                if operation_created:
                    cursor.execute(
                        """
                        INSERT INTO public.sleep_domain_operations (
                          operation_id, namespace_id, data_mode, operation_type,
                          subject_id, service_principal_id, actor_id,
                          target_resource_id, target_resource_key, idempotency_key,
                          request_sha256, status, attempt_count, cas_version,
                          operation_json, created_at, updated_at, protocol_version,
                          namespace_generation, run_id, arm_id, id_scheme,
                          origin_kind, semantic_key, queue_name, priority,
                          available_at, max_attempts, authorization_snapshot_json,
                          policy_sha256
                        ) VALUES (
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, 'pending', 0, 0, %s::jsonb,
                          clock_timestamp(), clock_timestamp(), 2,
                          %s, %s, %s, 'uuidv7', 'user', %s,
                          %s, 0, clock_timestamp(), 5, %s::jsonb, %s
                        )
                        """,
                        (
                            operation_id,
                            context.namespace_id,
                            context.data_mode,
                            command_type,
                            context.subject_id,
                            context.service_principal_id,
                            context.actor_id,
                            target_id,
                            target_id or "",
                            idempotency_key,
                            body_sha256,
                            _json(operation_json),
                            context.namespace_generation,
                            context.run_id,
                            context.arm_id,
                            semantic_key,
                            queue_name,
                            _json(snapshot),
                            context.policy_sha256,
                        ),
                    )
                receipt_json = {
                    "schema_version": "command_receipt.v2",
                    "command_receipt_id": receipt_id,
                    "operation_id": operation_id,
                    "route_template": route_template,
                    "request_sha256": body_sha256,
                }
                cursor.execute(
                    """
                    INSERT INTO public.backend_command_receipts (
                      command_receipt_id, namespace_id, data_mode,
                      namespace_generation, run_id, arm_id,
                      service_principal_id, actor_id, subject_id,
                      route_template, caller_idempotency_key, request_sha256,
                      operation_id, authorization_snapshot_json, receipt_json
                    ) VALUES (
                      %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s::jsonb, %s::jsonb
                    )
                    """,
                    (
                        receipt_id,
                        context.namespace_id,
                        context.data_mode,
                        context.namespace_generation,
                        context.run_id,
                        context.arm_id,
                        context.service_principal_id,
                        context.actor_id,
                        context.subject_id,
                        route_template,
                        idempotency_key,
                        body_sha256,
                        operation_id,
                        _json(snapshot),
                        _json(receipt_json),
                    ),
                )
                if operation_created:
                    event_id = str(self.id_generator())
                    event = {
                        "schema_version": "committed_event.v2",
                        "event_id": event_id,
                        "event_type": "PRODUCT_COMMAND_ACCEPTED",
                        "operation_id": operation_id,
                        "subject_ref": _subject_pseudonym(context.subject_id),
                        "correlation_id": operation_id,
                        "causation_id": receipt_id,
                        "data_mode": context.data_mode,
                        "synthetic_non_release": context.data_mode == "replay",
                    }
                    cursor.execute(
                        """
                        INSERT INTO public.sleep_domain_domain_outbox (
                          event_id, namespace_id, data_mode, event_type,
                          aggregate_type, aggregate_id, aggregate_version,
                          per_aggregate_sequence, subject_id, operation_id,
                          status, available_at, event_json, created_at,
                          protocol_version, namespace_generation, run_id, arm_id
                        ) VALUES (
                          %s, %s, %s, 'PRODUCT_COMMAND_ACCEPTED',
                          'Operation', %s, 1, 1, %s, %s,
                          'committed', clock_timestamp(), %s::jsonb,
                          clock_timestamp(), 2, %s, %s, %s
                        )
                        """,
                        (
                            event_id,
                            context.namespace_id,
                            context.data_mode,
                            operation_id,
                            context.subject_id,
                            operation_id,
                            _json(event),
                            context.namespace_generation,
                            context.run_id,
                            context.arm_id,
                        ),
                    )
                cursor.execute(
                    """
                    INSERT INTO public.backend_authorization_audit (
                      audit_id, namespace_id, data_mode, subject_id,
                      principal_id, actor_id, binding_id, decision,
                      reason_code, policy_sha256, authorization_epoch,
                      privacy_epoch, retrieval_policy_epoch, audit_json,
                      occurred_at
                    ) VALUES (
                      %s, %s, %s, %s, %s, %s, %s, 'allow',
                      'product_command_reserved', %s, %s, %s, %s,
                      %s::jsonb, clock_timestamp()
                    )
                    """,
                    (
                        str(self.id_generator()),
                        context.namespace_id,
                        context.data_mode,
                        context.subject_id,
                        context.service_principal_id,
                        context.actor_id,
                        context.binding_id,
                        context.policy_sha256,
                        context.authorization_epoch,
                        context.privacy_epoch,
                        context.retrieval_epoch,
                        _json({"operation_id": operation_id, "route": route_template}),
                    ),
                )
            finally:
                cursor.close()
            uow.commit()
        return operation_id

    def get_operation(
        self,
        context: ProductRequestContext,
        *,
        operation_id: str,
    ) -> InteractionStatusResponse | None:
        with self.uow_factory.begin(_uow_scope(context)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT status, operation_json, outcome_class, updated_at
                    FROM public.sleep_domain_operations
                    WHERE operation_id = %s
                      AND namespace_id = %s AND data_mode = %s
                      AND subject_id = %s AND protocol_version >= 2
                      AND authorization_snapshot_json ->> 'actor_id' = %s
                      AND authorization_snapshot_json ->> 'binding_id' = %s
                    """,
                    (
                        operation_id,
                        context.namespace_id,
                        context.data_mode,
                        context.subject_id,
                        context.actor_id,
                        context.binding_id,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if row is None:
            return None
        payload = row[1]
        if isinstance(payload, str):
            payload = json.loads(payload)
        result = payload.get("result")
        if not isinstance(result, Mapping):
            result = {}
        public_state = result.get("public_state")
        try:
            state = (
                PublicOperationState(str(public_state))
                if public_state is not None
                else _operation_state(str(row[0]))
            )
        except ValueError:
            state = _operation_state(str(row[0]))
        return InteractionStatusResponse(
            data_mode=context.data_mode,
            synthetic_non_release=context.data_mode == "replay",
            operation_id=operation_id,
            interaction_id=payload.get("interaction_id"),
            state=state,
            result_ref=result.get("result_ref"),
            interaction_revision=result.get("interaction_revision"),
            interaction_state=result.get("interaction_state"),
            answer_handle=result.get("answer_handle"),
            confirmation_handle=result.get("confirmation_handle"),
            human_decision_id=result.get("human_decision_id"),
            care_action_id=result.get("care_action_id"),
            delivery_intent_id=result.get("delivery_intent_id"),
            product_operation_id=result.get("product_operation_id"),
            error_code=None if row[2] is None else str(row[2]),
            retryable=str(row[0]) == "retry",
            updated_at=row[3],
        )

    def _existing_receipt(
        self,
        cursor: Any,
        *,
        context: ProductRequestContext,
        route_template: str,
        idempotency_key: str,
    ) -> Any:
        cursor.execute(
            """
            SELECT operation_id, request_sha256,
                   namespace_id, data_mode, subject_id,
                   authorization_snapshot_json
            FROM public.backend_command_receipts
            WHERE service_principal_id = %s AND actor_id = %s
              AND route_template = %s AND caller_idempotency_key = %s
            """,
            (
                context.service_principal_id,
                context.actor_id,
                route_template,
                idempotency_key,
            ),
        )
        return cursor.fetchone()

    def _read_cursor(
        self,
        context: ProductRequestContext,
        *,
        kind: str,
        cursor: str | None,
    ) -> tuple[datetime | None, str | None]:
        if cursor is None:
            return None, None
        value = self.cursor_codec.decode(cursor)
        if value.get("authority") != self._cursor_authority(context, kind=kind):
            raise ProductApiError(
                "cursor_resync_required",
                "Authority changed; restart Product pagination.",
                status_code=409,
            )
        if value.get("kind") != kind:
            raise ProductApiError(
                "invalid_cursor",
                "The Product cursor is invalid or stale.",
                status_code=400,
            )
        try:
            after_time = datetime.fromisoformat(str(value["sort_at"]))
            after_id = str(value["item_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductApiError(
                "invalid_cursor",
                "The Product cursor is invalid or stale.",
                status_code=400,
            ) from exc
        if (
            after_time.tzinfo is None
            or after_time.utcoffset() is None
            or not after_id
        ):
            raise ProductApiError(
                "invalid_cursor",
                "The Product cursor is invalid or stale.",
                status_code=400,
            )
        return after_time, after_id

    def _next_read_cursor(
        self,
        context: ProductRequestContext,
        *,
        kind: str,
        rows: list[Any],
        visible: list[Any],
        limit: int,
        time_index: int,
        id_index: int,
    ) -> str | None:
        if len(rows) <= limit or not visible:
            return None
        last = visible[-1]
        return self.cursor_codec.encode(
            {
                "kind": kind,
                "authority": self._cursor_authority(context, kind=kind),
                "sort_at": last[time_index].isoformat(),
                "item_id": str(last[id_index]),
            }
        )

    def _cursor_authority(self, context: ProductRequestContext, *, kind: str) -> str:
        return _sha256(
            {
                "kind": kind,
                "binding_id": context.binding_id,
                "role": context.role.value,
                "authorization_epoch": context.authorization_epoch,
                "privacy_epoch": context.privacy_epoch,
                "retrieval_epoch": context.retrieval_epoch,
                "policy_sha256": context.policy_sha256,
            }
        )

def build_product_authenticator(
    settings: SleepBackendSettings,
    uow_factory: UnitOfWorkFactory[Any],
) -> SleepApiAuthenticator:
    provider = BackendKeyProvider(settings.deployment_mode)
    service_secret = base64.urlsafe_b64encode(
        provider.secret(
            settings.service_credential_ref,
            purpose="service credential",
            minimum_bytes=32,
        )
    ).decode("ascii")
    actor_key = provider.actor_verification_key(
        settings.signing_key_ref,
        key_id=settings.actor_assertion_key_id,
    )
    credential = RotatingServiceCredential.from_secret(
        credential_id="backend-primary",
        principal_id=settings.service_principal_id,
        secret=service_secret,
        not_before=datetime(2000, 1, 1, tzinfo=UTC),
        not_after=datetime(2100, 1, 1, tzinfo=UTC),
        allowed_actor_issuers=frozenset({settings.actor_assertion_issuer}),
    )
    verification_key = ActorVerificationKey(
        issuer=settings.actor_assertion_issuer,
        key_id=settings.actor_assertion_key_id,
        algorithm="EdDSA",
        public_key_pem=actor_key.public_key_pem,
        not_before=datetime(2000, 1, 1, tzinfo=UTC),
        not_after=datetime(2100, 1, 1, tzinfo=UTC),
    )
    return SleepApiAuthenticator(
        service_verifier=HttpsBearerServicePrincipalVerifier(
            (credential,),
            require_https=settings.deployment_mode == DeploymentMode.PRODUCTION,
        ),
        actor_verifier=ActorAssertionVerifier(
            (verification_key,),
            audience=settings.actor_assertion_audience,
            replay_store=PostgresAssertionReplayStore(settings, uow_factory),
        ),
        role_binding_resolver=FailClosedRoleBindingResolver(),
    )


def _command_queue(command_type: str) -> str:
    if command_type.startswith("interaction."):
        return "product_interaction"
    if command_type.startswith("sleep_api."):
        return "sleep_command"
    raise ProductApiError(
        "invalid_request",
        "The command type has no durable handler.",
        status_code=400,
    )


def _uow_scope(context: ProductRequestContext) -> UowScope:
    return UowScope(
        namespace_id=context.namespace_id,
        namespace_generation=context.namespace_generation,
        data_mode=context.data_mode,
        run_id=context.run_id,
        arm_id=context.arm_id,
        process_role="api",
        purpose=context.purpose,
        service_principal_id=context.service_principal_id,
        subject_id=context.subject_id,
        actor_id=context.actor_id,
        actor_role=context.role.value,
        authorization_epoch=context.authorization_epoch,
        privacy_epoch=context.privacy_epoch,
        retrieval_policy_epoch=context.retrieval_epoch,
    )


def _product_scope_params(context: ProductRequestContext) -> tuple[Any, ...]:
    return (
        context.namespace_id,
        context.data_mode,
        context.namespace_generation,
        context.run_id,
        context.arm_id,
        context.subject_id,
    )


def _authorization_snapshot(context: ProductRequestContext) -> dict[str, Any]:
    return {
        "schema_version": "authorization_snapshot.v1",
        "principal_id": context.service_principal_id,
        "actor_id": context.actor_id,
        "binding_id": context.binding_id,
        "subject_id": context.subject_id,
        "role": context.role.value,
        "effective_scopes": sorted(context.effective_scopes),
        "namespace_id": context.namespace_id,
        "namespace_generation": context.namespace_generation,
        "data_mode": context.data_mode,
        "run_id": context.run_id,
        "arm_id": context.arm_id,
        "purpose": context.purpose,
        "authorization_epoch": context.authorization_epoch,
        "privacy_epoch": context.privacy_epoch,
        "retrieval_policy_epoch": context.retrieval_epoch,
        "policy_sha256": context.policy_sha256,
    }


def _operation_state(status: str) -> PublicOperationState:
    return {
        "pending": PublicOperationState.ACCEPTED,
        "retry": PublicOperationState.ACCEPTED,
        "running": PublicOperationState.RUNNING,
        "succeeded": PublicOperationState.SUCCEEDED,
        "failed": PublicOperationState.FAILED,
        "dead_letter": PublicOperationState.FAILED,
        "reconciliation_required": PublicOperationState.RECONCILIATION_REQUIRED,
        "outcome_unknown": PublicOperationState.RECONCILIATION_REQUIRED,
    }.get(status, PublicOperationState.BLOCKED)


def _care_record(row: Any) -> CareActionRecord | CareFollowupRecord:
    record_type = str(row[0])
    if record_type == "care_action":
        return CareActionRecord(
            care_action_id=str(row[1]),
            interaction_id=str(row[2]),
            state=str(row[3]),
            action_kind=str(row[4]),
            source_analysis_revision_id=(
                None if row[5] is None else str(row[5])
            ),
            confirmed_at=row[6],
            updated_at=row[7],
        )
    if record_type == "care_followup":
        return CareFollowupRecord(
            night_episode_id=str(row[8]),
            state=str(row[3]),
            updated_at=row[7],
        )
    raise ProductApiError(
        "projection_corrupt",
        "The care projection contains an unsupported record type.",
        status_code=500,
    )


def _subject_pseudonym(subject_id: str) -> str:
    return hashlib.sha256(f"audit:{subject_id}".encode()).hexdigest()[:24]


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_json(value: Any) -> bytes:
    return _json(value).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


__all__ = [
    "PostgresAssertionReplayStore",
    "PostgresAuthorityStore",
    "PostgresProductBackend",
    "PostgresProductIdentityResolver",
    "ResolvedActorAuthority",
    "build_product_authenticator",
]
