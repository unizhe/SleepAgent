# 本模块负责唯一 ASGI 服务的接口契约或请求编排，不承载领域状态。
"""PostgreSQL adapters for identity, Product API, and durable replay safety."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Literal, Mapping, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import ValidationError
from starlette.requests import Request

from sleepagent.config import BackendKeyProvider
from sleepagent.config import DeploymentMode, SleepBackendSettings
from sleepagent.api.product_contracts import (
    CareActionRecord,
    CareFollowupRecord,
    HabitChangeRequest,
    HabitChangeResponse,
    HabitProfileResponse,
    HabitQuestionSelectionRequest,
    HabitQuestionSelectionResponse,
    InteractionStatusResponse,
    L2ConfirmationRequest,
    L2ConfirmationResponse,
    MemoryChangeRequest,
    MemoryQueryRequest,
    MemoryQueryResponse,
    PendingL2Change,
    ProductCareResponse,
    ProductNarrativeState,
    ProductReportFailureCode,
    ProductReportNarrative,
    ProductReportProjection,
    ProductReportQuality,
    ProductReportState,
    ProductReportTrace,
    ProductRecordsResponse,
    ProductRole,
    ProductSleepReportListItem,
    ProductSleepReportListResponse,
    ProductSleepReportResponse,
    ProductSleepTodayProjection,
    ProductTrendsResponse,
    PublicOperationState,
    SleepRecord,
    TrendPoint,
)
from sleepagent.api.product import (
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
from sleepagent.api.public_auth import (
    ActorAssertionVerifier,
    ActorVerificationKey,
    FailClosedRoleBindingResolver,
    HttpsBearerServicePrincipalVerifier,
    RotatingServiceCredential,
    SleepApiAuthenticator,
    SleepApiSecurityError,
)
from sleepagent.api.public_contracts import PublicErrorCode
from sleepagent.domain.episodes import EpisodeAssignmentBasis, UUID7Generator
from sleepagent.domain.habit import (
    HABIT_CONCEPTS,
    HabitAnswer,
    HabitChange,
    HabitConceptStatus,
    HabitConfirmation,
    HabitEvidence,
    HabitFact,
    HabitDisposition,
    HabitOperation,
    HabitProfileState,
    HabitQuestionState,
    apply_confirmed_habit_change,
    capture_habit_answers,
    propose_habit_change,
    select_habit_questions,
)
from sleepagent.domain.product_data import public_product_subject_ref
from sleepagent.runtime.contracts import (
    AgentId,
    MemoryChangeCandidate,
    SourceScopeKind,
    stable_hash,
)
from sleepagent.runtime.deterministic_model import (
    DETERMINISTIC_REPLAY_MODEL_VERSION,
    DETERMINISTIC_REPLAY_PROVIDER,
    DeterministicReplayStructuredAgentModel,
)
from sleepagent.runtime.governance import (
    CareActionCatalog,
    PRODUCT_SAFETY_POLICY_VERSION,
)
from sleepagent.runtime.memory import (
    GovernedMemoryItemV2,
    GovernedMemoryState,
    MemoryChange,
    MemoryConfirmation,
    MemoryOperation,
    MemoryPurpose,
    MemoryQueryIntent,
    apply_memory_change,
    resolve_memory_query,
    select_memory_slice,
)
from sleepagent.runtime.registry import (
    PromptCompiler,
    SkillRegistry,
    default_agent_profiles,
    default_skill_packages,
    product_agent_manifest,
)
from sleepagent.runtime.provider import (
    OpenAICompatibleStructuredAgentModel,
    openai_compatible_provider_config_from_env,
)
from sleepagent.runtime.reports import (
    ElderMessageAtom,
    ElderNarrative as RuntimeElderNarrative,
    ElderNarrativeState as RuntimeElderNarrativeState,
    ReportRole,
    RoleProjection,
    RoleProjectionState,
    SharedNightAnalysis,
    build_elder_narrative_runtime_manifest,
    build_role_projection_runtime_manifest,
    build_safe_model_pin,
    build_shared_analysis_runtime_manifest,
    build_shared_role_projections,
    elder_presentation_authority_sha256,
    role_projection_identity_sha256,
    validate_elder_communication_draft,
    validate_elder_message_atoms,
)
from sleepagent.runtime.results import PRODUCT_EPISODE_RUNNER_VERSION


UTC = timezone.utc
_REPORT_EVIDENCE_MEMORY_CONCEPT_IDS = (
    "sleep.context.night_routine",
    "sleep.context.environment",
)
_REPORT_CARE_MEMORY_CONCEPT_IDS = (
    "sleep.preference.care_delivery",
    "sleep.preference.communication",
)
_REPORT_MODEL_MODE_ENV = "SLEEPAGENT_PRODUCT_ANALYSIS_MODEL_MODE"
_BACKEND_DEPLOYMENT_MODE_ENV = "SLEEPAGENT_BACKEND_DEPLOYMENT_MODE"


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


@dataclass(frozen=True, slots=True)
class _ResolvedReportSource:
    wake_date: date
    night_episode_id: str
    night_episode_revision_id: str
    quality_assessment_id: str
    current_risk_id: str


@dataclass(frozen=True, slots=True)
class _ReportReadRow:
    wake_date: date
    quality_json: Mapping[str, Any] | None
    risk_json: Mapping[str, Any] | None
    request_status: str | None
    request_json: Mapping[str, Any] | None
    shared_status: str | None
    analysis_json: Mapping[str, Any] | None
    view_status: str | None
    view_json: Mapping[str, Any] | None
    view_fact_snapshot_sha256: str | None
    view_projection_identity_sha256: str | None
    narrative_status: str | None
    narrative_operation_id: str | None
    narrative_operation_json: Mapping[str, Any] | None
    narrative_json: Mapping[str, Any] | None
    has_stale_artifact: bool
    source_revision_valid: bool
    source_date_match_count: int
    shared_failed_attempts: tuple[Mapping[str, Any], ...]
    narrative_failed_attempts: tuple[Mapping[str, Any], ...]
    shared_orphaned_journal_usage: tuple[Mapping[str, Any], ...]
    narrative_orphaned_journal_usage: tuple[Mapping[str, Any], ...]
    current_context_sha256: str
    current_runtime_manifest_sha256: str
    current_projection_manifest_sha256: str
    current_narrative_manifest_sha256: str


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
                # Authority resolution is a pure STABLE SELECT. Let the UoW
                # rollback on exit so read-only Product routes perform no commit.
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
            raise _product_auth_error(exc) from exc
        return self._resolve_authority(identity=identity, purpose=purpose)

    def resolve_read(
        self,
        request: Request,
        *,
        body: bytes,
        purpose: str,
    ) -> ProductRequestContext:
        try:
            identity = self.authenticator.verify_read_identity(
                request,
                body=body,
                required_scopes=frozenset(),
            )
        except SleepApiSecurityError as exc:
            raise _product_auth_error(exc) from exc
        return self._resolve_authority(identity=identity, purpose=purpose)

    def _resolve_authority(
        self,
        *,
        identity: Any,
        purpose: str,
    ) -> ProductRequestContext:
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
        policy_sha256 = _authorization_policy_sha256(
            principal_id=identity.service_principal.principal_id,
            resolved=resolved,
            purpose=purpose,
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
        now_factory: Callable[[], datetime] | None = None,
        report_runtime_manifest_sha256: str | None = None,
        report_narrative_manifest_sha256: str | None = None,
        report_model_mode: Literal["live", "deterministic"] | None = None,
        report_deployment_mode: str | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.cursor_codec = _ProductCursorCodec(cursor_key)
        self.id_generator = id_generator or UUID7Generator()
        self.now_factory = now_factory or (lambda: datetime.now(tz=UTC))
        self.report_runtime_manifest_sha256 = report_runtime_manifest_sha256
        self.report_narrative_manifest_sha256 = (
            report_narrative_manifest_sha256
        )
        configured_model_mode = (
            report_model_mode
            or os.environ.get(_REPORT_MODEL_MODE_ENV, "").strip()
            or None
        )
        if configured_model_mode not in {None, "live", "deterministic"}:
            raise ValueError(
                f"{_REPORT_MODEL_MODE_ENV} must be live or deterministic"
            )
        self.report_model_mode = configured_model_mode
        configured_deployment_mode = (
            report_deployment_mode
            or os.environ.get(_BACKEND_DEPLOYMENT_MODE_ENV, "").strip()
            or None
        )
        if configured_deployment_mode not in {
            None,
            DeploymentMode.TEST.value,
            DeploymentMode.DEVELOPMENT.value,
            DeploymentMode.PRODUCTION.value,
        }:
            raise ValueError(
                f"{_BACKEND_DEPLOYMENT_MODE_ENV} has an invalid value"
            )
        self.report_deployment_mode = configured_deployment_mode
        for label, value in (
            ("report runtime manifest", report_runtime_manifest_sha256),
            ("report narrative manifest", report_narrative_manifest_sha256),
        ):
            if value is not None and (
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{label} identity must be SHA-256")

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

    def get_report(
        self,
        context: ProductRequestContext,
        *,
        wake_date: date,
        trace: bool,
    ) -> ProductSleepReportResponse | None:
        rows = self._read_report_rows(
            context,
            wake_date=wake_date,
            after_date=None,
            limit=2,
        )
        if not rows:
            return None
        if len(rows) != 1:
            raise ProductApiError(
                "report_source_conflict",
                "The wake date does not resolve to one report source.",
                status_code=409,
            )
        return _report_response(context, rows[0], include_trace=trace)

    def list_reports(
        self,
        context: ProductRequestContext,
        *,
        limit: int,
        cursor: str | None,
        trace: bool,
    ) -> ProductSleepReportListResponse:
        after_date = self._read_report_cursor(context, cursor=cursor)
        rows = self._read_report_rows(
            context,
            wake_date=None,
            after_date=after_date,
            limit=limit + 1,
        )
        visible = rows[:limit]
        reports = tuple(
            _report_response(context, row, include_trace=False)
            for row in visible
        )
        items = tuple(
            ProductSleepReportListItem(
                wake_date=report.wake_date,
                state=report.state,
                audience=report.audience,
                quality=report.quality,
                quality_caveat=report.quality_caveat,
                narrative_state=(
                    None if report.narrative is None else report.narrative.state
                ),
                failure_code=report.failure_code,
            )
            for report in reports
        )
        next_cursor = None
        if len(rows) > limit and visible:
            next_cursor = self.cursor_codec.encode(
                {
                    "kind": "reports",
                    "authority": self._cursor_authority(context, kind="reports"),
                    "wake_date": visible[-1].wake_date.isoformat(),
                }
            )
        page_trace = None
        if trace:
            page_trace = _aggregate_report_trace(
                tuple(
                    _report_response(context, row, include_trace=True)
                    for row in visible
                )
            )
        return ProductSleepReportListResponse(
            items=items,
            next_cursor=next_cursor,
            trace=page_trace,
        )

    def _read_report_cursor(
        self,
        context: ProductRequestContext,
        *,
        cursor: str | None,
    ) -> date | None:
        if cursor is None:
            return None
        value = self.cursor_codec.decode(cursor)
        if (
            value.get("kind") != "reports"
            or value.get("authority")
            != self._cursor_authority(context, kind="reports")
        ):
            raise ProductApiError(
                "cursor_resync_required",
                "Authority changed; restart Product report pagination.",
                status_code=409,
            )
        try:
            return date.fromisoformat(str(value["wake_date"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductApiError(
                "invalid_cursor",
                "The Product report cursor is invalid or stale.",
                status_code=400,
            ) from exc

    def _read_report_rows(
        self,
        context: ProductRequestContext,
        *,
        wake_date: date | None,
        after_date: date | None,
        limit: int,
    ) -> list[_ReportReadRow]:
        with self.uow_factory.begin(_uow_scope(context)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT episode.episode_local_date,
                           quality.assessment_json,
                           risk.risk_json,
                           request.status,
                           request.operation_json,
                           shared.status,
                           current_analysis.analysis_json,
                           role_view.status,
                           role_view.view_json,
                           role_view.source_fact_snapshot_sha256,
                           role_view.projection_sha256,
                           narrative.status,
                           narrative.operation_json,
                           narrative.attempt_json,
                           (
                             current_analysis.analysis_json IS NULL
                             AND EXISTS (
                               SELECT 1
                               FROM public.sleep_domain_analysis_revisions AS old
                               WHERE old.namespace_id = episode.namespace_id
                                 AND old.data_mode = episode.data_mode
                                 AND old.subject_id = episode.subject_id
                                 AND old.night_episode_id = episode.night_episode_id
                                 AND old.analysis_json ->> 'schema_version' =
                                     'shared_night_analysis.v1'
                             )
                           ) AS has_stale_artifact,
                           revision.night_episode_revision_id IS NOT NULL
                             AS source_revision_valid,
                           COUNT(*) OVER (
                             PARTITION BY episode.episode_local_date
                           ) AS source_date_match_count,
                           narrative.operation_id AS narrative_operation_id,
                           shared_failure.attempt_jsons,
                           narrative_failure.attempt_jsons,
                           shared_journal_usage.usage_jsons,
                           narrative_journal_usage.usage_jsons
                    FROM public.sleep_domain_night_episodes AS episode
                    LEFT JOIN public.sleep_domain_night_episode_revisions AS revision
                      ON revision.night_episode_revision_id =
                           episode.current_revision_id
                     AND revision.night_episode_id = episode.night_episode_id
                     AND revision.namespace_id = episode.namespace_id
                     AND revision.data_mode = episode.data_mode
                     AND revision.namespace_generation =
                         episode.namespace_generation
                     AND COALESCE(revision.run_id, '') =
                         COALESCE(episode.run_id, '')
                     AND COALESCE(revision.arm_id, '') =
                         COALESCE(episode.arm_id, '')
                     AND revision.subject_id = episode.subject_id
                     AND revision.protocol_version >= 2
                     AND revision.date_state = 'finalized'
                     AND revision.date_conflict = FALSE
                     AND revision.episode_local_date = episode.episode_local_date
                    LEFT JOIN public.sleep_domain_current_quality AS quality
                      ON quality.namespace_id = episode.namespace_id
                     AND quality.data_mode = episode.data_mode
                     AND quality.subject_id = episode.subject_id
                     AND quality.night_episode_id = episode.night_episode_id
                     AND quality.assessment_json #>>
                           '{source_scope,night_episode_revision_id}' =
                           episode.current_revision_id
                    LEFT JOIN public.sleep_domain_current_risk AS risk
                      ON risk.namespace_id = episode.namespace_id
                     AND risk.data_mode = episode.data_mode
                     AND risk.subject_id = episode.subject_id
                     AND risk.night_episode_id = episode.night_episode_id
                     AND risk.risk_json #>>
                           '{source_scope,night_episode_revision_id}' =
                           episode.current_revision_id
                    LEFT JOIN LATERAL (
                      SELECT operation.status, operation.operation_json
                      FROM public.sleep_domain_operations AS operation
                      WHERE operation.namespace_id = episode.namespace_id
                        AND operation.data_mode = episode.data_mode
                        AND operation.namespace_generation =
                            episode.namespace_generation
                        AND COALESCE(operation.run_id, '') =
                            COALESCE(episode.run_id, '')
                        AND COALESCE(operation.arm_id, '') =
                            COALESCE(episode.arm_id, '')
                        AND operation.subject_id = episode.subject_id
                        AND operation.operation_type = 'product.report.run.v1'
                        AND operation.target_resource_id = episode.night_episode_id
                        AND operation.operation_json ->>
                            'night_episode_revision_id' =
                            episode.current_revision_id
                        AND operation.operation_json ->>
                            'quality_assessment_id' = quality.assessment_id
                        AND operation.operation_json ->>
                            'current_risk_id' = risk.current_risk_id
                        AND COALESCE(
                              operation.authorization_snapshot_json,
                              operation.workload_authorization_snapshot_json
                            ) ->>
                            'authorization_epoch' = %s::text
                        AND COALESCE(
                              operation.authorization_snapshot_json,
                              operation.workload_authorization_snapshot_json
                            ) ->>
                            'privacy_epoch' = %s::text
                        AND COALESCE(
                              operation.authorization_snapshot_json,
                              operation.workload_authorization_snapshot_json
                            ) ->>
                            'retrieval_policy_epoch' = %s::text
                      ORDER BY operation.created_at DESC, operation.operation_id DESC
                      LIMIT 1
                    ) AS request ON TRUE
                    LEFT JOIN LATERAL (
                      SELECT shared_operation.status,
                             shared_operation.operation_id,
                             shared_operation.operation_json
                      FROM public.sleep_domain_operations AS shared_operation
                      WHERE shared_operation.operation_id =
                              request.operation_json #>>
                              '{report_result,shared_operation_id}'
                        AND shared_operation.namespace_id = episode.namespace_id
                        AND shared_operation.data_mode = episode.data_mode
                        AND shared_operation.namespace_generation =
                            episode.namespace_generation
                        AND COALESCE(shared_operation.run_id, '') =
                            COALESCE(episode.run_id, '')
                        AND COALESCE(shared_operation.arm_id, '') =
                            COALESCE(episode.arm_id, '')
                        AND shared_operation.subject_id = episode.subject_id
                        AND shared_operation.operation_type =
                            'product.shared_analysis.v1'
                      LIMIT 1
                    ) AS shared ON TRUE
                    LEFT JOIN LATERAL (
                      SELECT jsonb_agg(
                               attempt.attempt_json
                               ORDER BY attempt.attempt_sequence
                             ) AS attempt_jsons
                      FROM public.backend_product_attempts AS attempt
                      WHERE attempt.operation_id = shared.operation_id
                        AND attempt.namespace_id = episode.namespace_id
                        AND attempt.data_mode = episode.data_mode
                        AND attempt.namespace_generation =
                            episode.namespace_generation
                        AND COALESCE(attempt.run_id, '') =
                            COALESCE(episode.run_id, '')
                        AND COALESCE(attempt.arm_id, '') =
                            COALESCE(episode.arm_id, '')
                        AND attempt.subject_id = episode.subject_id
                        AND attempt.night_episode_revision_id =
                            episode.current_revision_id
                        AND attempt.authorization_epoch = %s
                        AND attempt.privacy_epoch = %s
                        AND attempt.retrieval_policy_epoch = %s
                        AND attempt.query_visible = FALSE
                        AND (
                          (
                            attempt.attempt_state IN (
                              'abandoned', 'outcome_unknown'
                            )
                            AND attempt.attempt_json ->> 'schema_version' =
                              'product_provider_failed_attempt.v1'
                            AND attempt.attempt_json ->> 'operation_type' =
                              'product.shared_analysis.v1'
                          )
                          OR (
                            attempt.attempt_state IN (
                              'prepared', 'abandoned', 'outcome_unknown'
                            )
                            AND attempt.attempt_json ->> 'schema_version' =
                              'product_agent_prepared_attempt.v3'
                          )
                        )
                    ) AS shared_failure ON TRUE
                    LEFT JOIN LATERAL (
                      SELECT jsonb_agg(
                               journal.event_json #>
                                 '{response,artifact,provider_usage}'
                               ORDER BY journal.sequence
                             ) AS usage_jsons
                      FROM public.backend_invocations AS invocation
                      JOIN public.backend_invocation_journal AS journal
                        ON journal.invocation_id = invocation.invocation_id
                       AND journal.namespace_id = invocation.namespace_id
                       AND journal.data_mode = invocation.data_mode
                       AND journal.namespace_generation =
                           invocation.namespace_generation
                       AND COALESCE(journal.run_id, '') =
                           COALESCE(invocation.run_id, '')
                       AND COALESCE(journal.arm_id, '') =
                           COALESCE(invocation.arm_id, '')
                       AND journal.subject_id = invocation.subject_id
                       AND journal.to_state = 'response_received'
                      WHERE invocation.operation_id = shared.operation_id
                        AND invocation.namespace_id = episode.namespace_id
                        AND invocation.data_mode = episode.data_mode
                        AND invocation.namespace_generation =
                            episode.namespace_generation
                        AND COALESCE(invocation.run_id, '') =
                            COALESCE(episode.run_id, '')
                        AND COALESCE(invocation.arm_id, '') =
                            COALESCE(episode.arm_id, '')
                        AND invocation.subject_id = episode.subject_id
                        AND invocation.invocation_kind = 'model'
                        AND journal.event_json #>>
                            '{response,schema_version}' =
                            'product_agent_model_response.v1'
                        AND journal.event_json #>>
                            '{response,artifact,schema_version}' =
                            'product_agent_prepared_attempt.v3'
                        AND journal.event_json #>>
                            '{response,artifact,night_episode_revision_id}' =
                            episode.current_revision_id
                        AND jsonb_typeof(
                              journal.event_json #>
                                '{response,artifact,provider_usage}'
                            ) = 'object'
                        AND NOT EXISTS (
                          SELECT 1
                          FROM public.backend_product_attempts AS staged
                          WHERE staged.operation_id = invocation.operation_id
                            AND staged.product_attempt_id =
                                journal.event_json #>>
                                '{response,artifact,product_attempt_id}'
                        )
                    ) AS shared_journal_usage ON TRUE
                    LEFT JOIN LATERAL (
                      SELECT analysis.analysis_revision_id,
                             analysis.analysis_json
                      FROM public.sleep_domain_analysis_revisions AS analysis
                      JOIN public.backend_product_attempts AS attempt
                        ON attempt.night_episode_revision_id =
                             analysis.night_episode_revision_id
                       AND attempt.subject_id = analysis.subject_id
                       AND attempt.namespace_id = analysis.namespace_id
                       AND attempt.data_mode = analysis.data_mode
                       AND attempt.operation_id = shared.operation_id
                       AND attempt.product_attempt_id =
                           shared.operation_json #>>
                           '{result,product_attempt_id}'
                       AND attempt.attempt_state = 'committed'
                       AND attempt.query_visible = TRUE
                       AND attempt.attempt_json ->> 'schema_version' =
                           'product_agent_prepared_attempt.v3'
                       AND attempt.attempt_json #>>
                           '{analysis,analysis_revision_id}' =
                           analysis.analysis_revision_id
                       AND attempt.authorization_epoch = %s
                       AND attempt.privacy_epoch = %s
                       AND attempt.retrieval_policy_epoch = %s
                      WHERE analysis.namespace_id = episode.namespace_id
                        AND analysis.data_mode = episode.data_mode
                        AND analysis.subject_id = episode.subject_id
                        AND analysis.night_episode_id = episode.night_episode_id
                        AND analysis.night_episode_revision_id =
                            episode.current_revision_id
                        AND analysis.analysis_revision_id =
                            shared.operation_json #>>
                            '{result,analysis_revision_id}'
                        AND analysis.analysis_json ->>
                            'desired_analysis_sha256' =
                            request.operation_json #>>
                            '{report_result,desired_analysis_sha256}'
                        AND analysis.analysis_json ->> 'schema_version' =
                            'shared_night_analysis.v1'
                      ORDER BY analysis.revision_number DESC
                      LIMIT 1
                    ) AS current_analysis ON TRUE
                    LEFT JOIN LATERAL (
                      SELECT view.status, view.view_json,
                             view.source_fact_snapshot_sha256,
                             view.projection_sha256
                      FROM public.sleep_domain_analysis_role_views AS view
                      WHERE view.analysis_revision_id =
                              current_analysis.analysis_revision_id
                        AND view.namespace_id = episode.namespace_id
                        AND view.data_mode = episode.data_mode
                        AND view.namespace_generation =
                            episode.namespace_generation
                        AND COALESCE(view.run_id, '') =
                            COALESCE(episode.run_id, '')
                        AND COALESCE(view.arm_id, '') =
                            COALESCE(episode.arm_id, '')
                        AND view.subject_id = episode.subject_id
                        AND view.role = %s
                        AND view.authorization_epoch = %s
                        AND view.privacy_epoch = %s
                        AND view.retrieval_policy_epoch = %s
                        AND view.view_json ->> 'schema_version' =
                            'role_projection.v1'
                      LIMIT 1
                    ) AS role_view ON TRUE
                    LEFT JOIN LATERAL (
                      SELECT operation.operation_id, operation.status,
                             operation.operation_json,
                             attempt.attempt_json
                      FROM public.sleep_domain_operations AS operation
                      LEFT JOIN public.backend_product_attempts AS attempt
                        ON attempt.operation_id = operation.operation_id
                       AND attempt.attempt_state = 'committed'
                       AND attempt.query_visible = TRUE
                      WHERE %s = 'elder'
                        AND operation.namespace_id = episode.namespace_id
                        AND operation.data_mode = episode.data_mode
                        AND operation.namespace_generation =
                            episode.namespace_generation
                        AND COALESCE(operation.run_id, '') =
                            COALESCE(episode.run_id, '')
                        AND COALESCE(operation.arm_id, '') =
                            COALESCE(episode.arm_id, '')
                        AND operation.subject_id = episode.subject_id
                        AND operation.operation_type =
                            'product.elder_narrative.v1'
                        AND operation.target_resource_id =
                            current_analysis.analysis_revision_id
                      ORDER BY operation.created_at DESC, operation.operation_id DESC,
                               attempt.attempt_sequence DESC
                      LIMIT 1
                    ) AS narrative ON TRUE
                    LEFT JOIN LATERAL (
                      SELECT jsonb_agg(
                               attempt.attempt_json
                               ORDER BY attempt.attempt_sequence
                             ) AS attempt_jsons
                      FROM public.backend_product_attempts AS attempt
                      WHERE attempt.operation_id = narrative.operation_id
                        AND attempt.namespace_id = episode.namespace_id
                        AND attempt.data_mode = episode.data_mode
                        AND attempt.namespace_generation =
                            episode.namespace_generation
                        AND COALESCE(attempt.run_id, '') =
                            COALESCE(episode.run_id, '')
                        AND COALESCE(attempt.arm_id, '') =
                            COALESCE(episode.arm_id, '')
                        AND attempt.subject_id = episode.subject_id
                        AND attempt.night_episode_revision_id =
                            episode.current_revision_id
                        AND attempt.authorization_epoch = %s
                        AND attempt.privacy_epoch = %s
                        AND attempt.retrieval_policy_epoch = %s
                        AND attempt.query_visible = FALSE
                        AND (
                          (
                            attempt.attempt_state IN (
                              'abandoned', 'outcome_unknown'
                            )
                            AND attempt.attempt_json ->> 'schema_version' =
                              'product_provider_failed_attempt.v1'
                            AND attempt.attempt_json ->> 'operation_type' =
                              'product.elder_narrative.v1'
                          )
                          OR (
                            attempt.attempt_state IN (
                              'prepared', 'abandoned', 'outcome_unknown'
                            )
                            AND attempt.attempt_json ->> 'schema_version' =
                              'product_elder_narrative_prepared_attempt.v1'
                          )
                        )
                    ) AS narrative_failure ON TRUE
                    LEFT JOIN LATERAL (
                      SELECT jsonb_agg(
                               journal.event_json #>
                                 '{response,artifact,provider_usage}'
                               ORDER BY journal.sequence
                             ) AS usage_jsons
                      FROM public.backend_invocations AS invocation
                      JOIN public.backend_invocation_journal AS journal
                        ON journal.invocation_id = invocation.invocation_id
                       AND journal.namespace_id = invocation.namespace_id
                       AND journal.data_mode = invocation.data_mode
                       AND journal.namespace_generation =
                           invocation.namespace_generation
                       AND COALESCE(journal.run_id, '') =
                           COALESCE(invocation.run_id, '')
                       AND COALESCE(journal.arm_id, '') =
                           COALESCE(invocation.arm_id, '')
                       AND journal.subject_id = invocation.subject_id
                       AND journal.to_state = 'response_received'
                      WHERE invocation.operation_id = narrative.operation_id
                        AND invocation.namespace_id = episode.namespace_id
                        AND invocation.data_mode = episode.data_mode
                        AND invocation.namespace_generation =
                            episode.namespace_generation
                        AND COALESCE(invocation.run_id, '') =
                            COALESCE(episode.run_id, '')
                        AND COALESCE(invocation.arm_id, '') =
                            COALESCE(episode.arm_id, '')
                        AND invocation.subject_id = episode.subject_id
                        AND invocation.invocation_kind = 'model'
                        AND journal.event_json #>>
                            '{response,schema_version}' =
                            'product_elder_narrative_response.v1'
                        AND journal.event_json #>>
                            '{response,artifact,schema_version}' =
                            'product_elder_narrative_prepared_attempt.v1'
                        AND journal.event_json #>>
                            '{response,artifact,night_episode_revision_id}' =
                            episode.current_revision_id
                        AND jsonb_typeof(
                              journal.event_json #>
                                '{response,artifact,provider_usage}'
                            ) = 'object'
                        AND NOT EXISTS (
                          SELECT 1
                          FROM public.backend_product_attempts AS staged
                          WHERE staged.operation_id = invocation.operation_id
                            AND staged.product_attempt_id =
                                journal.event_json #>>
                                '{response,artifact,product_attempt_id}'
                        )
                    ) AS narrative_journal_usage ON TRUE
                    WHERE episode.namespace_id = %s AND episode.data_mode = %s
                      AND episode.namespace_generation = %s
                      AND COALESCE(episode.run_id, '') = COALESCE(%s, '')
                      AND COALESCE(episode.arm_id, '') = COALESCE(%s, '')
                      AND episode.subject_id = %s
                      AND episode.protocol_version >= 2
                      AND episode.date_state = 'finalized'
                      AND episode.date_conflict = FALSE
                      AND (%s::date IS NULL OR episode.episode_local_date = %s)
                      AND (%s::date IS NULL OR episode.episode_local_date < %s)
                    ORDER BY episode.episode_local_date DESC
                    LIMIT %s
                    """,
                    (
                        context.authorization_epoch,
                        context.privacy_epoch,
                        context.retrieval_epoch,
                        context.authorization_epoch,
                        context.privacy_epoch,
                        context.retrieval_epoch,
                        context.authorization_epoch,
                        context.privacy_epoch,
                        context.retrieval_epoch,
                        context.role.value,
                        context.authorization_epoch,
                        context.privacy_epoch,
                        context.retrieval_epoch,
                        context.role.value,
                        context.authorization_epoch,
                        context.privacy_epoch,
                        context.retrieval_epoch,
                        *_product_scope_params(context),
                        wake_date,
                        wake_date,
                        after_date,
                        after_date,
                        limit,
                    ),
                )
                rows = cursor.fetchall()
                as_of = self.now_factory()
                if as_of.tzinfo is None or as_of.utcoffset() is None:
                    raise RuntimeError("report context clock must be timezone-aware")
                current_context_sha256 = _current_report_context_sha256(
                    cursor,
                    context,
                    as_of=as_of,
                )
                report_model_mode = _resolved_report_model_mode(
                    context,
                    configured=self.report_model_mode,
                )
                current_runtime_manifest_sha256 = (
                    self.report_runtime_manifest_sha256
                    or _default_shared_runtime_manifest_sha256(
                        context,
                        model_mode=report_model_mode,
                        deployment_mode=self.report_deployment_mode,
                    )
                )
                current_projection_manifest_sha256 = stable_hash(
                    build_role_projection_runtime_manifest()
                )
                current_narrative_manifest_sha256 = (
                    self.report_narrative_manifest_sha256
                    or _default_elder_narrative_manifest_sha256(
                        context,
                        model_mode=report_model_mode,
                        deployment_mode=self.report_deployment_mode,
                    )
                )
            finally:
                cursor.close()
            # No commit: __exit__ rolls back this SELECT-only transaction.
        report_rows = [
            _report_read_row(
                row,
                current_context_sha256=current_context_sha256,
                current_runtime_manifest_sha256=current_runtime_manifest_sha256,
                current_projection_manifest_sha256=(
                    current_projection_manifest_sha256
                ),
                current_narrative_manifest_sha256=(
                    current_narrative_manifest_sha256
                ),
            )
            for row in rows
        ]
        if any(
            not row.source_revision_valid
            or row.source_date_match_count != 1
            for row in report_rows
        ):
            raise ProductApiError(
                "report_source_conflict",
                "The wake date does not resolve to one valid current revision.",
                status_code=409,
            )
        return report_rows

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
                projection_state=cast(
                    Literal["ready", "degraded", "blocked"], str(row[6])
                ),
                sleep_window_minutes=None if row[7] is None else int(row[7]),
                committed_at=row[8],
            )
            for row in visible
        )
        return ProductTrendsResponse(
            data_mode=cast(Literal["live", "replay"], context.data_mode),
            synthetic_non_release=context.data_mode == "replay",
            subject_ref=public_product_subject_ref(context.subject_id),
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
                projection_state=cast(
                    Literal["ready", "degraded", "blocked"], str(row[7])
                ),
                is_current=bool(row[8]),
                committed_at=row[9],
            )
            for row in visible
        )
        return ProductRecordsResponse(
            data_mode=cast(Literal["live", "replay"], context.data_mode),
            synthetic_non_release=context.data_mode == "replay",
            subject_ref=public_product_subject_ref(context.subject_id),
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
            data_mode=cast(Literal["live", "replay"], context.data_mode),
            synthetic_non_release=context.data_mode == "replay",
            subject_ref=public_product_subject_ref(context.subject_id),
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

    def select_habit_questions(
        self,
        context: ProductRequestContext,
        request: HabitQuestionSelectionRequest,
    ) -> HabitQuestionSelectionResponse:
        now = self.now_factory()
        with self.uow_factory.begin(_uow_scope(context)) as uow:
            cursor = uow.connection.cursor()
            try:
                _lock_l2_subject(cursor, context, capability="habit")
                profile = _load_habit_profile(cursor, context)
                suppressed, cooldown = _habit_question_history(cursor, context)
                current = profile.current(now)
                disputed = set(profile.disputed_concept_ids(now))
                stale = set(profile.stale_concept_ids(now))
                known = {item.concept_id for item in current}.difference(disputed)
                concept_states = {
                    concept_id: (
                        HabitConceptStatus.DISPUTED
                        if concept_id in disputed
                        else HabitConceptStatus.STALE
                        if concept_id in stale
                        else HabitConceptStatus.KNOWN
                        if concept_id in known
                        else HabitConceptStatus.UNKNOWN
                    )
                    for concept_id in HABIT_CONCEPTS
                }
                selected = select_habit_questions(
                    HabitQuestionState(
                        episode_id=request.episode_id,
                        subject_id=context.subject_id,
                        actor_id=context.actor_id,
                        role=cast(Literal["elder", "family"], context.role.value),
                        concept_states=concept_states,
                        candidate_concept_ids=request.candidate_concept_ids,
                        suppressed_concept_ids=suppressed,
                        cooldown_until=cooldown,
                        remaining_episode_budget=request.remaining_episode_budget,
                    ),
                    now=now,
                )
                cursor.execute(
                    """
                    INSERT INTO public.backend_habit_question_selections_v2 (
                      selection_id, namespace_id, data_mode,
                      namespace_generation, run_id, arm_id, subject_id,
                      actor_id, role, selection_sha256, selection_json,
                      created_at, expires_at
                    ) VALUES (
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s::jsonb, %s, %s
                    )
                    """,
                    (
                        selected.selection_id,
                        context.namespace_id,
                        context.data_mode,
                        context.namespace_generation,
                        context.run_id,
                        context.arm_id,
                        context.subject_id,
                        context.actor_id,
                        context.role.value,
                        selected.receipt_hash,
                        selected.model_dump_json(),
                        now,
                        selected.expires_at,
                    ),
                )
            finally:
                cursor.close()
            uow.commit()
        questions = tuple(
            {
                "concept_id": concept_id,
                "concept_version": concept_version,
                "question": (
                    HABIT_CONCEPTS[concept_id].observer_question
                    if context.role == ProductRole.FAMILY
                    else HABIT_CONCEPTS[concept_id].question
                ),
                "answer_type": HABIT_CONCEPTS[concept_id].answer_type.value,
                "options": HABIT_CONCEPTS[concept_id].options,
                "unit": HABIT_CONCEPTS[concept_id].unit,
                "minimum": HABIT_CONCEPTS[concept_id].minimum,
                "maximum": HABIT_CONCEPTS[concept_id].maximum,
            }
            for concept_id, concept_version in selected.selected_concepts
        )
        return HabitQuestionSelectionResponse(
            selection=selected.model_dump(mode="json"),
            questions=questions,
            profile_version=profile.version,
        )

    def propose_habit_changes(
        self,
        context: ProductRequestContext,
        request: HabitChangeRequest,
    ) -> HabitChangeResponse:
        now = self.now_factory()
        pending: list[PendingL2Change] = []
        evidence_values: tuple[HabitEvidence, ...] = ()
        with self.uow_factory.begin(_uow_scope(context)) as uow:
            cursor = uow.connection.cursor()
            try:
                _lock_l2_subject(cursor, context, capability="habit")
                profile = _load_habit_profile(cursor, context)
                if request.answers:
                    selection = _load_habit_selection_for_update(
                        cursor,
                        context,
                        str(request.selection_id),
                        now=now,
                    )
                    answers = tuple(
                        HabitAnswer.model_validate(item.answer)
                        for item in request.answers
                    )
                    evidence_values = capture_habit_answers(
                        selection,
                        answers,
                        episode_id=selection.episode_id,
                        subject_id=context.subject_id,
                        actor_id=context.actor_id,
                        role=cast(Literal["elder", "family"], context.role.value),
                        now=now,
                    )
                    cursor.execute(
                        """
                        UPDATE public.backend_habit_question_selections_v2
                        SET evidence_json = %s::jsonb, consumed_at = %s
                        WHERE selection_id = %s AND consumed_at IS NULL
                        """,
                        (
                            _json(
                                [item.model_dump(mode="json") for item in evidence_values]
                            ),
                            now,
                            selection.selection_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ProductApiError(
                            "state_conflict",
                            "Habit selection was already consumed.",
                            status_code=409,
                        )
                    command_by_concept = {
                        HabitAnswer.model_validate(item.answer).concept_id: item
                        for item in request.answers
                    }
                    changes = []
                    for evidence in evidence_values:
                        if not evidence.profile_eligible:
                            continue
                        command = command_by_concept[evidence.concept_id]
                        changes.append(
                            propose_habit_change(
                                operation=HabitOperation(command.operation),
                                subject_id=context.subject_id,
                                concept_id=evidence.concept_id,
                                expected_profile_version=profile.version,
                                confirmation_actor_id=request.confirmation_actor_id,
                                now=now,
                                evidence=evidence,
                                target_fact_id=command.target_fact_id,
                            )
                        )
                else:
                    changes = [
                        propose_habit_change(
                            operation=HabitOperation(str(request.operation)),
                            subject_id=context.subject_id,
                            concept_id=str(request.concept_id),
                            expected_profile_version=profile.version,
                            confirmation_actor_id=request.confirmation_actor_id,
                            now=now,
                            target_fact_id=request.target_fact_id,
                        )
                    ]
                for change in changes:
                    pending.append(
                        _insert_l2_pending_handle(
                            cursor,
                            context,
                            capability="habit",
                            change_id=change.change_id,
                            change_hash=change.change_hash,
                            change_json=change.model_dump(mode="json"),
                            confirmation_actor_id=change.confirmation_actor_id,
                            expected_state_version=change.expected_profile_version,
                            expires_at=change.confirmation_expires_at,
                        )
                    )
            finally:
                cursor.close()
            uow.commit()
        return HabitChangeResponse(
            evidence=tuple(item.model_dump(mode="json") for item in evidence_values),
            pending_changes=tuple(pending),
        )

    def confirm_l2_change(
        self,
        context: ProductRequestContext,
        request: L2ConfirmationRequest,
        *,
        capability: Literal["habit", "memory"],
    ) -> L2ConfirmationResponse:
        now = self.now_factory()
        with self.uow_factory.begin(_uow_scope(context)) as uow:
            cursor = uow.connection.cursor()
            try:
                _lock_l2_subject(cursor, context, capability=capability)
                handle_id, handle_payload = _consume_l2_pending_handle(
                    cursor,
                    context,
                    request,
                    capability=capability,
                    now=now,
                )
                confirmation_id = "l2-confirmation:" + str(self.id_generator(now))
                if capability == "habit":
                    habit_change = HabitChange.model_validate(
                        handle_payload["change"]
                    )
                    profile = _load_habit_profile(cursor, context)
                    try:
                        updated = apply_confirmed_habit_change(
                            profile,
                            habit_change,
                            HabitConfirmation(
                                confirmation_id=confirmation_id,
                                actor_id=context.actor_id,
                                actor_role="elder",
                                subject_id=context.subject_id,
                                target_change_id=habit_change.change_id,
                                target_hash=habit_change.change_hash,
                                approved_at=now,
                                expires_at=habit_change.confirmation_expires_at,
                            ),
                            now=now,
                        )
                    except (PermissionError, ValueError) as exc:
                        if "revision hash mismatch" in str(exc):
                            raise RuntimeError(
                                "Habit Profile revision integrity check failed"
                            ) from exc
                        raise ProductApiError(
                            "state_conflict",
                            "The Habit change is stale or no longer applicable.",
                            status_code=409,
                        ) from exc
                    revision = updated.revisions[-1]
                    cursor.execute(
                        """
                        INSERT INTO public.backend_habit_profile_revisions_v2 (
                          fact_id, namespace_id, data_mode,
                          namespace_generation, run_id, arm_id, subject_id,
                          profile_version, concept_id, concept_version,
                          operation, fact_sha256, fact_json,
                          confirmation_ref, committed_at
                        ) VALUES (
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s::jsonb, %s, %s
                        )
                        """,
                        (
                            revision.fact_id,
                            context.namespace_id,
                            context.data_mode,
                            context.namespace_generation,
                            context.run_id,
                            context.arm_id,
                            context.subject_id,
                            updated.version,
                            revision.concept_id,
                            revision.concept_version,
                            revision.operation.value,
                            revision.fact_hash,
                            revision.model_dump_json(),
                            confirmation_id,
                            now,
                        ),
                    )
                    revision_ref = revision.fact_id
                    revision_hash = revision.fact_hash
                    state_version = updated.version
                else:
                    memory_change = MemoryChange.model_validate(
                        handle_payload["change"]
                    )
                    state = _load_memory_state(cursor, context)
                    try:
                        updated_memory = apply_memory_change(
                            state,
                            memory_change,
                            MemoryConfirmation(
                                confirmation_id=confirmation_id,
                                actor_id=context.actor_id,
                                subject_id=context.subject_id,
                                target_change_id=memory_change.change_id,
                                target_change_hash=str(memory_change.change_hash),
                                approved_at=now,
                                expires_at=memory_change.confirmation_expires_at,
                            ),
                            now=now,
                        )
                    except (PermissionError, ValueError) as exc:
                        if "forged" in str(exc):
                            raise RuntimeError(
                                "Governed Memory revision integrity check failed"
                            ) from exc
                        raise ProductApiError(
                            "state_conflict",
                            "The Memory change is stale or no longer applicable.",
                            status_code=409,
                        ) from exc
                    memory_revision = cast(
                        GovernedMemoryItemV2,
                        updated_memory.revisions[-1],
                    )
                    revision_hash = stable_hash(memory_revision)
                    cursor.execute(
                        """
                        INSERT INTO public.backend_governed_memory_revisions_v2 (
                          revision_ref, namespace_id, data_mode,
                          namespace_generation, run_id, arm_id, subject_id,
                          state_version, memory_id, memory_version, concept_id,
                          status, revision_sha256, revision_json,
                          confirmation_ref, committed_at
                        ) VALUES (
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s::jsonb, %s, %s
                        )
                        """,
                        (
                            memory_revision.revision_ref,
                            context.namespace_id,
                            context.data_mode,
                            context.namespace_generation,
                            context.run_id,
                            context.arm_id,
                            context.subject_id,
                            updated_memory.version,
                            memory_revision.memory_id,
                            memory_revision.version,
                            memory_revision.concept_id,
                            memory_revision.status.value,
                            revision_hash,
                            memory_revision.model_dump_json(),
                            confirmation_id,
                            now,
                        ),
                    )
                    revision_ref = memory_revision.revision_ref
                    state_version = updated_memory.version
                cursor.execute(
                    """
                    UPDATE public.backend_pending_handles
                    SET status = 'consumed', consumed_at = %s,
                        consumed_by_command_receipt_id = %s,
                        cas_version = cas_version + 1
                    WHERE handle_id = %s AND status = 'pending'
                    """,
                    (now, confirmation_id, handle_id),
                )
                if cursor.rowcount != 1:
                    raise ProductApiError(
                        "state_conflict",
                        "L2 confirmation handle changed concurrently.",
                        status_code=409,
                    )
            finally:
                cursor.close()
            uow.commit()
        return L2ConfirmationResponse(
            capability=capability,
            state_version=state_version,
            revision_ref=revision_ref,
            revision_hash=revision_hash,
        )

    def get_habit_profile(
        self,
        context: ProductRequestContext,
    ) -> HabitProfileResponse:
        now = self.now_factory()
        with self.uow_factory.begin(_uow_scope(context)) as uow:
            cursor = uow.connection.cursor()
            try:
                profile = _load_habit_profile(cursor, context)
            finally:
                cursor.close()
            uow.commit()
        current = profile.current(now)
        profile_hash = (
            None
            if profile.version == 0
            else stable_hash(
                {
                    "subject_id": context.subject_id,
                    "profile_version": profile.version,
                    "fact_hashes": [item.fact_hash for item in current],
                }
            )
        )
        return HabitProfileResponse(
            profile_version=profile.version,
            profile_hash=profile_hash,
            current_facts=tuple(item.model_dump(mode="json") for item in current),
            stale_concept_ids=profile.stale_concept_ids(now),
            disputed_concept_ids=profile.disputed_concept_ids(now),
        )

    def propose_memory_change(
        self,
        context: ProductRequestContext,
        request: MemoryChangeRequest,
    ) -> PendingL2Change:
        now = self.now_factory()
        with self.uow_factory.begin(_uow_scope(context)) as uow:
            cursor = uow.connection.cursor()
            try:
                _lock_l2_subject(cursor, context, capability="memory")
                state = _load_memory_state(cursor, context)
                operation = MemoryOperation(request.operation)
                candidate = None
                source_ref = "user_report:" + stable_hash(
                    {
                        "actor_id": context.actor_id,
                        "subject_id": context.subject_id,
                        "source_text": request.source_text,
                        "at": now,
                    }
                )[:32]
                if operation in {MemoryOperation.REMEMBER, MemoryOperation.CORRECT}:
                    assert request.memory_type is not None
                    assert request.concept_id is not None
                    assert request.value_schema_id is not None
                    assert request.sensitivity_class is not None
                    candidate = MemoryChangeCandidate(
                        candidate_id=request.memory_id,
                        operation=(
                            "create"
                            if operation == MemoryOperation.REMEMBER
                            else "replace"
                        ),
                        subject_id=context.subject_id,
                        memory_type=request.memory_type,
                        concept_id=request.concept_id,
                        value_schema_id=request.value_schema_id,
                        typed_value=request.typed_value,
                        provenance_type="elder_confirmed",
                        source_ref=source_ref,
                        sensitivity_class=request.sensitivity_class,
                        allowed_roles=tuple(
                            AgentId(value) for value in request.allowed_roles
                        ),
                        allowed_purposes=request.allowed_purposes,
                        valid_until=request.valid_until,
                        explicit_user_authorization=True,
                        confirmation_required=True,
                    )
                change = MemoryChange(
                    change_id="memory-change:" + str(self.id_generator(now)),
                    operation=operation,
                    memory_id=request.memory_id,
                    subject_id=context.subject_id,
                    expected_state_version=state.version,
                    proposed_value=candidate,
                    causal_ref=source_ref,
                    source_actor_id=context.actor_id,
                    source_actor_role="elder",
                    source_scope_kind=SourceScopeKind(request.source_scope_kind),
                    target_revision_ref=request.target_revision_ref,
                    target_revision_hash=request.target_revision_hash,
                    confirmation_actor_id=context.actor_id,
                    created_at=now,
                    confirmation_expires_at=now + timedelta(minutes=30),
                )
                pending = _insert_l2_pending_handle(
                    cursor,
                    context,
                    capability="memory",
                    change_id=change.change_id,
                    change_hash=str(change.change_hash),
                    change_json=change.model_dump(mode="json"),
                    confirmation_actor_id=context.actor_id,
                    expected_state_version=state.version,
                    expires_at=change.confirmation_expires_at,
                )
            finally:
                cursor.close()
            uow.commit()
        return pending

    def query_memory(
        self,
        context: ProductRequestContext,
        request: MemoryQueryRequest,
    ) -> MemoryQueryResponse:
        now = self.now_factory()
        with self.uow_factory.begin(_uow_scope(context)) as uow:
            cursor = uow.connection.cursor()
            try:
                state = _load_memory_state(cursor, context)
                query = resolve_memory_query(
                    MemoryQueryIntent(
                        purpose=MemoryPurpose.EXPLICIT_MEMORY_REVIEW,
                        concept_ids=request.concept_ids,
                        source_scope_kind=SourceScopeKind(
                            request.source_scope_kind
                        ),
                        max_items=request.max_items,
                        token_budget=request.token_budget,
                    ),
                    invocation_id="memory-review:" + str(self.id_generator(now)),
                    actor_id=context.actor_id,
                    actor_role="elder",
                    subject_id=context.subject_id,
                    requesting_agent=AgentId.SLEEP_CARE,
                    authorization_scope=("memory:read",),
                    as_of=now,
                    privacy_epoch=context.privacy_epoch,
                    authorization_epoch=context.authorization_epoch,
                )
                receipt = select_memory_slice(query, state, now=now)
                _insert_memory_read_receipt(
                    cursor,
                    context,
                    receipt,
                    product_episode_id=None,
                )
            finally:
                cursor.close()
            uow.commit()
        return MemoryQueryResponse(receipt=receipt.model_dump(mode="json"))

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
        return self._reserve_command(
            context,
            route_template=route_template,
            command_type=command_type,
            idempotency_key=idempotency_key,
            body_sha256=body_sha256,
            payload=payload,
            target_id=target_id,
            report_wake_date=None,
        )

    def reserve_report_run(
        self,
        context: ProductRequestContext,
        *,
        wake_date: date,
        idempotency_key: str,
        body_sha256: str,
    ) -> str:
        return self._reserve_command(
            context,
            route_template="/product/sleep/reports/run",
            command_type="product.report.run.v1",
            idempotency_key=idempotency_key,
            body_sha256=body_sha256,
            payload={
                "schema_version": "product_sleep_report_run.v1",
                "wake_date": wake_date.isoformat(),
            },
            target_id=None,
            report_wake_date=wake_date,
        )

    def _reserve_command(
        self,
        context: ProductRequestContext,
        *,
        route_template: str,
        command_type: str,
        idempotency_key: str,
        body_sha256: str,
        payload: Mapping[str, Any],
        target_id: str | None,
        report_wake_date: date | None,
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
                report_source = None
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
                if report_wake_date is not None:
                    report_source = self._resolve_report_source_for_reservation(
                        cursor,
                        context=context,
                        wake_date=report_wake_date,
                    )
                    target_id = report_source.night_episode_id
                receipt_id = str(self.id_generator())
                snapshot = _authorization_snapshot(context)
                semantic_material = {
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
                if report_source is not None:
                    semantic_material.update(
                        {
                            "caller_idempotency_key": idempotency_key,
                            "night_episode_revision_id": (
                                report_source.night_episode_revision_id
                            ),
                            "quality_assessment_id": (
                                report_source.quality_assessment_id
                            ),
                            "current_risk_id": report_source.current_risk_id,
                        }
                    )
                semantic_key = _sha256(semantic_material)
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
                target_resource_key = target_id or ""
                if report_source is not None:
                    operation_json.update(
                        {
                            "wake_date": report_source.wake_date.isoformat(),
                            "night_episode_id": report_source.night_episode_id,
                            "night_episode_revision_id": (
                                report_source.night_episode_revision_id
                            ),
                            "quality_assessment_id": (
                                report_source.quality_assessment_id
                            ),
                            "current_risk_id": report_source.current_risk_id,
                        }
                    )
                    target_resource_key = report_source.wake_date.isoformat()
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
                            target_resource_key,
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

    def _resolve_report_source_for_reservation(
        self,
        cursor: Any,
        *,
        context: ProductRequestContext,
        wake_date: date,
    ) -> _ResolvedReportSource:
        cursor.execute(
            """
            SELECT episode.night_episode_id,
                   revision.night_episode_revision_id,
                   quality.assessment_id,
                   risk.current_risk_id
            FROM public.sleep_domain_night_episodes AS episode
            LEFT JOIN public.sleep_domain_night_episode_revisions AS revision
              ON revision.night_episode_revision_id = episode.current_revision_id
             AND revision.night_episode_id = episode.night_episode_id
             AND revision.namespace_id = episode.namespace_id
             AND revision.data_mode = episode.data_mode
             AND revision.namespace_generation = episode.namespace_generation
             AND COALESCE(revision.run_id, '') = COALESCE(episode.run_id, '')
             AND COALESCE(revision.arm_id, '') = COALESCE(episode.arm_id, '')
             AND revision.subject_id = episode.subject_id
             AND revision.protocol_version >= 2
             AND revision.date_state = 'finalized'
             AND revision.date_conflict = FALSE
             AND revision.episode_local_date = episode.episode_local_date
            LEFT JOIN public.sleep_domain_current_quality AS quality
              ON quality.namespace_id = episode.namespace_id
             AND quality.data_mode = episode.data_mode
             AND quality.subject_id = episode.subject_id
             AND quality.night_episode_id = episode.night_episode_id
             AND quality.assessment_json #>>
                   '{source_scope,night_episode_revision_id}' =
                   episode.current_revision_id
            LEFT JOIN public.sleep_domain_current_risk AS risk
              ON risk.namespace_id = episode.namespace_id
             AND risk.data_mode = episode.data_mode
             AND risk.subject_id = episode.subject_id
             AND risk.night_episode_id = episode.night_episode_id
             AND risk.risk_json #>>
                   '{source_scope,night_episode_revision_id}' =
                   episode.current_revision_id
            WHERE episode.namespace_id = %s AND episode.data_mode = %s
              AND episode.namespace_generation = %s
              AND COALESCE(episode.run_id, '') = COALESCE(%s, '')
              AND COALESCE(episode.arm_id, '') = COALESCE(%s, '')
              AND episode.subject_id = %s
              AND episode.protocol_version >= 2
              AND episode.episode_local_date = %s
              AND episode.date_state = 'finalized'
              AND episode.date_conflict = FALSE
            FOR UPDATE OF episode
            """,
            (*_product_scope_params(context), wake_date),
        )
        rows = cursor.fetchall()
        if not rows:
            raise ProductApiError(
                "not_found",
                "A finalized report night was not found for that wake date.",
                status_code=404,
            )
        if len(rows) != 1:
            raise ProductApiError(
                "report_source_conflict",
                "The wake date does not resolve to one report source.",
                status_code=409,
            )
        row = rows[0]
        if any(value is None for value in row[1:]):
            raise ProductApiError(
                "report_source_pending",
                "The finalized night is not ready for report analysis.",
                status_code=409,
                retryable=True,
            )
        return _ResolvedReportSource(
            wake_date=wake_date,
            night_episode_id=str(row[0]),
            night_episode_revision_id=str(row[1]),
            quality_assessment_id=str(row[2]),
            current_risk_id=str(row[3]),
        )

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
            data_mode=cast(Literal["live", "replay"], context.data_mode),
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
    if command_type == "product.report.run.v1":
        return "product_agent"
    if command_type.startswith("interaction."):
        return "product_interaction"
    if command_type.startswith("sleep_api."):
        return "sleep_command"
    raise ProductApiError(
        "invalid_request",
        "The command type has no durable handler.",
        status_code=400,
    )


def _lock_l2_subject(
    cursor: Any,
    context: ProductRequestContext,
    *,
    capability: Literal["habit", "memory"],
) -> None:
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 42))",
        (
            ":".join(
                (
                    "l2",
                    capability,
                    context.namespace_id,
                    context.data_mode,
                    str(context.namespace_generation),
                    context.run_id or "",
                    context.arm_id or "",
                    context.subject_id,
                )
            ),
        ),
    )


def _load_habit_profile(
    cursor: Any,
    context: ProductRequestContext,
) -> HabitProfileState:
    cursor.execute(
        """
        SELECT profile_version, fact_json, fact_sha256
        FROM public.backend_habit_profile_revisions_v2
        WHERE namespace_id = %s AND data_mode = %s
          AND namespace_generation = %s
          AND COALESCE(run_id, '') = COALESCE(%s, '')
          AND COALESCE(arm_id, '') = COALESCE(%s, '')
          AND subject_id = %s
        ORDER BY profile_version
        """,
        _product_scope_params(context),
    )
    rows = cursor.fetchall()
    revisions: list[HabitFact] = []
    for expected_version, row in enumerate(rows, 1):
        if int(row[0]) != expected_version:
            raise RuntimeError("Habit Profile version chain is not contiguous")
        fact = HabitFact.model_validate(_json_object(row[1]))
        expected_hash = stable_hash(
            fact.model_dump(mode="json", exclude={"fact_hash"})
        )
        if fact.fact_hash != str(row[2]) or fact.fact_hash != expected_hash:
            raise RuntimeError("Habit Profile revision integrity check failed")
        revisions.append(fact)
    return HabitProfileState(
        subject_id=context.subject_id,
        version=len(revisions),
        revisions=tuple(revisions),
    )


def _load_memory_state(
    cursor: Any,
    context: ProductRequestContext,
) -> GovernedMemoryState:
    cursor.execute(
        """
        SELECT state_version, revision_json, revision_sha256
        FROM public.backend_governed_memory_revisions_v2
        WHERE namespace_id = %s AND data_mode = %s
          AND namespace_generation = %s
          AND COALESCE(run_id, '') = COALESCE(%s, '')
          AND COALESCE(arm_id, '') = COALESCE(%s, '')
          AND subject_id = %s
        ORDER BY state_version
        """,
        _product_scope_params(context),
    )
    rows = cursor.fetchall()
    revisions: list[GovernedMemoryItemV2] = []
    for expected_version, row in enumerate(rows, 1):
        if int(row[0]) != expected_version:
            raise RuntimeError("Governed Memory version chain is not contiguous")
        revision = GovernedMemoryItemV2.model_validate(_json_object(row[1]))
        if stable_hash(revision) != str(row[2]):
            raise RuntimeError("Governed Memory revision integrity check failed")
        revisions.append(revision)
    return GovernedMemoryState(
        subject_id=context.subject_id,
        version=len(revisions),
        revisions=tuple(revisions),
    )


def _habit_question_history(
    cursor: Any,
    context: ProductRequestContext,
) -> tuple[tuple[str, ...], dict[str, datetime]]:
    cursor.execute(
        """
        SELECT evidence_json
        FROM public.backend_habit_question_selections_v2
        WHERE namespace_id = %s AND data_mode = %s
          AND namespace_generation = %s
          AND COALESCE(run_id, '') = COALESCE(%s, '')
          AND COALESCE(arm_id, '') = COALESCE(%s, '')
          AND subject_id = %s AND evidence_json IS NOT NULL
        ORDER BY consumed_at
        """,
        _product_scope_params(context),
    )
    suppressed: set[str] = set()
    cooldown: dict[str, datetime] = {}
    for row in cursor.fetchall():
        values = row[0]
        if isinstance(values, str):
            values = json.loads(values)
        if not isinstance(values, list):
            raise RuntimeError("Habit evidence history is invalid")
        for value in values:
            evidence = HabitEvidence.model_validate(value)
            if evidence.disposition == HabitDisposition.NEVER_ASK:
                suppressed.add(evidence.concept_id)
            concept = HABIT_CONCEPTS[evidence.concept_id]
            until = evidence.captured_at + timedelta(hours=concept.cooldown_hours)
            if until > cooldown.get(evidence.concept_id, datetime.min.replace(tzinfo=UTC)):
                cooldown[evidence.concept_id] = until
    return tuple(sorted(suppressed)), cooldown


def _load_habit_selection_for_update(
    cursor: Any,
    context: ProductRequestContext,
    selection_id: str,
    *,
    now: datetime,
) -> HabitQuestionState:
    cursor.execute(
        """
        SELECT actor_id, role, selection_sha256, selection_json,
               consumed_at, expires_at
        FROM public.backend_habit_question_selections_v2
        WHERE selection_id = %s AND namespace_id = %s AND data_mode = %s
          AND namespace_generation = %s
          AND COALESCE(run_id, '') = COALESCE(%s, '')
          AND COALESCE(arm_id, '') = COALESCE(%s, '')
          AND subject_id = %s
        FOR UPDATE
        """,
        (
            selection_id,
            context.namespace_id,
            context.data_mode,
            context.namespace_generation,
            context.run_id,
            context.arm_id,
            context.subject_id,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise ProductApiError(
            "not_found",
            "Habit question selection was not found.",
            status_code=404,
        )
    if (
        str(row[0]) != context.actor_id
        or str(row[1]) != context.role.value
        or row[4] is not None
        or row[5] <= now
    ):
        raise ProductApiError(
            "state_conflict",
            "Habit question selection is unavailable or expired.",
            status_code=409,
        )
    selection = HabitQuestionState.model_validate(_json_object(row[3]))
    if selection.receipt_hash != str(row[2]):
        raise RuntimeError("Habit selection integrity check failed")
    return selection


def _insert_l2_pending_handle(
    cursor: Any,
    context: ProductRequestContext,
    *,
    capability: Literal["habit", "memory"],
    change_id: str,
    change_hash: str,
    change_json: Mapping[str, Any],
    confirmation_actor_id: str,
    expected_state_version: int,
    expires_at: datetime,
) -> PendingL2Change:
    confirmation_authority = _resolve_confirmation_authority(
        cursor,
        context,
        confirmation_actor_id=confirmation_actor_id,
    )
    confirmation_policy_sha256 = _authorization_policy_sha256(
        principal_id=context.service_principal_id,
        resolved=confirmation_authority,
        purpose=context.purpose,
    )
    handle_id = "l2-handle:" + secrets.token_hex(24)
    token = secrets.token_urlsafe(32)
    token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
    payload = {
        "schema_version": "l2_pending_change.v1",
        "capability": capability,
        "change_id": change_id,
        "change_hash": change_hash,
        "change": change_json,
        "source_actor_id": context.actor_id,
        "source_actor_role": context.role.value,
        "confirmation_actor_id": confirmation_actor_id,
        "token_sha256": token_sha256,
    }
    cursor.execute(
        """
        INSERT INTO public.backend_pending_handles (
          handle_id, handle_kind, namespace_id, data_mode,
          namespace_generation, run_id, arm_id, subject_id, actor_id,
          role, target_resource_type, target_resource_id,
          target_state_version, target_sha256, fact_snapshot_sha256,
          care_profile_state_version, authorization_epoch, privacy_epoch,
          retrieval_policy_epoch, policy_sha256, status, expires_at,
          handle_json
        ) VALUES (
          %s, 'confirmation', %s, %s, %s, %s, %s, %s, %s, 'elder',
          %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, 'pending', %s,
          %s::jsonb
        )
        """,
        (
            handle_id,
            context.namespace_id,
            context.data_mode,
            context.namespace_generation,
            context.run_id,
            context.arm_id,
            context.subject_id,
            confirmation_actor_id,
            f"l2_{capability}_change",
            change_id,
            expected_state_version + 1,
            change_hash,
            change_hash,
            confirmation_authority.authorization_epoch,
            confirmation_authority.privacy_epoch,
            confirmation_authority.retrieval_policy_epoch,
            confirmation_policy_sha256,
            expires_at,
            _json(payload),
        ),
    )
    return PendingL2Change(
        capability=capability,
        change_id=change_id,
        change_hash=change_hash,
        confirmation_handle=f"{handle_id}.{token}",
        expires_at=expires_at,
    )


def _resolve_confirmation_authority(
    cursor: Any,
    context: ProductRequestContext,
    *,
    confirmation_actor_id: str,
) -> ResolvedActorAuthority:
    try:
        cursor.execute(
            "SELECT * FROM public.sleepagent_resolve_actor_authority("
            "%s, %s, 'elder', %s)",
            (confirmation_actor_id, context.subject_id, context.purpose),
        )
        row = cursor.fetchone()
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "P0001":
            raise ProductApiError(
                "authorization_denied",
                "The confirmation actor has no unique active authority.",
                status_code=403,
            ) from exc
        raise
    if row is None:
        raise ProductApiError(
            "authorization_denied",
            "The confirmation actor has no active authority.",
            status_code=403,
        )
    scopes_value = row[7]
    if isinstance(scopes_value, str):
        scopes_value = json.loads(scopes_value)
    resolved = ResolvedActorAuthority(
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
    if (
        resolved.role is not ProductRole.ELDER
        or resolved.namespace_id != context.namespace_id
        or resolved.data_mode != context.data_mode
        or resolved.namespace_generation != context.namespace_generation
        or resolved.run_id != context.run_id
        or resolved.arm_id != context.arm_id
    ):
        raise ProductApiError(
            "authorization_denied",
            "The confirmation actor authority is outside this L2 scope.",
            status_code=403,
        )
    return resolved


def _consume_l2_pending_handle(
    cursor: Any,
    context: ProductRequestContext,
    request: L2ConfirmationRequest,
    *,
    capability: Literal["habit", "memory"],
    now: datetime,
) -> tuple[str, dict[str, Any]]:
    try:
        handle_id, token = request.confirmation_handle.rsplit(".", 1)
    except ValueError as exc:
        raise ProductApiError(
            "invalid_confirmation_handle",
            "The L2 confirmation handle is invalid.",
            status_code=400,
        ) from exc
    cursor.execute(
        """
        SELECT actor_id, role, target_resource_id, target_sha256,
               authorization_epoch, privacy_epoch, retrieval_policy_epoch,
               policy_sha256, status, expires_at, handle_json
        FROM public.backend_pending_handles
        WHERE handle_id = %s AND namespace_id = %s AND data_mode = %s
          AND namespace_generation = %s
          AND COALESCE(run_id, '') = COALESCE(%s, '')
          AND COALESCE(arm_id, '') = COALESCE(%s, '')
          AND subject_id = %s AND target_resource_type = %s
        FOR UPDATE
        """,
        (
            handle_id,
            context.namespace_id,
            context.data_mode,
            context.namespace_generation,
            context.run_id,
            context.arm_id,
            context.subject_id,
            f"l2_{capability}_change",
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise ProductApiError(
            "not_found",
            "L2 confirmation handle was not found.",
            status_code=404,
        )
    payload = _json_object(row[10])
    token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
    exact = (
        str(row[0]) == context.actor_id
        and str(row[1]) == "elder"
        and str(row[2]) == request.change_id
        and str(row[3]) == request.change_hash
        and int(row[4]) == context.authorization_epoch
        and int(row[5]) == context.privacy_epoch
        and int(row[6]) == context.retrieval_epoch
        and str(row[7]) == context.policy_sha256
        and str(row[8]) == "pending"
        and row[9] > now
        and payload.get("capability") == capability
        and payload.get("change_id") == request.change_id
        and payload.get("change_hash") == request.change_hash
        and payload.get("confirmation_actor_id") == context.actor_id
        and isinstance(payload.get("token_sha256"), str)
        and secrets.compare_digest(payload["token_sha256"], token_sha256)
    )
    if not exact:
        raise ProductApiError(
            "confirmation_binding_changed",
            "The L2 confirmation no longer matches its exact authority.",
            status_code=409,
        )
    return handle_id, payload


def _insert_memory_read_receipt(
    cursor: Any,
    context: ProductRequestContext,
    receipt: Any,
    *,
    product_episode_id: str | None,
) -> None:
    cursor.execute(
        """
        INSERT INTO public.backend_memory_read_receipts_v2 (
          receipt_id, namespace_id, data_mode, namespace_generation,
          run_id, arm_id, subject_id, product_episode_id,
          requesting_agent, purpose, query_sha256, result_sha256,
          receipt_sha256, authorization_epoch, privacy_epoch,
          receipt_json, completed_at
        ) VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
          %s, %s, %s::jsonb, %s
        )
        """,
        (
            receipt.receipt_id,
            context.namespace_id,
            context.data_mode,
            context.namespace_generation,
            context.run_id,
            context.arm_id,
            context.subject_id,
            product_episode_id,
            receipt.requesting_agent.value,
            receipt.purpose.value,
            receipt.query_hash,
            receipt.result_hash,
            receipt.receipt_hash,
            receipt.authorization_epoch,
            receipt.privacy_epoch,
            receipt.model_dump_json(),
            receipt.completed_at,
        ),
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise RuntimeError("PostgreSQL JSON contract is not an object")
    return value


def _optional_json_object(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return _json_object(value)


def _optional_json_objects(value: Any) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise RuntimeError("PostgreSQL JSON aggregate is not an array")
    return tuple(_json_object(item) for item in value)


def _resolved_report_model_mode(
    context: ProductRequestContext,
    *,
    configured: str | None,
) -> Literal["live", "deterministic"]:
    mode = configured or (
        "live" if context.data_mode == "live" else "deterministic"
    )
    if mode not in {"live", "deterministic"}:
        raise RuntimeError("report analysis model mode is invalid")
    if mode == "deterministic" and context.data_mode != "replay":
        raise RuntimeError("deterministic report analysis requires replay data")
    return cast(Literal["live", "deterministic"], mode)


def _default_model_pin(
    context: ProductRequestContext,
    *,
    model_mode: Literal["live", "deterministic"],
    deployment_mode: str | None,
) -> dict[str, Any]:
    if model_mode == "live":
        config = openai_compatible_provider_config_from_env()
        model_type = OpenAICompatibleStructuredAgentModel
        return build_safe_model_pin(
            implementation=(
                f"{model_type.__module__}.{model_type.__qualname__}"
            ),
            provider="openai-compatible",
            model_id=config.model,
            temperature=config.temperature,
            max_output_tokens=config.max_output_tokens,
            thinking_type=config.thinking_type,
            retry=config.retry,
            timeout_seconds=config.timeout_seconds,
            provider_endpoint=config.base_url,
        )
    selected_deployment = deployment_mode or (
        os.environ.get(_BACKEND_DEPLOYMENT_MODE_ENV, "").strip()
        or DeploymentMode.TEST.value
    )
    if selected_deployment not in {
        DeploymentMode.TEST.value,
        DeploymentMode.DEVELOPMENT.value,
    }:
        raise RuntimeError(
            "deterministic report analysis requires a non-production deployment"
        )
    model_type = DeterministicReplayStructuredAgentModel
    return build_safe_model_pin(
        implementation=f"{model_type.__module__}.{model_type.__qualname__}",
        provider=DETERMINISTIC_REPLAY_PROVIDER,
        model_id=DETERMINISTIC_REPLAY_MODEL_VERSION,
        deployment_mode=selected_deployment,
        data_mode=context.data_mode,
    )


def _report_runtime_subject_ref(context: ProductRequestContext) -> str:
    material = stable_hash(
        {"data_mode": context.data_mode, "subject_id": context.subject_id}
    )
    return f"subject:{material[:32]}"


def _default_shared_runtime_manifest_sha256(
    context: ProductRequestContext,
    *,
    model_mode: Literal["live", "deterministic"] | None = None,
    deployment_mode: str | None = None,
) -> str:
    profiles = default_agent_profiles()
    registry = SkillRegistry(default_skill_packages())
    registry_manifest = product_agent_manifest()
    agent_skills = {
        AgentId.EVIDENCE_REASONING: "interpret_scoped_evidence",
        AgentId.CARE_STRATEGY: "propose_single_care_action",
        AgentId.SAFETY_REVIEW: "review_action_and_publication",
    }
    subject_ref = _report_runtime_subject_ref(context)
    selected_model_mode = _resolved_report_model_mode(
        context,
        configured=model_mode,
    )
    model_pin = _default_model_pin(
        context,
        model_mode=selected_model_mode,
        deployment_mode=deployment_mode,
    )
    agents: dict[str, dict[str, Any]] = {}
    for agent_id, skill_id in agent_skills.items():
        profile = profiles[agent_id]
        package = registry.released(skill_id, agent_id, subject_id=subject_ref)
        agents[agent_id.value] = {
            "profile": {
                "profile_id": profile.profile_id,
                "version": profile.version,
                "profile_hash": profile.profile_hash,
                "output_schema_id": profile.output_schema_id,
            },
            "skill": {
                "skill_id": package.skill_id,
                "version": package.version,
                "package_hash": package.package_hash,
                "output_schema_id": package.output_schema_id,
            },
            "model": dict(model_pin),
        }
    tool_allowlist = registry_manifest["tool_invocation_allowlist"]
    tool_definitions = registry_manifest["tool_definitions"]
    tool_names = sorted(
        {
            tool_name
            for agent_id in agent_skills
            for tool_name in tool_allowlist[agent_id.value]
        }
    )
    catalog = CareActionCatalog()
    catalog_material = {
        "definitions": [
            item.model_dump(mode="json") for item in catalog.list_definitions()
        ],
        "delivery_policy": catalog.delivery_policy.model_dump(mode="json"),
    }
    sleepcare_profile = profiles[AgentId.SLEEP_CARE]
    sleepcare_control_skills = []
    for skill_id in ("plan_episode", "evaluate_work_product"):
        package = registry.released(
            skill_id,
            AgentId.SLEEP_CARE,
            subject_id=subject_ref,
        )
        sleepcare_control_skills.append(
            {
                "skill_id": package.skill_id,
                "version": package.version,
                "package_hash": package.package_hash,
                "output_schema_id": package.output_schema_id,
            }
        )
    manifest = build_shared_analysis_runtime_manifest(
        runner_version=PRODUCT_EPISODE_RUNNER_VERSION,
        prompt_compiler_version=PromptCompiler.compiler_version,
        safety_policy_version=PRODUCT_SAFETY_POLICY_VERSION,
        sleepcare_control={
            "profile": {
                "profile_id": sleepcare_profile.profile_id,
                "version": sleepcare_profile.version,
                "profile_hash": sleepcare_profile.profile_hash,
                "output_schema_id": sleepcare_profile.output_schema_id,
            },
            "skills": sleepcare_control_skills,
            "model": dict(model_pin),
        },
        agents=agents,
        tools={
            name: dict(tool_definitions[name]) for name in tool_names
        },
        care_catalog=catalog_material,
    )
    return stable_hash(manifest)


def _default_elder_narrative_manifest_sha256(
    context: ProductRequestContext,
    *,
    model_mode: Literal["live", "deterministic"] | None = None,
    deployment_mode: str | None = None,
) -> str:
    profiles = default_agent_profiles()
    registry = SkillRegistry(default_skill_packages())
    profile = profiles[AgentId.SLEEP_CARE]
    package = registry.released(
        "explain_for_elder",
        AgentId.SLEEP_CARE,
        subject_id=_report_runtime_subject_ref(context),
    )
    selected_model_mode = _resolved_report_model_mode(
        context,
        configured=model_mode,
    )
    return stable_hash(
        build_elder_narrative_runtime_manifest(
            runner_version=PRODUCT_EPISODE_RUNNER_VERSION,
            prompt_compiler_version=PromptCompiler.compiler_version,
            safety_policy_version=PRODUCT_SAFETY_POLICY_VERSION,
            profile={
                "profile_id": profile.profile_id,
                "version": profile.version,
                "profile_hash": profile.profile_hash,
                "output_schema_id": profile.output_schema_id,
            },
            skill={
                "skill_id": package.skill_id,
                "version": package.version,
                "package_hash": package.package_hash,
                "output_schema_id": package.output_schema_id,
            },
            model=_default_model_pin(
                context,
                model_mode=selected_model_mode,
                deployment_mode=deployment_mode,
            ),
            content_plan_assembly=True,
        )
    )


def _current_report_context_sha256(
    cursor: Any,
    context: ProductRequestContext,
    *,
    as_of: datetime,
) -> str:
    """Recompute only selected Habit/Memory meaning without read receipts."""

    habit_profile = _load_habit_profile(cursor, context)
    memory_state = _load_memory_state(cursor, context)
    habit_facts = sorted(
        (
            {"fact_id": fact.fact_id, "fact_hash": fact.fact_hash}
            for fact in habit_profile.current(as_of)
        ),
        key=lambda item: (item["fact_id"], item["fact_hash"]),
    )
    memory_slices: list[dict[str, Any]] = []
    for requesting_agent, purpose, concept_ids in (
        (
            AgentId.EVIDENCE_REASONING,
            MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT,
            _REPORT_EVIDENCE_MEMORY_CONCEPT_IDS,
        ),
        (
            AgentId.CARE_STRATEGY,
            MemoryPurpose.CARE_PREFERENCE_CONTEXT,
            _REPORT_CARE_MEMORY_CONCEPT_IDS,
        ),
    ):
        query = resolve_memory_query(
            MemoryQueryIntent(
                purpose=purpose,
                concept_ids=concept_ids,
                source_scope_kind=SourceScopeKind.HISTORICAL_RANGE,
                max_items=4,
                token_budget=800,
            ),
            invocation_id="product-report-read-only-context",
            actor_id=f"workload:{context.service_principal_id}",
            actor_role="system",
            subject_id=context.subject_id,
            requesting_agent=requesting_agent,
            authorization_scope=("memory:read",),
            as_of=as_of,
            privacy_epoch=context.privacy_epoch,
            authorization_epoch=context.authorization_epoch,
        )
        receipt = select_memory_slice(query, memory_state, now=as_of)
        memory_slices.append(
            {
                "requesting_agent": requesting_agent.value,
                "purpose": purpose.value,
                "query_intent": {
                    "source_scope_kind": SourceScopeKind.HISTORICAL_RANGE.value,
                    "max_items": 4,
                    "token_budget": 800,
                    "concept_ids": list(concept_ids),
                },
                "items": [
                    {
                        "revision_ref": item.revision_ref,
                        "concept_id": item.concept_id,
                        "value_schema_id": str(item.value_schema_id),
                        "value_schema_version": item.value_schema_version,
                        "value_hash": item.value_hash,
                    }
                    for item in receipt.items
                ],
            }
        )
    memory_slices.sort(
        key=lambda item: (item["requesting_agent"], item["purpose"])
    )
    return stable_hash(
        {
            "schema_version": "consumed_product_context.v1",
            "habit_facts": habit_facts,
            "memory_slices": memory_slices,
            "authorization_epoch": context.authorization_epoch,
            "privacy_epoch": context.privacy_epoch,
            "retrieval_policy_epoch": context.retrieval_epoch,
        }
    )


def _report_read_row(
    row: Any,
    *,
    current_context_sha256: str,
    current_runtime_manifest_sha256: str,
    current_projection_manifest_sha256: str,
    current_narrative_manifest_sha256: str,
) -> _ReportReadRow:
    return _ReportReadRow(
        wake_date=row[0],
        quality_json=_optional_json_object(row[1]),
        risk_json=_optional_json_object(row[2]),
        request_status=None if row[3] is None else str(row[3]),
        request_json=_optional_json_object(row[4]),
        shared_status=None if row[5] is None else str(row[5]),
        analysis_json=_optional_json_object(row[6]),
        view_status=None if row[7] is None else str(row[7]),
        view_json=_optional_json_object(row[8]),
        view_fact_snapshot_sha256=(
            None if row[9] is None else str(row[9])
        ),
        view_projection_identity_sha256=(
            None if row[10] is None else str(row[10])
        ),
        narrative_status=None if row[11] is None else str(row[11]),
        narrative_operation_json=_optional_json_object(row[12]),
        narrative_json=_optional_json_object(row[13]),
        has_stale_artifact=bool(row[14]),
        source_revision_valid=bool(row[15]),
        source_date_match_count=int(row[16]),
        narrative_operation_id=(
            None if row[17] is None else str(row[17])
        ),
        shared_failed_attempts=_optional_json_objects(row[18]),
        narrative_failed_attempts=_optional_json_objects(row[19]),
        shared_orphaned_journal_usage=_optional_json_objects(row[20]),
        narrative_orphaned_journal_usage=_optional_json_objects(row[21]),
        current_context_sha256=current_context_sha256,
        current_runtime_manifest_sha256=current_runtime_manifest_sha256,
        current_projection_manifest_sha256=(
            current_projection_manifest_sha256
        ),
        current_narrative_manifest_sha256=(
            current_narrative_manifest_sha256
        ),
    )


def _report_quality(
    quality_json: Mapping[str, Any] | None,
) -> ProductReportQuality | None:
    if quality_json is None:
        return None
    sufficiency = str(quality_json.get("data_sufficiency", ""))
    if sufficiency == "sufficient":
        return ProductReportQuality.GOOD
    if sufficiency == "partial":
        return ProductReportQuality.PARTIAL
    if sufficiency == "data_insufficient":
        return ProductReportQuality.UNUSABLE
    return None


def _shared_analysis_payload(
    analysis_json: Mapping[str, Any] | None,
) -> SharedNightAnalysis | None:
    if analysis_json is None:
        return None
    value = analysis_json.get("shared_analysis")
    if not isinstance(value, Mapping):
        return None
    try:
        return SharedNightAnalysis.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        return None


def _partial_caveat(
    quality: ProductReportQuality | None,
    analysis_json: Mapping[str, Any] | None,
) -> str | None:
    if quality != ProductReportQuality.PARTIAL:
        return None
    shared = _shared_analysis_payload(analysis_json)
    if shared is not None and shared.source.partial_caveat:
        return shared.source.partial_caveat.strip()
    return "This report is based on partial sleep data."


def _request_result(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if value is None:
        return {}
    report_result = value.get("report_result")
    if isinstance(report_result, Mapping):
        return report_result
    # Compatibility for already-persisted pre-report command artifacts.
    result = value.get("result")
    return result if isinstance(result, Mapping) else {}


def _allowlisted_trace_state(
    value: Any,
    *,
    allowed: frozenset[str],
    fallback: str,
) -> str:
    candidate = str(value) if value is not None else ""
    return candidate if candidate in allowed else fallback


def _report_trace(
    row: _ReportReadRow,
    *,
    quality: ProductReportQuality | None,
    narrative: ProductReportNarrative | None,
) -> ProductReportTrace:
    result = _request_result(row.request_json)
    request_trace = result.get("trace")
    if not isinstance(request_trace, Mapping):
        request_trace = {}
    usage_values: list[Mapping[str, Any]] = []
    if row.analysis_json is not None:
        candidate = row.analysis_json.get("provider_usage")
        if isinstance(candidate, Mapping):
            usage_values.append(candidate)
    if row.narrative_json is not None:
        candidate = row.narrative_json.get("provider_usage")
        if isinstance(candidate, Mapping):
            usage_values.append(candidate)
    for uncommitted_attempts, operation_type, prepared_schema in (
        (
            row.shared_failed_attempts,
            "product.shared_analysis.v1",
            "product_agent_prepared_attempt.v3",
        ),
        (
            row.narrative_failed_attempts,
            "product.elder_narrative.v1",
            "product_elder_narrative_prepared_attempt.v1",
        ),
    ):
        for uncommitted_attempt in uncommitted_attempts:
            uncommitted_usage = _uncommitted_provider_usage(
                uncommitted_attempt,
                operation_type=operation_type,
                prepared_schema=prepared_schema,
            )
            if uncommitted_usage is not None:
                usage_values.append(uncommitted_usage)
    usage_values.extend(row.shared_orphaned_journal_usage)
    usage_values.extend(row.narrative_orphaned_journal_usage)
    gate_fallback = (
        "not_evaluated"
        if row.request_status is None
        else "urgent"
        if _risk_is_urgent(row.risk_json)
        else "unusable"
        if quality == ProductReportQuality.UNUSABLE
        else "analyzable"
    )
    gate = _allowlisted_trace_state(
        result.get("gate"),
        allowed=frozenset(
            {"analyzable", "urgent", "unusable", "not_evaluated"}
        ),
        fallback=gate_fallback,
    )
    shared_fallback = (
        "created"
        if result.get("shared_operation_created") is True
        else "reused"
        if result.get("shared_operation_created") is False
        and isinstance(result.get("shared_operation_id"), str)
        else "reused"
        if row.analysis_json is not None
        else "pending"
        if row.request_status in {"pending", "retry", "running"}
        or row.shared_status in {"pending", "retry", "running"}
        else "not_applicable"
    )
    narrative_fallback = (
        "fallback"
        if narrative is not None
        and narrative.state == ProductNarrativeState.FALLBACK
        else "pending"
        if narrative is not None
        and narrative.state == ProductNarrativeState.PENDING
        else _report_narrative_creation_state(row, result) or "reused"
        if narrative is not None
        and narrative.state == ProductNarrativeState.READY
        else "not_applicable"
    )
    narrative_trace_state = (
        narrative_fallback
        if narrative is not None
        else _allowlisted_trace_state(
            request_trace.get("elder_narrative"),
            allowed=frozenset(
                {
                    "created",
                    "reused",
                    "pending",
                    "fallback",
                    "not_applicable",
                }
            ),
            fallback=narrative_fallback,
        )
    )
    return ProductReportTrace(
        gate=cast(
            Literal["analyzable", "urgent", "unusable", "not_evaluated"],
            gate,
        ),
        shared_analysis=cast(
            Literal["created", "reused", "pending", "not_applicable"],
            _allowlisted_trace_state(
                request_trace.get("shared_analysis"),
                allowed=frozenset(
                    {"created", "reused", "pending", "not_applicable"}
                ),
                fallback=shared_fallback,
            ),
        ),
        elder_narrative=cast(
            Literal[
                "created", "reused", "pending", "fallback", "not_applicable"
            ],
            narrative_trace_state,
        ),
        fallback_used=(
            narrative is not None
            and narrative.state == ProductNarrativeState.FALLBACK
        ),
        provider_call_count=sum(
            _safe_usage_count(item.get("call_count")) for item in usage_values
        ),
        provider_input_tokens=sum(
            _safe_usage_count(item.get("input_tokens")) for item in usage_values
        ),
        provider_output_tokens=sum(
            _safe_usage_count(item.get("output_tokens")) for item in usage_values
        ),
        provider_request_ids_present=any(
            item.get("request_ids_present") is True for item in usage_values
        ),
    )


def _report_narrative_creation_state(
    row: _ReportReadRow,
    result: Mapping[str, Any],
) -> Literal["created", "reused"] | None:
    """Attribute the selected narrative without exposing its durable ID."""

    selected_operation_id = row.narrative_operation_id
    if not isinstance(selected_operation_id, str) or not selected_operation_id:
        return None
    request_narrative_id = result.get("elder_narrative_operation_id")
    if isinstance(request_narrative_id, str):
        if request_narrative_id != selected_operation_id:
            return None
        created = result.get("elder_narrative_operation_created")
        if created is True:
            return "created"
        if created is False:
            return "reused"
        return None
    if request_narrative_id is not None:
        return None

    # Legacy request rows were closed before the shared commit reserved the
    # narrative. The request that created that shared operation is the only
    # request that can have created its first, identity-bound narrative;
    # concurrent joiners reused the canonical shared work.
    shared_operation_id = result.get("shared_operation_id")
    if not isinstance(shared_operation_id, str) or not shared_operation_id:
        return None
    if result.get("shared_operation_created") is True:
        return "created"
    if result.get("shared_operation_created") is False:
        return "reused"
    return None


def _risk_is_urgent(value: Mapping[str, Any] | None) -> bool:
    return bool(value and value.get("health_escalation_allowed") is True)


def _safe_usage_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, parsed)


def _uncommitted_provider_usage(
    value: Mapping[str, Any],
    *,
    operation_type: str,
    prepared_schema: str,
) -> Mapping[str, Any] | None:
    """Extract aggregates from bound, query-invisible Product work."""

    schema_version = value.get("schema_version")
    if schema_version == "product_provider_failed_attempt.v1":
        if (
            value.get("operation_type") != operation_type
            or value.get("failure_code") != "provider_attempt_failed"
            or not isinstance(value.get("outcome_unknown"), bool)
        ):
            return None
    elif schema_version != prepared_schema:
        return None
    usage = value.get("provider_usage")
    return usage if isinstance(usage, Mapping) else None


def _is_sha256_string(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validated_shared_analysis(
    row: _ReportReadRow,
) -> SharedNightAnalysis | None:
    analysis = row.analysis_json
    if (
        analysis is None
        or analysis.get("schema_version") != "shared_night_analysis.v1"
        or row.request_json is None
    ):
        return None
    result = _request_result(row.request_json)
    desired = result.get("desired_analysis_sha256")
    root_desired = analysis.get("desired_analysis_sha256")
    root_context = analysis.get("consumed_context_sha256")
    root_runtime = analysis.get("runtime_manifest_sha256")
    if (
        result.get("schema_version") != "product_report_request_result.v1"
        or not _is_sha256_string(desired)
        or not _is_sha256_string(root_desired)
        or root_desired != desired
        or not _is_sha256_string(root_context)
        or root_context != row.current_context_sha256
        or not _is_sha256_string(root_runtime)
        or root_runtime != row.current_runtime_manifest_sha256
    ):
        return None
    shared = _shared_analysis_payload(analysis)
    if shared is None:
        return None
    if (
        shared.source.desired_analysis_sha256 != desired
        or shared.source.consumed_context_sha256 != root_context
        or shared.source.runtime_manifest_sha256 != root_runtime
        or shared.source.wake_date != row.wake_date
    ):
        return None
    return shared


def _validated_role_projection(
    row: _ReportReadRow,
    shared: SharedNightAnalysis,
) -> RoleProjection | None:
    analysis = row.analysis_json
    if analysis is None or row.view_json is None:
        return None
    try:
        projection = RoleProjection.model_validate(row.view_json)
    except (ValidationError, ValueError, TypeError):
        return None
    if projection.role is ReportRole.ELDER:
        if projection.presentation_authority_sha256 is None:
            return None
    else:
        expected_projection = next(
            (
                item
                for item in build_shared_role_projections(shared)
                if item.role is projection.role
            ),
            None,
        )
        if expected_projection is None or projection != expected_projection:
            return None
    if (
        row.view_fact_snapshot_sha256 != shared.fact_snapshot_hash
        or not _is_sha256_string(row.view_projection_identity_sha256)
    ):
        return None
    projection_manifest = analysis.get("projection_manifest")
    projection_manifest_sha256 = analysis.get(
        "projection_manifest_sha256"
    )
    projection_identities = analysis.get("projection_identities")
    if (
        not isinstance(projection_manifest, Mapping)
        or not _is_sha256_string(projection_manifest_sha256)
        or stable_hash(projection_manifest) != projection_manifest_sha256
        or not _is_sha256_string(row.current_projection_manifest_sha256)
        or not isinstance(projection_identities, Mapping)
        or set(projection_identities)
        != {role.value for role in ReportRole}
        or any(
            not _is_sha256_string(value)
            for value in projection_identities.values()
        )
    ):
        return None
    desired = analysis.get("desired_analysis_sha256")
    if not isinstance(desired, str):
        return None
    expected_identity = role_projection_identity_sha256(
        desired_analysis_sha256=desired,
        role=projection.role,
        projection_sha256=projection.projection_sha256,
        projection_manifest_sha256=(
            row.current_projection_manifest_sha256
        ),
    )
    if row.view_projection_identity_sha256 != expected_identity:
        return None
    # AnalysisRevision projection metadata is immutable provenance.  It binds
    # the initial role rows when its manifest is still current; projection-only
    # refreshes advance the role-row identity without rewriting shared analysis.
    if (
        projection_manifest_sha256 == row.current_projection_manifest_sha256
        and projection_identities.get(projection.role.value)
        != expected_identity
    ):
        return None
    return projection


def _analysis_is_compatible(row: _ReportReadRow) -> bool:
    return _validated_shared_analysis(row) is not None


def _report_state(
    row: _ReportReadRow,
    quality: ProductReportQuality | None,
) -> ProductReportState:
    result = _request_result(row.request_json)
    result_schema = result.get("schema_version")
    result_state = result.get("state")
    terminal_result = (
        row.request_status == "succeeded"
        and result_schema == "product_report_request_result.v1"
    )
    if terminal_result and result_state == "urgent_handled":
        return ProductReportState.URGENT_HANDLED
    if terminal_result and result_state == "unusable_blocked":
        return ProductReportState.UNUSABLE_BLOCKED
    shared = _validated_shared_analysis(row)
    if shared is not None:
        projection = _validated_role_projection(row, shared)
        if projection is None:
            return ProductReportState.STALE
        if (
            row.view_status == "blocked"
            and projection.state is RoleProjectionState.POLICY_BLOCKED
        ):
            return ProductReportState.POLICY_BLOCKED
        if (
            row.view_status == "ready"
            and projection.state is RoleProjectionState.READY
        ):
            return ProductReportState.READY
        return ProductReportState.FAILED
    if row.request_status in {"pending", "retry", "running"}:
        return ProductReportState.PENDING
    if row.shared_status in {"pending", "retry", "running"}:
        return ProductReportState.PENDING
    if terminal_result and result_state == "pending":
        if row.shared_status in {"failed", "dead_letter", "outcome_unknown"}:
            return ProductReportState.FAILED
        if row.shared_status == "succeeded":
            return (
                ProductReportState.STALE
                if row.analysis_json is not None or row.has_stale_artifact
                else ProductReportState.FAILED
            )
        return ProductReportState.PENDING
    if row.shared_status in {"failed", "dead_letter", "outcome_unknown"}:
        return ProductReportState.FAILED
    if row.request_status in {"failed", "dead_letter", "outcome_unknown"}:
        return ProductReportState.FAILED
    if row.analysis_json is not None or row.has_stale_artifact:
        return ProductReportState.STALE
    if terminal_result and result_state == "ready":
        return ProductReportState.FAILED
    return ProductReportState.NOT_RUN


def _report_narrative(
    context: ProductRequestContext,
    row: _ReportReadRow,
    *,
    report_state: ProductReportState,
) -> ProductReportNarrative | None:
    if context.role != ProductRole.ELDER or report_state != ProductReportState.READY:
        return None
    shared = _validated_shared_analysis(row)
    projection = (
        None if shared is None else _validated_role_projection(row, shared)
    )
    if (
        shared is None
        or projection is None
        or projection.role is not ReportRole.ELDER
        or not _is_sha256_string(row.view_projection_identity_sha256)
    ):
        return ProductReportNarrative(state=ProductNarrativeState.STALE)
    operation = row.narrative_operation_json
    message_atoms: tuple[ElderMessageAtom, ...] = ()
    if operation is not None:
        atom_values = operation.get("elder_message_atoms")
        try:
            message_atoms = tuple(
                ElderMessageAtom.model_validate(item)
                for item in atom_values
            ) if isinstance(atom_values, list) else ()
            atom_authority_sha256 = elder_presentation_authority_sha256(
                message_atoms
            )
            validate_elder_message_atoms(shared, message_atoms)
        except (ValidationError, ValueError, TypeError):
            return ProductReportNarrative(state=ProductNarrativeState.STALE)
        expected_render_identity = stable_hash(
            {
                "schema_version": "elder_narrative_request.v1",
                "shared_analysis_sha256": shared.shared_analysis_sha256,
                "elder_projection_sha256": (
                    row.view_projection_identity_sha256
                ),
                "render_manifest_sha256": (
                    row.current_narrative_manifest_sha256
                ),
            }
        )
        if (
            operation.get("narrative_manifest_sha256")
            != row.current_narrative_manifest_sha256
            or operation.get("projection_manifest_sha256")
            != row.current_projection_manifest_sha256
            or operation.get("shared_analysis_sha256")
            != shared.shared_analysis_sha256
            or operation.get("elder_projection_sha256")
            != row.view_projection_identity_sha256
            or operation.get("elder_projection_content_sha256")
            != projection.projection_sha256
            or atom_authority_sha256
            != projection.presentation_authority_sha256
            or operation.get("elder_projection")
            != projection.model_dump(mode="json")
            or operation.get("render_identity_sha256")
            != expected_render_identity
        ):
            return ProductReportNarrative(state=ProductNarrativeState.STALE)
    if row.narrative_status in {"pending", "retry", "running"}:
        return ProductReportNarrative(state=ProductNarrativeState.PENDING)
    payload = row.narrative_json
    narrative = None if payload is None else payload.get("narrative")
    if isinstance(narrative, Mapping):
        try:
            validated = RuntimeElderNarrative.model_validate(narrative)
        except (ValidationError, ValueError, TypeError):
            validated = None
        if validated is not None:
            if (
                operation is None
                or validated.source_shared_analysis_sha256
                != shared.shared_analysis_sha256
                or validated.source_projection_sha256
                != row.view_projection_identity_sha256
                or validated.render_identity_sha256
                != operation.get("render_identity_sha256")
            ):
                return ProductReportNarrative(
                    state=ProductNarrativeState.FALLBACK,
                    text=projection.text.strip(),
                )
            if validated.state is RuntimeElderNarrativeState.READY:
                assert validated.text is not None
                assert validated.communication is not None
                try:
                    validate_elder_communication_draft(
                        validated.communication,
                        message_atoms,
                    )
                except (ValueError, TypeError):
                    return ProductReportNarrative(
                        state=ProductNarrativeState.FALLBACK,
                        text=projection.text.strip(),
                    )
                return ProductReportNarrative(
                    state=ProductNarrativeState.READY,
                    text=validated.text.strip(),
                )
            if validated.state is RuntimeElderNarrativeState.FALLBACK:
                return ProductReportNarrative(
                    state=ProductNarrativeState.FALLBACK,
                    text=projection.text.strip(),
                )
            if validated.state is RuntimeElderNarrativeState.STALE:
                return ProductReportNarrative(
                    state=ProductNarrativeState.STALE
                )
            if validated.state is RuntimeElderNarrativeState.FAILED:
                return ProductReportNarrative(
                    state=ProductNarrativeState.FALLBACK,
                    text=projection.text.strip(),
                )
    # A missing/invalid/failed narrative never suppresses deterministic content.
    return ProductReportNarrative(
        state=ProductNarrativeState.FALLBACK,
        text=projection.text.strip(),
    )


def _report_failure_code(
    state: ProductReportState,
) -> ProductReportFailureCode | None:
    return cast(
        ProductReportFailureCode | None,
        {
            ProductReportState.FAILED: "analysis_failed",
            ProductReportState.STALE: "source_stale",
            ProductReportState.POLICY_BLOCKED: "doctor_safety_unavailable",
            ProductReportState.UNUSABLE_BLOCKED: "data_unusable",
        }.get(state),
    )


def _report_response(
    context: ProductRequestContext,
    row: _ReportReadRow,
    *,
    include_trace: bool,
) -> ProductSleepReportResponse:
    quality = _report_quality(row.quality_json)
    state = _report_state(row, quality)
    shared = _validated_shared_analysis(row)
    validated_projection = (
        None if shared is None else _validated_role_projection(row, shared)
    )
    if state in {
        ProductReportState.READY,
        ProductReportState.POLICY_BLOCKED,
    } and (
        validated_projection is None
        or validated_projection.role.value != context.role.value
    ):
        state = ProductReportState.STALE
    projection = None
    if state == ProductReportState.READY:
        assert validated_projection is not None
        assert validated_projection.text is not None
        projection = ProductReportProjection(
            audience=context.role,
            summary_text=validated_projection.text.strip(),
            context_notice=validated_projection.context_notice.strip(),
        )
    narrative = _report_narrative(context, row, report_state=state)
    response_trace = (
        _report_trace(row, quality=quality, narrative=narrative)
        if include_trace
        else None
    )
    return ProductSleepReportResponse(
        wake_date=row.wake_date,
        state=state,
        audience=context.role,
        quality=quality,
        quality_caveat=_partial_caveat(quality, row.analysis_json),
        projection=projection,
        narrative=narrative,
        failure_code=_report_failure_code(state),
        trace=response_trace,
    )


def _aggregate_report_trace(
    reports: tuple[ProductSleepReportResponse, ...],
) -> ProductReportTrace | None:
    traces = tuple(report.trace for report in reports if report.trace is not None)
    if not traces:
        return None
    return ProductReportTrace(
        gate="not_evaluated",
        shared_analysis=(
            "pending"
            if any(item.shared_analysis == "pending" for item in traces)
            else "created"
            if any(item.shared_analysis == "created" for item in traces)
            else "reused"
            if any(item.shared_analysis == "reused" for item in traces)
            else "not_applicable"
        ),
        elder_narrative=(
            "pending"
            if any(item.elder_narrative == "pending" for item in traces)
            else "created"
            if any(item.elder_narrative == "created" for item in traces)
            else "reused"
            if any(item.elder_narrative == "reused" for item in traces)
            else "fallback"
            if any(item.elder_narrative == "fallback" for item in traces)
            else "not_applicable"
        ),
        fallback_used=any(item.fallback_used for item in traces),
        provider_call_count=sum(item.provider_call_count for item in traces),
        provider_input_tokens=sum(item.provider_input_tokens for item in traces),
        provider_output_tokens=sum(item.provider_output_tokens for item in traces),
        provider_request_ids_present=any(
            item.provider_request_ids_present for item in traces
        ),
    )


def _product_auth_error(exc: SleepApiSecurityError) -> ProductApiError:
    return ProductApiError(
        exc.code.value.lower(),
        str(exc),
        status_code=exc.status_code,
        retryable=exc.retryable,
    )


def _uow_scope(context: ProductRequestContext) -> UowScope:
    return UowScope(
        namespace_id=context.namespace_id,
        namespace_generation=context.namespace_generation,
        data_mode=cast(Literal["live", "replay"], context.data_mode),
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
            state=cast(
                Literal[
                    "confirmed_pending_delivery", "active", "completed", "cancelled"
                ],
                str(row[3]),
            ),
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
            state=cast(
                Literal["pending_feedback", "following_up", "completed", "ended"],
                str(row[3]),
            ),
            updated_at=row[7],
        )
    raise ProductApiError(
        "projection_corrupt",
        "The care projection contains an unsupported record type.",
        status_code=500,
    )


def _subject_pseudonym(subject_id: str) -> str:
    return public_product_subject_ref(subject_id)


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


def _authorization_policy_sha256(
    *,
    principal_id: str,
    resolved: ResolvedActorAuthority,
    purpose: str,
) -> str:
    return _sha256(
        {
            "schema_version": "authorization_policy.v1",
            "principal_id": principal_id,
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
