# 本模块以 PostgreSQL 租约和围栏提交唯一 Product Agent 队列结果。
"""Durable PostgreSQL worker adapter for the canonical four-role Product Agent.

The model/Agent phase prepares an immutable artifact without a database
transaction.  A later short Unit of Work validates the current lease fence,
source Episode revision and governance epochs before atomically publishing the
AnalysisRevision, all three role views, the terminal Operation and outbox
event.  Prepared attempts are durable but never query-visible.

Replay keeps one durable Product path while model selection is explicit:
deterministic mode remains the reproducible baseline and live mode reuses the
configured OpenAI-compatible structured Agent runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from sleepagent.config import (
    DataMode as BackendDataMode,
    ModelMode,
    ProcessRole,
    ReportPipelineMode,
    SleepBackendSettings,
)
from sleepagent.persistence.uow import (
    TransactionBoundConnection,
    UnitOfWorkFactory,
    UowScope,
)
from sleepagent.runtime.contracts import (
    AgentId,
    AuthenticatedBinding,
    CareStrategy,
    EpisodeStatus,
    EpisodeType,
    EvidencePacket,
    ExecutionMode,
    FactSnapshot,
    SourceScope,
    SourceScopeKind,
    stable_hash,
)
from sleepagent.runtime.results import (
    PRODUCT_EPISODE_RUNNER_VERSION,
    PinnedPersonalizationContext,
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
)
from sleepagent.runtime.registry import product_agent_manifest
from sleepagent.runtime.reports import (
    ElderMessageAtom,
    ElderNarrative,
    ElderNarrativeRequest,
    ReportRole,
    ReportingContextV1,
    RoleProjection,
    RoleProjectionState,
    SharedAnalysisRunRequest,
    SharedAnalysisSourceV1,
    SharedNightAnalysis,
    build_elder_narrative_runtime_manifest,
    build_elder_message_atoms,
    build_role_projection_runtime_manifest,
    build_safe_model_pin,
    build_shared_analysis_runtime_manifest,
    build_shared_role_projections,
    role_projection_identity_sha256,
)
from sleepagent.runtime.governance import (
    PRODUCT_SAFETY_POLICY_VERSION,
    AcceptedWorkProduct,
)
from sleepagent.runtime.provider import (
    ProviderTransportAuditObserver,
    provider_transport_audit_scope,
)
from sleepagent.domain.habit import HabitFact, HabitProfileState
from sleepagent.runtime.memory import (
    GovernedMemoryItemV2,
    GovernedMemoryState,
    MemoryPurpose,
    MemoryQueryIntent,
    MemoryReadReceipt,
    resolve_memory_query,
    select_memory_slice,
)
from sleepagent.domain.contracts import (
    AnalysisRevision,
    AnalysisRole,
    AnalysisRoleView,
    AnalysisStatus,
    CurrentRisk,
    DataMode,
    DataSufficiency,
    DeterministicQualityAssessment,
    HeartRatePayload,
    RespiratoryRatePayload,
    RoleViewStatus,
    SleepObservation,
)
from sleepagent.domain.episodes import NightEpisodeV2, UUID7Generator
from sleepagent.domain.product_data import (
    ProductLongitudinalRiskContext,
    ProductNightVitalSummary,
    ProductRevisionFacts,
    _project_observation,
    build_longitudinal_vital_risk_context,
    public_product_subject_ref,
)
from sleepagent.workers.runtime import (
    B3ClaimInvariantError,
    DispatchKnownNotSent,
    InvocationKind,
    LeaseLostError,
    LeaseClaim,
    OutcomeUnknownError,
    RetryableWorkError,
    TerminalWorkError,
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
    WorkHandler,
    WorkResult,
    exact_worker_scope,
    worker_uow_factory,
)

if TYPE_CHECKING:
    from sleepagent.runtime.factory import (
        ProductRuntimeBundle,
    )

UTC = timezone.utc

EVIDENCE_MEMORY_CONCEPT_IDS = (
    "sleep.context.night_routine",
    "sleep.context.environment",
)
CARE_MEMORY_CONCEPT_IDS = (
    "sleep.preference.care_delivery",
    "sleep.preference.communication",
)

PRODUCT_AGENT_OPERATION = "product_agent"
PRODUCT_REPORT_RUN_OPERATION = "product.report.run.v1"
PRODUCT_SHARED_ANALYSIS_OPERATION = "product.shared_analysis.v1"
PRODUCT_ELDER_NARRATIVE_OPERATION = "product.elder_narrative.v1"
PRODUCT_AGENT_COMPATIBILITY_QUEUE = "product_agent_compatibility"
PRODUCT_CANONICAL_RETRY_BUDGET = 5
PRODUCT_FINAL_CONTEXT_LOCK_ORDER = ("habit", "memory")
PRODUCT_WORK_OPERATION_TYPES = frozenset(
    {
        PRODUCT_AGENT_OPERATION,
        PRODUCT_REPORT_RUN_OPERATION,
        PRODUCT_SHARED_ANALYSIS_OPERATION,
        PRODUCT_ELDER_NARRATIVE_OPERATION,
    }
)

PUBLIC_PRODUCT_TODAY_FIELD_ALLOWLIST = frozenset(
    {
        "schema_version",
        "data_mode",
        "synthetic_non_release",
        "state",
        "subject_ref",
        "role",
        "episode_id",
        "episode_revision_id",
        "episode_local_date",
        "assignment_basis",
        "analysis_revision_id",
        "projection_id",
        "projection_version",
        "committed_at",
        "content",
    }
)


class _PublicTodayContent(BaseModel):
    """Typed allowlist for role-visible health content only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audience: AnalysisRole
    summary_text: str = Field(min_length=1)
    context_notice: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def enforce_role_specific_fields(self) -> "_PublicTodayContent":
        if self.audience == AnalysisRole.DOCTOR:
            if self.evidence_refs is None:
                raise ValueError("doctor public content requires evidence_refs")
        elif self.evidence_refs is not None:
            raise ValueError("non-doctor public content cannot expose evidence_refs")
        return self


class _PublicTodayProjection(BaseModel):
    """Typed public persistence contract; internal lineage fields are excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["product_sleep_today.v1"] = "product_sleep_today.v1"
    data_mode: DataMode
    synthetic_non_release: bool
    state: RoleViewStatus
    subject_ref: str = Field(pattern=r"^subject:sha256:[0-9a-f]{64}$")
    role: AnalysisRole
    episode_id: str = Field(min_length=1)
    episode_revision_id: str = Field(min_length=1)
    episode_local_date: str = Field(min_length=1)
    assignment_basis: str = Field(min_length=1)
    analysis_revision_id: str = Field(min_length=1)
    projection_id: str = Field(min_length=1)
    projection_version: int = Field(ge=1)
    committed_at: str = Field(min_length=1)
    content: _PublicTodayContent

    @model_validator(mode="after")
    def enforce_role_and_public_subject(self) -> "_PublicTodayProjection":
        if self.content.audience != self.role:
            raise ValueError("public content audience does not match its role")
        return self


class ProductAgentWorkerError(RuntimeError):
    """Base error for the fenced Product Agent vertical slice."""


class ProductAgentCompositionError(ProductAgentWorkerError):
    """The configured process cannot safely compose this handler."""


class ProductAgentInvariantError(ProductAgentWorkerError):
    """Persisted Product work violates a frozen vertical-slice contract."""


class ProductAgentLeaseLost(ProductAgentWorkerError):
    """The Operation lease fence no longer authorizes a commit."""


class ProductAgentStaleSource(ProductAgentWorkerError):
    """The Product work targets a superseded NightEpisode revision."""


class ProductAgentConflict(ProductAgentWorkerError):
    """A concurrent immutable Product result differs from this attempt."""


class ProductAgentLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["product_agent_lease.v1"] = "product_agent_lease.v1"
    operation_id: str = Field(min_length=1)
    attempt_sequence: int = Field(ge=1)
    lease_generation: int = Field(ge=1)
    fencing_token: str = Field(min_length=32)
    worker_instance: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class LoadedProductAgentSource:
    operation_id: str
    operation_json: dict[str, Any]
    night_episode_id: str
    night_episode_revision_id: str
    night_episode_revision_number: int
    previous_analysis_revision_id: str | None
    next_analysis_revision_number: int
    subject_id: str
    policy_sha256: str
    episode: NightEpisodeV2
    facts: ProductRevisionFacts
    observation_set_sha256: str
    adapter_versions: dict[str, str]
    observation_schema_versions: tuple[str, ...]
    policy_versions: dict[str, str]
    habit_profile: HabitProfileState
    memory_state: GovernedMemoryState


class PreparedRoleRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: AnalysisRole
    product_episode_id: str = Field(min_length=1)
    fact_snapshot: FactSnapshot
    personalization: PinnedPersonalizationContext
    result: ProductEpisodeRunResult | None = None
    role_projection: RoleProjection | None = None
    role_view: AnalysisRoleView

    @model_serializer(mode="wrap")
    def serialize_role_run(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, Any]:
        payload = dict(handler(self))
        if self.result is None:
            payload.pop("result", None)
        if self.role_projection is None:
            payload.pop("role_projection", None)
        return payload


class ProductProviderUsage(BaseModel):
    """Safe aggregate retained with a prepared Product attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call_count: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    request_ids_present: bool = False
    failure_count: int = Field(default=0, ge=0)


ReportParityCategory = Literal[
    "fact_identities",
    "metric_values_units",
    "quality_status",
    "risk_classification",
    "care_candidate_semantics",
    "role_visible_fact_sets",
    "source_references",
]


class ReportShadowComparison(BaseModel):
    """Audit-only structured parity evidence; shared remains authoritative."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["report_shadow_comparison.v1"] = (
        "report_shadow_comparison.v1"
    )
    authoritative_path: Literal["shared"] = "shared"
    external_side_effects_permitted: Literal[False] = False
    legacy_attempt_sha256: str = Field(min_length=64, max_length=64)
    shared_analysis_sha256: str = Field(min_length=64, max_length=64)
    category_material_sha256: dict[str, dict[Literal["legacy", "shared"], str]]
    mismatch_categories: tuple[ReportParityCategory, ...] = ()
    comparison_sha256: str = Field(min_length=64, max_length=64)

    @classmethod
    def create(
        cls,
        *,
        legacy_attempt_sha256: str,
        shared_analysis_sha256: str,
        category_material: Mapping[
            ReportParityCategory,
            tuple[Any, Any],
        ],
    ) -> "ReportShadowComparison":
        category_hashes: dict[
            str,
            dict[Literal["legacy", "shared"], str],
        ] = {}
        mismatches: list[ReportParityCategory] = []
        for category, (legacy_material, shared_material) in category_material.items():
            legacy_hash = stable_hash(legacy_material)
            shared_hash = stable_hash(shared_material)
            category_hashes[category] = {
                "legacy": legacy_hash,
                "shared": shared_hash,
            }
            if legacy_hash != shared_hash:
                mismatches.append(category)
        material = {
            "schema_version": "report_shadow_comparison.v1",
            "authoritative_path": "shared",
            "external_side_effects_permitted": False,
            "legacy_attempt_sha256": legacy_attempt_sha256,
            "shared_analysis_sha256": shared_analysis_sha256,
            "category_material_sha256": category_hashes,
            "mismatch_categories": mismatches,
        }
        return cls(
            legacy_attempt_sha256=legacy_attempt_sha256,
            shared_analysis_sha256=shared_analysis_sha256,
            category_material_sha256=category_hashes,
            mismatch_categories=tuple(mismatches),
            comparison_sha256=stable_hash(material),
        )

    @model_validator(mode="after")
    def validate_comparison_identity(self) -> "ReportShadowComparison":
        expected_mismatches = tuple(
            cast(ReportParityCategory, category)
            for category, values in self.category_material_sha256.items()
            if values["legacy"] != values["shared"]
        )
        if self.mismatch_categories != expected_mismatches:
            raise ValueError("shadow mismatch categories are inconsistent")
        expected = stable_hash(
            self.model_dump(mode="json", exclude={"comparison_sha256"})
        )
        if self.comparison_sha256 != expected:
            raise ValueError("shadow comparison hash is inconsistent")
        return self


class FailedProductProviderAttempt(BaseModel):
    """Query-invisible audit row for provider work without an artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["product_provider_failed_attempt.v1"] = (
        "product_provider_failed_attempt.v1"
    )
    product_attempt_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    operation_type: Literal[
        "product.shared_analysis.v1",
        "product.elder_narrative.v1",
    ]
    night_episode_revision_id: str = Field(min_length=1)
    source_state_version: int = Field(ge=1)
    source_fact_snapshot_sha256: str = Field(min_length=64, max_length=64)
    policy_sha256: str = Field(min_length=64, max_length=64)
    attempt_sequence: int = Field(ge=1)
    provider_usage: ProductProviderUsage
    failure_code: Literal["provider_attempt_failed"] = (
        "provider_attempt_failed"
    )
    outcome_unknown: bool
    failed_at: datetime

    @property
    def attempt_sha256(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


class _ProductProviderUsageObserver(ProviderTransportAuditObserver):
    """Fence every provider send and retain safe aggregate usage only."""

    def __init__(
        self,
        context: WorkContext,
        *,
        source_fence: Callable[[], None] | None = None,
    ) -> None:
        self._context = context
        self._source_fence = source_fence
        self._call_count = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._request_ids_present = False
        self._failure_count = 0

    def before_request(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        attempt: int,
    ) -> None:
        del messages
        try:
            self._context.checkpoint(
                "product_provider_send",
                {
                    "model_sha256": stable_hash(model),
                    "attempt": attempt,
                },
            )
        except LeaseLostError as exc:
            raise ProductAgentLeaseLost(
                "Product lease was rejected before provider transport"
            ) from exc
        if self._source_fence is not None:
            self._source_fence()
        self._call_count += 1

    def after_response(
        self,
        *,
        model: str,
        attempt: int,
        status_code: int,
        request_id_present: bool,
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int,
    ) -> None:
        del model, attempt, status_code, latency_ms
        self._request_ids_present = (
            self._request_ids_present or request_id_present
        )
        self._input_tokens += max(0, input_tokens or 0)
        self._output_tokens += max(0, output_tokens or 0)

    def after_failure(
        self,
        *,
        model: str,
        attempt: int,
        error_type: str,
        latency_ms: int,
    ) -> None:
        del model, attempt, error_type, latency_ms
        self._failure_count += 1

    def snapshot(self) -> ProductProviderUsage:
        return ProductProviderUsage(
            call_count=self._call_count,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            request_ids_present=self._request_ids_present,
            failure_count=self._failure_count,
        )


class PreparedProductAgentArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "product_agent_prepared_attempt.v2",
        "product_agent_prepared_attempt.v3",
    ] = (
        "product_agent_prepared_attempt.v2"
    )
    product_attempt_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    night_episode_id: str = Field(min_length=1)
    night_episode_revision_id: str = Field(min_length=1)
    source_state_version: int = Field(ge=1)
    source_fact_snapshot_sha256: str = Field(min_length=64, max_length=64)
    policy_sha256: str = Field(min_length=64, max_length=64)
    analysis: AnalysisRevision
    role_runs: tuple[PreparedRoleRun, ...]
    desired_analysis_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    consumed_context_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    runtime_manifest_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    runtime_manifest: dict[str, Any] | None = None
    shared_analysis: SharedNightAnalysis | None = None
    projection_manifest_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    projection_manifest: dict[str, Any] | None = None
    projection_identities: dict[str, str] | None = None
    elder_narrative_manifest_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    elder_narrative_manifest: dict[str, Any] | None = None
    elder_narrative_identity_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    provider_usage: ProductProviderUsage = Field(
        default_factory=ProductProviderUsage
    )
    shadow_comparison: ReportShadowComparison | None = None
    prepared_at: datetime

    @model_validator(mode="after")
    def exact_three_role_result(self) -> "PreparedProductAgentArtifact":
        roles = tuple(item.role for item in self.role_runs)
        if set(roles) != set(AnalysisRole) or len(roles) != len(AnalysisRole):
            raise ValueError("prepared Product attempt requires exactly three roles")
        common_mismatch = any(
            item.role_view.analysis_revision_id
            != self.analysis.analysis_revision_id
            or item.role_view.night_episode_id
            != self.analysis.night_episode_id
            or item.role_view.night_episode_revision_id
            != self.night_episode_revision_id
            or item.role_view.subject_id != self.analysis.subject_id
            or item.role_view.data_mode != self.analysis.data_mode
            or item.role_view.role != item.role
            or item.role_view.product_agent_episode_id
            != item.product_episode_id
            for item in self.role_runs
        )
        if common_mismatch:
            raise ValueError("prepared role views are not bound to the analysis")
        if self.schema_version == "product_agent_prepared_attempt.v2":
            if any(
                item.result is None
                or item.role_projection is not None
                or item.personalization.habit_profile_hash
                != item.fact_snapshot.habit_profile_hash
                or tuple(
                    receipt.receipt_id
                    for receipt in item.personalization.memory_read_receipts
                )
                != item.fact_snapshot.memory_read_receipt_refs
                or item.result.receipt.episode_id != item.product_episode_id
                or item.result.receipt.fact_snapshot_id
                != item.fact_snapshot.fact_snapshot_id
                or item.result.receipt.fact_snapshot_hash
                != item.fact_snapshot.fact_snapshot_hash
                for item in self.role_runs
            ):
                raise ValueError("v2 role results are not bound to the analysis")
            if any(
                value is not None
                for value in (
                    self.desired_analysis_sha256,
                    self.consumed_context_sha256,
                    self.runtime_manifest_sha256,
                    self.runtime_manifest,
                    self.shared_analysis,
                    self.projection_manifest_sha256,
                    self.projection_manifest,
                    self.projection_identities,
                    self.elder_narrative_manifest_sha256,
                    self.elder_narrative_manifest,
                    self.elder_narrative_identity_sha256,
                    self.shadow_comparison,
                )
            ):
                raise ValueError("v2 attempts cannot contain shared analysis fields")
        else:
            if any(
                value is None
                for value in (
                    self.desired_analysis_sha256,
                    self.consumed_context_sha256,
                    self.runtime_manifest_sha256,
                    self.runtime_manifest,
                    self.shared_analysis,
                    self.projection_manifest_sha256,
                    self.projection_manifest,
                    self.projection_identities,
                    self.elder_narrative_manifest_sha256,
                    self.elder_narrative_manifest,
                    self.elder_narrative_identity_sha256,
                )
            ):
                raise ValueError("v3 attempt requires complete shared identity")
            assert self.shared_analysis is not None
            if any(
                item.result is not None
                or item.role_projection is None
                or item.role_projection.role.value != item.role.value
                or item.role_projection.source_shared_analysis_sha256
                != self.shared_analysis.shared_analysis_sha256
                or item.fact_snapshot.fact_snapshot_hash
                != self.shared_analysis.fact_snapshot_hash
                or item.fact_snapshot.memory_context_version != 0
                or bool(item.fact_snapshot.memory_read_receipt_refs)
                or bool(item.fact_snapshot.memory_read_receipt_hashes)
                for item in self.role_runs
            ):
                raise ValueError("v3 projections are not bound to one shared result")
            if len({item.product_episode_id for item in self.role_runs}) != 1:
                raise ValueError("v3 projections must share one Product Episode")
            if (
                self.shared_analysis.source.desired_analysis_sha256
                != self.desired_analysis_sha256
                or self.shared_analysis.source.consumed_context_sha256
                != self.consumed_context_sha256
                or self.shared_analysis.source.runtime_manifest_sha256
                != self.runtime_manifest_sha256
            ):
                raise ValueError("v3 shared identity drifted")
            elder = next(
                item
                for item in self.role_runs
                if item.role is AnalysisRole.ELDER
            )
            assert self.elder_narrative_manifest_sha256 is not None
            assert self.elder_narrative_manifest is not None
            assert self.elder_narrative_identity_sha256 is not None
            assert self.projection_manifest_sha256 is not None
            assert self.projection_manifest is not None
            assert self.projection_identities is not None
            assert elder.role_projection is not None
            if (
                stable_hash(self.projection_manifest)
                != self.projection_manifest_sha256
                or set(self.projection_identities)
                != {role.value for role in AnalysisRole}
                or any(
                    self.projection_identities[item.role.value]
                    != role_projection_identity_sha256(
                        desired_analysis_sha256=(
                            self.desired_analysis_sha256 or ""
                        ),
                        role=ReportRole(item.role.value),
                        projection_sha256=(
                            item.role_projection.projection_sha256
                            if item.role_projection is not None
                            else ""
                        ),
                        projection_manifest_sha256=(
                            self.projection_manifest_sha256
                        ),
                    )
                    for item in self.role_runs
                )
                or
                stable_hash(self.elder_narrative_manifest)
                != self.elder_narrative_manifest_sha256
                or _elder_narrative_identity_sha256(
                    shared_analysis_sha256=(
                        self.shared_analysis.shared_analysis_sha256
                    ),
                    elder_projection_sha256=(
                        self.projection_identities[AnalysisRole.ELDER.value]
                    ),
                    narrative_manifest_sha256=(
                        self.elder_narrative_manifest_sha256
                    ),
                )
                != self.elder_narrative_identity_sha256
            ):
                raise ValueError("v3 elder narrative identity drifted")
        expected_snapshot_hash = stable_hash(
            {
                item.role.value: item.fact_snapshot.fact_snapshot_hash
                for item in self.role_runs
            }
        )
        if expected_snapshot_hash != self.source_fact_snapshot_sha256:
            raise ValueError("prepared FactSnapshot set hash is inconsistent")
        return self

    @property
    def attempt_sha256(self) -> str:
        return stable_hash(self.model_dump(mode="json"))

    @model_serializer(mode="wrap")
    def serialize_attempt(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, Any]:
        payload = dict(handler(self))
        if self.schema_version == "product_agent_prepared_attempt.v2":
            for key in (
                "desired_analysis_sha256",
                "consumed_context_sha256",
                "runtime_manifest_sha256",
                "runtime_manifest",
                "shared_analysis",
                "projection_manifest_sha256",
                "projection_manifest",
                "projection_identities",
                "elder_narrative_manifest_sha256",
                "elder_narrative_manifest",
                "elder_narrative_identity_sha256",
                "provider_usage",
                "shadow_comparison",
            ):
                payload.pop(key, None)
        return payload


class PreparedElderNarrativeArtifact(BaseModel):
    """Query-invisible attempt for one independently reusable elder render."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["product_elder_narrative_prepared_attempt.v1"] = (
        "product_elder_narrative_prepared_attempt.v1"
    )
    product_attempt_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    night_episode_id: str = Field(min_length=1)
    night_episode_revision_id: str = Field(min_length=1)
    source_state_version: int = Field(ge=1)
    source_fact_snapshot_sha256: str = Field(min_length=64, max_length=64)
    policy_sha256: str = Field(min_length=64, max_length=64)
    shared_product_attempt_id: str = Field(min_length=1)
    shared_analysis_revision_id: str = Field(min_length=1)
    shared_analysis_sha256: str = Field(min_length=64, max_length=64)
    source_projection_sha256: str = Field(min_length=64, max_length=64)
    narrative_manifest_sha256: str = Field(min_length=64, max_length=64)
    narrative_manifest: dict[str, Any]
    render_identity_sha256: str = Field(min_length=64, max_length=64)
    narrative: ElderNarrative
    provider_usage: ProductProviderUsage = Field(
        default_factory=ProductProviderUsage
    )
    prepared_at: datetime

    @model_validator(mode="after")
    def validate_narrative_attempt(self) -> "PreparedElderNarrativeArtifact":
        if (
            self.narrative.source_shared_analysis_sha256
            != self.shared_analysis_sha256
            or self.narrative.source_projection_sha256
            != self.source_projection_sha256
            or self.narrative.render_identity_sha256
            != self.render_identity_sha256
            or stable_hash(self.narrative_manifest)
            != self.narrative_manifest_sha256
            or _elder_narrative_identity_sha256(
                shared_analysis_sha256=self.shared_analysis_sha256,
                elder_projection_sha256=self.source_projection_sha256,
                narrative_manifest_sha256=self.narrative_manifest_sha256,
            )
            != self.render_identity_sha256
        ):
            raise ValueError("elder narrative attempt identity drifted")
        return self

    @property
    def attempt_sha256(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class ProductAgentCommitResult:
    operation_id: str
    product_attempt_id: str
    analysis_revision_id: str
    role_view_ids: tuple[str, ...]
    analysis_status: str
    induction_operation_id: str
    induction_manifest_id: str
    elder_narrative_operation_id: str | None = None
    elder_narrative_operation_created: bool = False


@dataclass(frozen=True, slots=True)
class ProductElderNarrativeCommitResult:
    operation_id: str
    product_attempt_id: str
    state: str
    narrative: ElderNarrative
    provider_usage: ProductProviderUsage


@dataclass(frozen=True, slots=True)
class ProductReportRouteResult:
    """Safe worker result for a report request before shared analysis runs."""

    operation_id: str
    state: Literal["pending", "ready", "unusable_blocked", "urgent_handled"]
    shared_operation_id: str | None = None
    shared_operation_created: bool = False
    elder_narrative_operation_id: str | None = None
    elder_narrative_operation_created: bool = False


class ProductAgentProcessor:
    """Prepare outside a transaction, then publish under the current fence."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        runtime_bundle: ProductRuntimeBundle,
        report_pipeline_mode: ReportPipelineMode = ReportPipelineMode.SHARED_COMPAT,
        id_generator: Callable[[datetime | None], str] | None = None,
        now_factory: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        repository_factory: Callable[
            [TransactionBoundConnection, UowScope],
            "PostgresProductAgentRepository",
        ]
        | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.runtime_bundle = runtime_bundle
        self.report_pipeline_mode = report_pipeline_mode
        self.id_generator = id_generator or UUID7Generator()
        self.now_factory = now_factory
        self.repository_factory = repository_factory or (
            lambda connection, scope: PostgresProductAgentRepository(
                connection,
                scope,
                id_generator=self.id_generator,
            )
        )

    def process(
        self,
        scope: UowScope,
        lease: ProductAgentLease,
    ) -> ProductAgentCommitResult:
        source = self.load_source(scope, lease)

        prepared_at = self.now_factory()
        _require_aware(prepared_at, "prepared_at")
        artifact = self.prepare(
            scope=scope,
            source=source,
            lease=lease,
            prepared_at=prepared_at,
        )

        return self.persist_and_commit(
            scope,
            lease,
            artifact,
            source=source,
        )

    def load_source(
        self,
        scope: UowScope,
        lease: ProductAgentLease,
    ) -> LoadedProductAgentSource:
        """Fence briefly, then load the exact source without holding that lock."""

        _require_worker_subject_scope(scope)
        # Lease/fence validation is authoritative, but the operation-row lock
        # must not span the large immutable Episode membership load below.  A
        # later invocation reservation and dispatch permit revalidate this same
        # fence before any external transport can start.
        with self.uow_factory.begin(scope) as uow:
            repository = self.repository_factory(uow.connection, scope)
            repository.lock_source_lease(lease)
            uow.commit()
        with self.uow_factory.begin(scope) as uow:
            repository = self.repository_factory(uow.connection, scope)
            source = repository.load_source(lease)
            uow.commit()
        return source

    def revalidate_provider_source_fence(
        self,
        scope: UowScope,
        lease: ProductAgentLease,
        *,
        source: LoadedProductAgentSource,
        operation_type: str,
        committed_shared: PreparedProductAgentArtifact | None = None,
    ) -> None:
        """Revalidate the complete v3 source immediately before HTTP send."""

        checked_at = self.now_factory()
        _require_aware(checked_at, "provider_fence_checked_at")
        with self.uow_factory.begin(scope) as uow:
            repository = self.repository_factory(uow.connection, scope)
            habit_profile, memory_state = (
                repository.revalidate_provider_source_fence(
                    lease,
                    source=source,
                    operation_type=operation_type,
                )
            )
            if operation_type == PRODUCT_ELDER_NARRATIVE_OPERATION:
                if committed_shared is None:
                    raise ProductAgentInvariantError(
                        "narrative provider fence has no committed shared source"
                    )
                reread_shared = (
                    repository.load_committed_shared_for_narrative(
                        lease,
                        source=source,
                    )
                )
                if (
                    reread_shared.product_attempt_id
                    != committed_shared.product_attempt_id
                    or reread_shared.attempt_sha256
                    != committed_shared.attempt_sha256
                ):
                    raise ProductAgentStaleSource(
                        "narrative shared attempt changed before provider send"
                    )
            uow.commit()

        if operation_type not in {
            PRODUCT_SHARED_ANALYSIS_OPERATION,
            PRODUCT_ELDER_NARRATIVE_OPERATION,
        }:
            return
        current_source = replace(
            source,
            habit_profile=habit_profile,
            memory_state=memory_state,
        )
        runtime_manifest = _shared_runtime_manifest(
            self.runtime_bundle,
            source=current_source,
        )
        (
            consumed_context_sha256,
            runtime_manifest_sha256,
            desired_analysis_sha256,
        ) = _recompute_shared_identity(
            scope=scope,
            source=source,
            habit_profile=habit_profile,
            memory_state=memory_state,
            as_of=checked_at,
            runtime_manifest=runtime_manifest,
        )
        if operation_type == PRODUCT_SHARED_ANALYSIS_OPERATION:
            expected_context = _required_string(
                source.operation_json,
                "consumed_context_sha256",
            )
            expected_manifest = _required_string(
                source.operation_json,
                "runtime_manifest_sha256",
            )
            expected_desired = _required_string(
                source.operation_json,
                "desired_analysis_sha256",
            )
        else:
            assert committed_shared is not None
            expected_context = _required_optional_string(
                committed_shared.consumed_context_sha256,
                "committed shared consumed context",
            )
            expected_manifest = _required_optional_string(
                committed_shared.runtime_manifest_sha256,
                "committed shared runtime manifest",
            )
            expected_desired = _required_optional_string(
                committed_shared.desired_analysis_sha256,
                "committed shared desired analysis",
            )
            narrative_manifest = _elder_narrative_runtime_manifest(
                self.runtime_bundle,
                source=current_source,
            )
            if (
                stable_hash(narrative_manifest)
                != _required_string(
                    source.operation_json,
                    "narrative_manifest_sha256",
                )
                or narrative_manifest
                != source.operation_json.get("narrative_manifest")
            ):
                raise ProductAgentStaleSource(
                    "narrative manifest changed before provider send"
                )
        if (
            consumed_context_sha256 != expected_context
            or runtime_manifest_sha256 != expected_manifest
            or desired_analysis_sha256 != expected_desired
        ):
            raise ProductAgentStaleSource(
                "Product source/context/manifest changed before provider send"
            )

    def route_report_request(
        self,
        scope: UowScope,
        lease: ProductAgentLease,
    ) -> ProductReportRouteResult:
        """Gate one report request and converge it on canonical shared work."""

        if lease.operation_id == "":
            raise ProductAgentInvariantError("report request has no operation id")
        source = self.load_source(scope, lease)
        risk = source.facts.provider_risk_summary()
        quality = source.facts.provider_quality_summary()
        now = self.now_factory()
        _require_aware(now, "report_routed_at")
        if bool(risk.get("health_escalation_allowed")):
            state: Literal["urgent_handled", "unusable_blocked"] = (
                "urgent_handled"
            )
            with self.uow_factory.begin(scope) as uow:
                repository = self.repository_factory(uow.connection, scope)
                closed = repository.complete_report_request(
                    lease,
                    source=source,
                    state=state,
                    completed_at=now,
                )
                uow.commit()
            return ProductReportRouteResult(
                operation_id=source.operation_id,
                state=state if closed else "pending",
            )
        if source.facts.data_sufficiency not in {
            DataSufficiency.SUFFICIENT.value,
            DataSufficiency.PARTIAL.value,
        }:
            state = "unusable_blocked"
            with self.uow_factory.begin(scope) as uow:
                repository = self.repository_factory(uow.connection, scope)
                closed = repository.complete_report_request(
                    lease,
                    source=source,
                    state=state,
                    completed_at=now,
                )
                uow.commit()
            return ProductReportRouteResult(
                operation_id=source.operation_id,
                state=state if closed else "pending",
            )

        context_episode_id = (
            "shared-context:"
            f"{stable_hash({'revision': source.night_episode_revision_id})[:32]}"
        )
        personalization = _personalization_for_product_episode(
            scope=scope,
            source=source,
            product_episode_id=context_episode_id,
            as_of=now,
        )
        consumed_context_sha256 = _consumed_context_sha256(
            personalization,
            authorization_epoch=cast(int, scope.authorization_epoch),
            privacy_epoch=cast(int, scope.privacy_epoch),
            retrieval_policy_epoch=cast(int, scope.retrieval_policy_epoch),
        )
        runtime_manifest = _shared_runtime_manifest(
            self.runtime_bundle,
            source=source,
        )
        runtime_manifest_sha256 = stable_hash(runtime_manifest)
        projection_manifest = build_role_projection_runtime_manifest()
        projection_manifest_sha256 = stable_hash(projection_manifest)
        narrative_manifest = _elder_narrative_runtime_manifest(
            self.runtime_bundle,
            source=source,
        )
        narrative_manifest_sha256 = stable_hash(narrative_manifest)
        desired_analysis_sha256 = _desired_analysis_sha256(
            scope=scope,
            source=source,
            consumed_context_sha256=consumed_context_sha256,
            runtime_manifest_sha256=runtime_manifest_sha256,
        )
        with self.uow_factory.begin(scope) as uow:
            repository = self.repository_factory(uow.connection, scope)
            routed = repository.reserve_shared_analysis(
                lease,
                source=source,
                desired_analysis_sha256=desired_analysis_sha256,
                consumed_context_sha256=consumed_context_sha256,
                runtime_manifest=runtime_manifest,
                runtime_manifest_sha256=runtime_manifest_sha256,
                projection_manifest=projection_manifest,
                projection_manifest_sha256=projection_manifest_sha256,
                narrative_manifest=narrative_manifest,
                narrative_manifest_sha256=narrative_manifest_sha256,
                routed_at=now,
            )
            uow.commit()
        return routed

    def persist_and_commit(
        self,
        scope: UowScope,
        lease: ProductAgentLease,
        artifact: PreparedProductAgentArtifact,
        *,
        source: LoadedProductAgentSource | None = None,
    ) -> ProductAgentCommitResult:
        """Persist an invocation-journaled artifact, then CAS-publish it."""

        if (
            artifact.schema_version == "product_agent_prepared_attempt.v3"
            and source is None
        ):
            raise ProductAgentInvariantError(
                "v3 final commit requires its loaded source fence"
            )

        with self.uow_factory.begin(scope) as uow:
            repository = self.repository_factory(uow.connection, scope)
            repository.persist_prepared(lease, artifact)
            uow.commit()

        committed_at = self.now_factory()
        _require_aware(committed_at, "committed_at")
        runtime_manifest = (
            None
            if source is None
            else _shared_runtime_manifest(
                self.runtime_bundle,
                source=source,
            )
        )
        projection_manifest = (
            None
            if source is None
            else build_role_projection_runtime_manifest()
        )
        with self.uow_factory.begin(scope) as uow:
            repository = self.repository_factory(uow.connection, scope)
            result = repository.commit_prepared(
                lease,
                artifact,
                source=source,
                runtime_manifest=runtime_manifest,
                projection_manifest=projection_manifest,
                committed_at=committed_at,
            )
            uow.commit()
        return result

    def load_committed_shared_for_narrative(
        self,
        scope: UowScope,
        lease: ProductAgentLease,
        source: LoadedProductAgentSource,
    ) -> PreparedProductAgentArtifact:
        """Load the exact committed v3 artifact named by narrative work."""

        with self.uow_factory.begin(scope) as uow:
            repository = self.repository_factory(uow.connection, scope)
            artifact = repository.load_committed_shared_for_narrative(
                lease,
                source=source,
            )
            uow.commit()
        return artifact

    def persist_and_commit_elder_narrative(
        self,
        scope: UowScope,
        lease: ProductAgentLease,
        artifact: PreparedElderNarrativeArtifact,
        *,
        source: LoadedProductAgentSource,
        committed_shared: PreparedProductAgentArtifact,
    ) -> ProductElderNarrativeCommitResult:
        """Durably stage, then publish only the elder narrative attempt."""

        with self.uow_factory.begin(scope) as uow:
            repository = self.repository_factory(uow.connection, scope)
            repository.persist_elder_narrative_prepared(lease, artifact)
            uow.commit()

        committed_at = self.now_factory()
        _require_aware(committed_at, "elder_narrative_committed_at")
        shared_runtime_manifest = _shared_runtime_manifest(
            self.runtime_bundle,
            source=source,
        )
        narrative_manifest = _elder_narrative_runtime_manifest(
            self.runtime_bundle,
            source=source,
        )
        projection_manifest = build_role_projection_runtime_manifest()
        with self.uow_factory.begin(scope) as uow:
            repository = self.repository_factory(uow.connection, scope)
            result = repository.commit_elder_narrative_prepared(
                lease,
                artifact,
                source=source,
                committed_shared=committed_shared,
                shared_runtime_manifest=shared_runtime_manifest,
                projection_manifest=projection_manifest,
                narrative_manifest=narrative_manifest,
                committed_at=committed_at,
            )
            uow.commit()
        return result

    def fail_compatibility_bridge(
        self,
        scope: UowScope,
        lease: ProductAgentLease,
        *,
        operation_type: str,
        error_code: str,
        outcome_unknown: bool = False,
    ) -> None:
        """Fail only automatic legacy wrappers linked to terminally failed work."""

        if operation_type not in {
            PRODUCT_REPORT_RUN_OPERATION,
            PRODUCT_SHARED_ANALYSIS_OPERATION,
        }:
            return
        failed_at = self.now_factory()
        _require_aware(failed_at, "compatibility_failed_at")
        with self.uow_factory.begin(scope) as uow:
            repository = self.repository_factory(uow.connection, scope)
            repository.fail_compatibility_bridge(
                lease,
                operation_type=operation_type,
                error_code=error_code,
                outcome_unknown=outcome_unknown,
                failed_at=failed_at,
            )
            uow.commit()

    def persist_failed_provider_attempt(
        self,
        scope: UowScope,
        lease: ProductAgentLease,
        *,
        source: LoadedProductAgentSource,
        operation_type: str,
        provider_usage: ProductProviderUsage,
    ) -> None:
        """Retain safe usage when provider work produced no artifact."""

        if operation_type not in {
            PRODUCT_SHARED_ANALYSIS_OPERATION,
            PRODUCT_ELDER_NARRATIVE_OPERATION,
        }:
            return
        failed_at = self.now_factory()
        _require_aware(failed_at, "provider_attempt_failed_at")
        identity_value = source.operation_json.get(
            "desired_analysis_sha256"
            if operation_type == PRODUCT_SHARED_ANALYSIS_OPERATION
            else "render_identity_sha256"
        )
        if not isinstance(identity_value, str) or not identity_value:
            raise ProductAgentInvariantError(
                "failed Product provider attempt has no semantic identity"
            )
        failure_identity = stable_hash(
            {
                "schema_version": "product_provider_failed_attempt_identity.v1",
                "operation_id": lease.operation_id,
                "attempt_sequence": lease.attempt_sequence,
                "operation_type": operation_type,
            }
        )
        attempt = FailedProductProviderAttempt(
            product_attempt_id=f"product-provider-failure:{failure_identity}",
            operation_id=lease.operation_id,
            operation_type=cast(Any, operation_type),
            night_episode_revision_id=source.night_episode_revision_id,
            source_state_version=source.night_episode_revision_number,
            source_fact_snapshot_sha256=stable_hash(
                {
                    "schema_version": "failed_product_source.v1",
                    "night_episode_revision_id": (
                        source.night_episode_revision_id
                    ),
                    "observation_set_sha256": source.observation_set_sha256,
                    "semantic_identity_sha256": identity_value,
                }
            ),
            policy_sha256=source.policy_sha256,
            attempt_sequence=lease.attempt_sequence,
            provider_usage=provider_usage,
            outcome_unknown=provider_usage.call_count > 0,
            failed_at=failed_at,
        )
        with self.uow_factory.begin(scope) as uow:
            repository = self.repository_factory(uow.connection, scope)
            repository.persist_failed_provider_attempt(lease, attempt)
            uow.commit()

    def prepare(
        self,
        *,
        scope: UowScope,
        source: LoadedProductAgentSource,
        lease: ProductAgentLease,
        prepared_at: datetime,
    ) -> PreparedProductAgentArtifact:
        if source.subject_id != scope.subject_id:
            raise ProductAgentInvariantError("Product source subject mismatch")
        runner = self.runtime_bundle.runner
        analysis_id = self.id_generator(prepared_at)
        role_material: list[
            tuple[
                AnalysisRole,
                str,
                FactSnapshot,
                ProductEpisodeRunResult,
            ]
        ] = []
        personalization_by_episode: dict[
            str, PinnedPersonalizationContext
        ] = {}
        for role in AnalysisRole:
            product_episode_id = self.id_generator(prepared_at)
            personalization = _personalization_for_product_episode(
                scope=scope,
                source=source,
                product_episode_id=product_episode_id,
                as_of=prepared_at,
            )
            personalization_by_episode[product_episode_id] = personalization
            snapshot = _fact_snapshot_for_role(
                scope=scope,
                source=source,
                role=role,
                fact_snapshot_id=self.id_generator(prepared_at),
                created_at=prepared_at,
                personalization=personalization,
            )
            request = ProductEpisodeRunRequest(
                episode_id=product_episode_id,
                episode_type=EpisodeType.MORNING_REVIEW,
                objective=(
                    "基于一个精确 NightEpisode revision 和已授权 Canonical "
                    f"Observations，为 {role.value} 生成受证据约束的"
                    "睡眠照护视图。"
                ),
                fact_snapshot=snapshot,
                audience_role=role.value,
                tool_inputs=source.facts.tool_inputs(),
                personalized=True,
                doctor_material=role == AnalysisRole.DOCTOR,
                idempotency_key=f"{source.operation_id}:{role.value}",
                personalization=personalization,
            )
            result = runner.run(request)
            role_material.append(
                (role, product_episode_id, snapshot, result)
            )

        all_ready = all(
            result.receipt.status == EpisodeStatus.COMPLETE
            and result.receipt.execution_mode == ExecutionMode.INTELLIGENT
            and result.publication is not None
            and result.publication.audience_role == role.value
            for role, _episode_id, _snapshot, result in role_material
        )
        failure_codes = tuple(
            sorted(
                {
                    code
                    for _role, _episode_id, _snapshot, result in role_material
                    for code in result.receipt.failure_codes
                }
            )
        )
        if not all_ready and not failure_codes:
            failure_codes = ("ROLE_VIEW_DEGRADED",)
        analysis = AnalysisRevision(
            analysis_revision_id=analysis_id,
            night_episode_id=source.night_episode_id,
            night_episode_revision_id=source.night_episode_revision_id,
            night_episode_revision_number=source.night_episode_revision_number,
            data_mode=DataMode(scope.data_mode),
            subject_id=source.subject_id,
            revision_number=source.next_analysis_revision_number,
            parent_analysis_revision_id=source.previous_analysis_revision_id,
            analysis_run_id=next(
                item[1] for item in role_material if item[0] == AnalysisRole.ELDER
            ),
            observation_set_sha256=source.observation_set_sha256,
            adapter_versions=source.adapter_versions,
            observation_schema_versions=source.observation_schema_versions,
            policy_versions=source.policy_versions,
            skill_versions=_skill_versions(role_material),
            model_versions=_model_versions(role_material),
            data_sufficiency=DataSufficiency(source.facts.data_sufficiency),
            status=(AnalysisStatus.READY if all_ready else AnalysisStatus.DEGRADED),
            execution_mode=("intelligent" if all_ready else "safe_degraded"),
            failure_codes=failure_codes,
            result_resource_id=analysis_id,
            created_at=prepared_at,
        )
        role_runs = tuple(
            PreparedRoleRun(
                role=role,
                product_episode_id=product_episode_id,
                fact_snapshot=snapshot,
                personalization=personalization_by_episode[product_episode_id],
                result=result,
                role_view=_role_view_from_result(
                    analysis=analysis,
                    role=role,
                    product_episode_id=product_episode_id,
                    result=result,
                    source_refs=source.facts.provenance_references,
                    role_view_id=self.id_generator(prepared_at),
                    generated_at=prepared_at,
                ),
            )
            for role, product_episode_id, snapshot, result in role_material
        )
        snapshot_set_hash = stable_hash(
            {
                item.role.value: item.fact_snapshot.fact_snapshot_hash
                for item in role_runs
            }
        )
        return PreparedProductAgentArtifact(
            product_attempt_id=self.id_generator(prepared_at),
            operation_id=source.operation_id,
            night_episode_id=source.night_episode_id,
            night_episode_revision_id=source.night_episode_revision_id,
            source_state_version=source.night_episode_revision_number,
            source_fact_snapshot_sha256=snapshot_set_hash,
            policy_sha256=source.policy_sha256,
            analysis=analysis,
            role_runs=role_runs,
            prepared_at=prepared_at,
        )

    def prepare_shared(
        self,
        *,
        scope: UowScope,
        source: LoadedProductAgentSource,
        lease: ProductAgentLease,
        prepared_at: datetime,
    ) -> PreparedProductAgentArtifact:
        """Prepare one role-neutral analysis and three deterministic views."""

        if source.subject_id != scope.subject_id:
            raise ProductAgentInvariantError("shared Product source subject mismatch")
        if source.facts.data_sufficiency not in {
            DataSufficiency.SUFFICIENT.value,
            DataSufficiency.PARTIAL.value,
        } or bool(
            source.facts.provider_risk_summary().get(
                "health_escalation_allowed"
            )
        ):
            raise ProductAgentInvariantError(
                "shared analysis bypassed the authoritative report gate"
            )
        desired_analysis_sha256 = _required_string(
            source.operation_json,
            "desired_analysis_sha256",
        )
        expected_context_sha256 = _required_string(
            source.operation_json,
            "consumed_context_sha256",
        )
        expected_runtime_sha256 = _required_string(
            source.operation_json,
            "runtime_manifest_sha256",
        )
        shared_episode_id = (
            "product-shared:"
            f"{desired_analysis_sha256[:32]}"
        )
        personalization = _personalization_for_product_episode(
            scope=scope,
            source=source,
            product_episode_id=shared_episode_id,
            as_of=prepared_at,
        )
        consumed_context_sha256 = _consumed_context_sha256(
            personalization,
            authorization_epoch=cast(int, scope.authorization_epoch),
            privacy_epoch=cast(int, scope.privacy_epoch),
            retrieval_policy_epoch=cast(int, scope.retrieval_policy_epoch),
        )
        runtime_manifest = _shared_runtime_manifest(
            self.runtime_bundle,
            source=source,
        )
        runtime_manifest_sha256 = stable_hash(runtime_manifest)
        if (
            consumed_context_sha256 != expected_context_sha256
            or runtime_manifest_sha256 != expected_runtime_sha256
            or _desired_analysis_sha256(
                scope=scope,
                source=source,
                consumed_context_sha256=consumed_context_sha256,
                runtime_manifest_sha256=runtime_manifest_sha256,
            )
            != desired_analysis_sha256
        ):
            raise ProductAgentStaleSource(
                "shared analysis context or runtime pins changed before execution"
            )
        snapshot = _fact_snapshot_for_shared(
            scope=scope,
            source=source,
            fact_snapshot_id=(
                "fact-snapshot:shared:"
                f"{desired_analysis_sha256[:32]}"
            ),
            created_at=prepared_at,
            consumed_context_sha256=consumed_context_sha256,
            personalization=personalization,
        )
        quality = source.facts.provider_quality_summary()
        risk = source.facts.provider_risk_summary()
        is_partial = (
            source.facts.data_sufficiency == DataSufficiency.PARTIAL.value
        )
        partial_caveat = (
            "部分时段数据覆盖不足；结论仅限于可用观测，需结合后续夜晚复核。"
            if is_partial
            else None
        )
        source_contract = SharedAnalysisSourceV1(
            night_episode_id=source.night_episode_id,
            night_episode_revision_id=source.night_episode_revision_id,
            night_episode_revision_number=source.night_episode_revision_number,
            wake_date=date.fromisoformat(source.facts.local_sleep_date),
            observation_set_sha256=source.observation_set_sha256,
            canonical_data_version=source.facts.canonical_data_version,
            desired_analysis_sha256=desired_analysis_sha256,
            consumed_context_sha256=consumed_context_sha256,
            runtime_manifest_sha256=runtime_manifest_sha256,
            data_sufficiency=cast(
                Literal["sufficient", "partial"],
                source.facts.data_sufficiency,
            ),
            quality_state=("partial" if is_partial else "good"),
            risk_state=str(risk.get("risk_state") or "unknown"),
            quality_reason_codes=tuple(
                str(item) for item in quality.get("reason_codes", ())
            ),
            risk_reason_codes=tuple(
                str(item) for item in risk.get("reason_codes", ())
            ),
            limitations=tuple(
                dict.fromkeys(
                    (
                        "仅作睡眠健康观察参考，不构成诊断。",
                        *(
                            ("数据覆盖不完整。",)
                            if is_partial
                            else ()
                        ),
                    )
                )
            ),
            partial_caveat=partial_caveat,
            reporting_context=ReportingContextV1(
                timezone_name=source.facts.timezone_name,
                locale="zh-CN",
                audience="shared",
                authoritative_start_at_utc=(
                    source.episode.bed_at
                    or source.episode.collection_start_at
                ).astimezone(timezone.utc),
                authoritative_end_at_utc=(
                    source.episode.wake_at
                    or source.episode.deterministic_close_deadline_at
                ).astimezone(timezone.utc),
                local_sleep_date=date.fromisoformat(
                    source.facts.local_sleep_date
                ),
                renderer_version="shared_semantic_facts.v1",
            ),
        )
        runtime_request = ProductEpisodeRunRequest(
            episode_id=shared_episode_id,
            episode_type=EpisodeType.MORNING_REVIEW,
            objective=(
                "基于一个精确 NightEpisode revision 和已授权 Canonical "
                "Observations，生成一次角色中立、受证据约束的睡眠分析。"
            ),
            fact_snapshot=snapshot,
            audience_role=None,
            tool_inputs=source.facts.tool_inputs(),
            personalized=True,
            doctor_material=False,
            idempotency_key=desired_analysis_sha256,
            personalization=personalization,
            personalization_projection_version="selected_stable.v1",
        )
        shared_analysis = self.runtime_bundle.runner.analyze_shared(
            SharedAnalysisRunRequest(
                source=source_contract,
                runtime_request=runtime_request,
            )
        )
        elder_message_atoms = build_elder_message_atoms(
            shared_analysis,
            source.facts.elder_presentation_facts(),
        )
        projections = build_shared_role_projections(
            shared_analysis,
            elder_message_atoms=elder_message_atoms,
        )
        projection_manifest = build_role_projection_runtime_manifest()
        projection_manifest_sha256 = stable_hash(projection_manifest)
        projection_identities = {
            projection.role.value: role_projection_identity_sha256(
                desired_analysis_sha256=desired_analysis_sha256,
                role=projection.role,
                projection_sha256=projection.projection_sha256,
                projection_manifest_sha256=projection_manifest_sha256,
            )
            for projection in projections
        }
        elder_projection = next(
            item for item in projections if item.role is ReportRole.ELDER
        )
        elder_narrative_manifest = _elder_narrative_runtime_manifest(
            self.runtime_bundle,
            source=source,
        )
        elder_narrative_manifest_sha256 = stable_hash(
            elder_narrative_manifest
        )
        elder_narrative_identity_sha256 = _elder_narrative_identity_sha256(
            shared_analysis_sha256=shared_analysis.shared_analysis_sha256,
            elder_projection_sha256=projection_identities[
                AnalysisRole.ELDER.value
            ],
            narrative_manifest_sha256=elder_narrative_manifest_sha256,
        )
        analysis_id = self.id_generator(prepared_at)
        model_versions = {
            f"shared:{invocation.agent_id.value}": invocation.model_id
            for invocation in shared_analysis.agent_invocations
        }
        skill_versions = {
            f"shared:{invocation.agent_id.value}": invocation.skill_version
            for invocation in shared_analysis.agent_invocations
        }
        analysis = AnalysisRevision(
            analysis_revision_id=analysis_id,
            night_episode_id=source.night_episode_id,
            night_episode_revision_id=source.night_episode_revision_id,
            night_episode_revision_number=source.night_episode_revision_number,
            data_mode=DataMode(scope.data_mode),
            subject_id=source.subject_id,
            revision_number=source.next_analysis_revision_number,
            parent_analysis_revision_id=source.previous_analysis_revision_id,
            analysis_run_id=shared_episode_id,
            observation_set_sha256=source.observation_set_sha256,
            adapter_versions=source.adapter_versions,
            observation_schema_versions=source.observation_schema_versions,
            policy_versions=source.policy_versions,
            skill_versions=skill_versions,
            model_versions=model_versions,
            data_sufficiency=DataSufficiency(source.facts.data_sufficiency),
            status=AnalysisStatus.READY,
            execution_mode="intelligent",
            failure_codes=(),
            result_resource_id=analysis_id,
            created_at=prepared_at,
        )
        role_runs = tuple(
            PreparedRoleRun(
                role=AnalysisRole(projection.role.value),
                product_episode_id=shared_episode_id,
                fact_snapshot=snapshot,
                personalization=personalization,
                role_projection=projection,
                role_view=_role_view_from_projection(
                    analysis=analysis,
                    projection=projection,
                    product_episode_id=shared_episode_id,
                    role_view_id=self.id_generator(prepared_at),
                    generated_at=prepared_at,
                ),
            )
            for projection in projections
        )
        snapshot_set_hash = stable_hash(
            {
                item.role.value: item.fact_snapshot.fact_snapshot_hash
                for item in role_runs
            }
        )
        artifact = PreparedProductAgentArtifact(
            schema_version="product_agent_prepared_attempt.v3",
            product_attempt_id=self.id_generator(prepared_at),
            operation_id=source.operation_id,
            night_episode_id=source.night_episode_id,
            night_episode_revision_id=source.night_episode_revision_id,
            source_state_version=source.night_episode_revision_number,
            source_fact_snapshot_sha256=snapshot_set_hash,
            policy_sha256=source.policy_sha256,
            analysis=analysis,
            role_runs=role_runs,
            desired_analysis_sha256=desired_analysis_sha256,
            consumed_context_sha256=consumed_context_sha256,
            runtime_manifest_sha256=runtime_manifest_sha256,
            runtime_manifest=runtime_manifest,
            shared_analysis=shared_analysis,
            projection_manifest_sha256=projection_manifest_sha256,
            projection_manifest=projection_manifest,
            projection_identities=projection_identities,
            elder_narrative_manifest_sha256=(
                elder_narrative_manifest_sha256
            ),
            elder_narrative_manifest=elder_narrative_manifest,
            elder_narrative_identity_sha256=(
                elder_narrative_identity_sha256
            ),
            prepared_at=prepared_at,
        )
        if self.report_pipeline_mode is ReportPipelineMode.SHADOW:
            legacy_artifact = self.prepare(
                scope=scope,
                source=source,
                lease=lease,
                prepared_at=prepared_at,
            )
            artifact = artifact.model_copy(
                update={
                    "shadow_comparison": build_report_shadow_comparison(
                        legacy=legacy_artifact,
                        shared=artifact,
                        source=source,
                    )
                }
            )
        return artifact

    def prepare_elder_narrative(
        self,
        *,
        scope: UowScope,
        source: LoadedProductAgentSource,
        lease: ProductAgentLease,
        committed_shared: PreparedProductAgentArtifact,
        prepared_at: datetime,
    ) -> PreparedElderNarrativeArtifact:
        """Render once from a committed shared result and elder projection."""

        del lease
        if source.subject_id != scope.subject_id:
            raise ProductAgentInvariantError(
                "elder narrative Product source subject mismatch"
            )
        if (
            committed_shared.schema_version
            != "product_agent_prepared_attempt.v3"
            or committed_shared.shared_analysis is None
            or committed_shared.night_episode_id != source.night_episode_id
            or committed_shared.night_episode_revision_id
            != source.night_episode_revision_id
        ):
            raise ProductAgentStaleSource(
                "elder narrative source is not the current committed v3 analysis"
            )
        elder_run = next(
            item
            for item in committed_shared.role_runs
            if item.role is AnalysisRole.ELDER
        )
        projection_value = source.operation_json.get("elder_projection")
        if not isinstance(projection_value, Mapping):
            raise ProductAgentInvariantError(
                "elder narrative operation has no deterministic projection"
            )
        elder_projection = RoleProjection.model_validate(projection_value)
        projection_identity_sha256 = _required_string(
            source.operation_json,
            "elder_projection_sha256",
        )
        projection_manifest_sha256 = _required_string(
            source.operation_json,
            "projection_manifest_sha256",
        )
        if (
            elder_projection.role is not ReportRole.ELDER
            or elder_projection.source_shared_analysis_sha256
            != committed_shared.shared_analysis.shared_analysis_sha256
            or elder_projection.projection_sha256
            != _required_string(
                source.operation_json,
                "elder_projection_content_sha256",
            )
            or projection_identity_sha256
            != role_projection_identity_sha256(
                desired_analysis_sha256=_required_optional_string(
                    committed_shared.desired_analysis_sha256,
                    "committed shared desired analysis",
                ),
                role=ReportRole.ELDER,
                projection_sha256=elder_projection.projection_sha256,
                projection_manifest_sha256=projection_manifest_sha256,
            )
        ):
            raise ProductAgentStaleSource(
                "elder narrative projection identity changed"
            )
        expected_manifest_sha256 = _required_string(
            source.operation_json,
            "narrative_manifest_sha256",
        )
        expected_render_identity = _required_string(
            source.operation_json,
            "render_identity_sha256",
        )
        narrative_manifest = _elder_narrative_runtime_manifest(
            self.runtime_bundle,
            source=source,
        )
        narrative_manifest_sha256 = stable_hash(narrative_manifest)
        render_identity_sha256 = _elder_narrative_identity_sha256(
            shared_analysis_sha256=(
                committed_shared.shared_analysis.shared_analysis_sha256
            ),
            elder_projection_sha256=projection_identity_sha256,
            narrative_manifest_sha256=narrative_manifest_sha256,
        )
        if (
            narrative_manifest_sha256 != expected_manifest_sha256
            or render_identity_sha256 != expected_render_identity
            or narrative_manifest
            != source.operation_json.get("narrative_manifest")
        ):
            raise ProductAgentStaleSource(
                "elder narrative runtime pins changed before execution"
            )
        runtime_request = ProductEpisodeRunRequest(
            episode_id=f"product-elder:{render_identity_sha256[:32]}",
            episode_type=EpisodeType.MORNING_REVIEW,
            objective=(
                "仅将已提交的共享睡眠分析和确定性 elder 投影渲染为受约束叙述；"
                "不得新增事实、建议或安全结论。"
            ),
            fact_snapshot=elder_run.fact_snapshot,
            audience_role=AnalysisRole.ELDER.value,
            tool_inputs={},
            # The accepted shared result already binds selected Habit/Memory
            # meaning.  Rendering must not re-project receipt/global identity
            # or make a retry's provider input depend on fresh timestamps.
            personalized=False,
            doctor_material=False,
            idempotency_key=render_identity_sha256,
            personalization=None,
            personalization_projection_version="selected_stable.v1",
        )
        atom_values = source.operation_json.get("elder_message_atoms")
        if not isinstance(atom_values, list) or not atom_values:
            raise ProductAgentInvariantError(
                "elder narrative operation has no typed message atoms"
            )
        message_atoms = tuple(
            ElderMessageAtom.model_validate(item) for item in atom_values
        )
        narrative_request = ElderNarrativeRequest.create(
            runtime_request=runtime_request,
            shared_analysis=committed_shared.shared_analysis,
            elder_projection=elder_projection,
            message_atoms=message_atoms,
            render_manifest_sha256=narrative_manifest_sha256,
            source_projection_identity_sha256=(
                projection_identity_sha256
            ),
        )
        narrative = self.runtime_bundle.runner.render_elder_narrative(
            narrative_request
        )
        return PreparedElderNarrativeArtifact(
            product_attempt_id=self.id_generator(prepared_at),
            operation_id=source.operation_id,
            night_episode_id=source.night_episode_id,
            night_episode_revision_id=source.night_episode_revision_id,
            source_state_version=source.night_episode_revision_number,
            source_fact_snapshot_sha256=(
                committed_shared.shared_analysis.fact_snapshot_hash
            ),
            policy_sha256=source.policy_sha256,
            shared_product_attempt_id=(
                committed_shared.product_attempt_id
            ),
            shared_analysis_revision_id=(
                committed_shared.analysis.analysis_revision_id
            ),
            shared_analysis_sha256=(
                committed_shared.shared_analysis.shared_analysis_sha256
            ),
            source_projection_sha256=projection_identity_sha256,
            narrative_manifest_sha256=narrative_manifest_sha256,
            narrative_manifest=narrative_manifest,
            render_identity_sha256=render_identity_sha256,
            narrative=narrative,
            prepared_at=prepared_at,
        )


class PostgresProductAgentRepository:
    """SQL adapter bound to one caller-owned Product Agent transaction."""

    def __init__(
        self,
        connection: TransactionBoundConnection,
        scope: UowScope,
        *,
        id_generator: Callable[[datetime | None], str] | None = None,
    ) -> None:
        self.connection = connection
        self.scope = scope
        self.id_generator = id_generator or UUID7Generator()

    def lock_source_lease(self, lease: ProductAgentLease) -> None:
        """Lock and validate only the durable source pointer and current fence."""

        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                SELECT 1
                FROM public.sleep_domain_operations AS operation
                JOIN public.sleep_domain_night_episodes AS episode
                  ON episode.night_episode_id = CASE
                       WHEN operation.operation_type =
                            'product.elder_narrative.v1'
                       THEN operation.operation_json ->> 'night_episode_id'
                       ELSE operation.target_resource_id
                     END
                 AND episode.namespace_id = operation.namespace_id
                 AND episode.data_mode = operation.data_mode
                 AND episode.namespace_generation = operation.namespace_generation
                 AND episode.subject_id = operation.subject_id
                WHERE operation.operation_id = %s
                  AND operation.namespace_id = %s
                  AND operation.data_mode = %s
                  AND operation.namespace_generation = %s
                  AND operation.subject_id = %s
                  AND COALESCE(operation.run_id, '') = COALESCE(%s, '')
                  AND COALESCE(operation.arm_id, '') = COALESCE(%s, '')
                  AND operation.operation_type IN (
                    'product_agent',
                    'product.report.run.v1',
                    'product.shared_analysis.v1',
                    'product.elder_narrative.v1'
                  )
                  AND operation.queue_name = 'product_agent'
                  AND operation.status = 'running'
                  AND operation.lease_generation = %s
                  AND operation.fencing_token = %s
                  AND operation.worker_instance = %s
                  AND operation.lease_expires_at > clock_timestamp()
                  AND episode.current_revision_id =
                       operation.operation_json ->> 'night_episode_revision_id'
                  AND episode.date_state = 'finalized'
                  AND episode.date_conflict = FALSE
                FOR UPDATE OF operation
                """,
                self._lease_scope_params(lease),
            )
            if cursor.fetchone() is None:
                raise ProductAgentLeaseLost(
                    "Product operation fence or source revision was rejected"
                )
        finally:
            cursor.close()

    def load_source(self, lease: ProductAgentLease) -> LoadedProductAgentSource:
        """Load a pinned immutable source without retaining the operation lock."""

        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                SELECT operation.operation_json, operation.subject_id,
                       operation.policy_sha256, episode.night_episode_id,
                       episode.episode_json, episode.current_revision_id,
                       revision.revision_json, revision.revision_number,
                       quality.assessment_json, risk.risk_json
                FROM public.sleep_domain_operations AS operation
                JOIN public.sleep_domain_night_episodes AS episode
                  ON episode.night_episode_id = CASE
                       WHEN operation.operation_type =
                            'product.elder_narrative.v1'
                       THEN operation.operation_json ->> 'night_episode_id'
                       ELSE operation.target_resource_id
                     END
                 AND episode.namespace_id = operation.namespace_id
                 AND episode.data_mode = operation.data_mode
                 AND episode.namespace_generation = operation.namespace_generation
                 AND episode.subject_id = operation.subject_id
                JOIN public.sleep_domain_night_episode_revisions AS revision
                  ON revision.night_episode_revision_id =
                       operation.operation_json ->> 'night_episode_revision_id'
                 AND revision.night_episode_id = episode.night_episode_id
                 AND revision.namespace_id = episode.namespace_id
                 AND revision.data_mode = episode.data_mode
                 AND revision.namespace_generation = episode.namespace_generation
                 AND revision.subject_id = episode.subject_id
                JOIN public.sleep_domain_current_quality AS quality
                  ON quality.namespace_id = episode.namespace_id
                 AND quality.data_mode = episode.data_mode
                 AND quality.subject_id = episode.subject_id
                 AND quality.night_episode_id = episode.night_episode_id
                 AND quality.assessment_id =
                       operation.operation_json ->> 'quality_assessment_id'
                JOIN public.sleep_domain_current_risk AS risk
                  ON risk.namespace_id = episode.namespace_id
                 AND risk.data_mode = episode.data_mode
                 AND risk.subject_id = episode.subject_id
                 AND risk.night_episode_id = episode.night_episode_id
                 AND risk.current_risk_id =
                       operation.operation_json ->> 'current_risk_id'
                WHERE operation.operation_id = %s
                  AND operation.namespace_id = %s
                  AND operation.data_mode = %s
                  AND operation.namespace_generation = %s
                  AND operation.subject_id = %s
                  AND COALESCE(operation.run_id, '') = COALESCE(%s, '')
                  AND COALESCE(operation.arm_id, '') = COALESCE(%s, '')
                  AND operation.operation_type IN (
                    'product_agent',
                    'product.report.run.v1',
                    'product.shared_analysis.v1',
                    'product.elder_narrative.v1'
                  )
                  AND operation.queue_name = 'product_agent'
                  AND operation.status = 'running'
                  AND operation.lease_generation = %s
                  AND operation.fencing_token = %s
                  AND operation.worker_instance = %s
                  AND operation.lease_expires_at > clock_timestamp()
                  AND episode.current_revision_id =
                       operation.operation_json ->> 'night_episode_revision_id'
                  AND episode.date_state = 'finalized'
                  AND episode.date_conflict = FALSE
                """,
                self._lease_scope_params(lease),
            )
            row = cursor.fetchone()
            if row is None:
                raise ProductAgentLeaseLost(
                    "Product operation fence or source revision was rejected"
                )
            operation_json = _json_value(row[0])
            subject_id = str(row[1])
            policy_sha256 = str(row[2])
            night_episode_id = str(row[3])
            episode = NightEpisodeV2.model_validate(_json_value(row[4]))
            current_revision_id = str(row[5])
            revision_payload = _json_value(row[6])
            revision_number = int(row[7])
            quality = DeterministicQualityAssessment.model_validate(
                _json_value(row[8])
            )
            risk = CurrentRisk.model_validate(_json_value(row[9]))
            pinned_revision_id = _required_string(
                operation_json,
                "night_episode_revision_id",
            )
            if current_revision_id != pinned_revision_id:
                raise ProductAgentStaleSource(
                    "Product operation targets a superseded Episode revision"
                )
            if len(policy_sha256) != 64:
                raise ProductAgentInvariantError(
                    "Product operation has no frozen policy hash"
                )
            observation_ids = tuple(
                str(value)
                for value in revision_payload.get("observation_ids", ())
            )
            if not observation_ids or len(observation_ids) != len(set(observation_ids)):
                raise ProductAgentInvariantError(
                    "Product source revision has no exact observation set"
                )
            _require_exact_fast_path_source(
                pinned_revision_id=pinned_revision_id,
                observation_ids=observation_ids,
                quality=quality,
                risk=risk,
            )
            cursor.execute(
                """
                SELECT observation_id, observation_json
                FROM public.sleep_domain_canonical_observations
                WHERE namespace_id = %s AND data_mode = %s
                  AND subject_id = %s AND observation_id = ANY(%s)
                """,
                (
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.subject_id,
                    list(observation_ids),
                ),
            )
            observation_rows = cursor.fetchall()
            observations_by_id = {
                str(item[0]): SleepObservation.model_validate(_json_value(item[1]))
                for item in observation_rows
            }
            if set(observations_by_id) != set(observation_ids):
                raise ProductAgentInvariantError(
                    "Product source revision has missing canonical observations"
                )
            observations = tuple(
                observations_by_id[observation_id]
                for observation_id in observation_ids
            )
            cursor.execute(
                """
                SELECT observation_id, schema_version, semantics_version,
                       metric_id, semantic_payload_json, canonical_unit,
                       occurred_at, aggregation_start_at, aggregation_end_at,
                       source_kind, vendor_semantic_code, ontology_version,
                       normalizer_version, semantic_identity,
                       trusted_for_analytics, upcast_status,
                       classification_evidence
                FROM public.sleep_domain_observation_semantics_v2
                WHERE namespace_id = %s AND data_mode = %s
                  AND subject_id = %s AND observation_id = ANY(%s)
                """,
                (
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.subject_id,
                    list(observation_ids),
                ),
            )
            semantics_by_observation: dict[str, dict[str, Any]] = {}
            for row in cursor.fetchall():
                semantic_payload = _json_value(row[4])
                semantics_by_observation[str(row[0])] = {
                    "schema_version": str(row[1]),
                    "semantics_version": str(row[2]),
                    "metric_id": str(row[3]),
                    "value": semantic_payload.get("value"),
                    "unit": None if row[5] is None else str(row[5]),
                    "occurred_at": row[6].isoformat(),
                    "aggregation_start_at": (
                        None if row[7] is None else row[7].isoformat()
                    ),
                    "aggregation_end_at": (
                        None if row[8] is None else row[8].isoformat()
                    ),
                    "source_kind": str(row[9]),
                    "vendor_semantic_code": (
                        None if row[10] is None else str(row[10])
                    ),
                    "ontology_version": str(row[11]),
                    "normalizer_version": str(row[12]),
                    "semantic_identity": str(row[13]),
                    "trusted_for_analytics": bool(row[14]),
                    "upcast_status": str(row[15]),
                    "classification_evidence": _json_value(row[16]),
                }
            cursor.execute(
                """
                SELECT observation_id, acquisition_channel
                FROM public.sleep_domain_observation_acquisitions
                WHERE namespace_id = %s AND data_mode = %s
                  AND observation_id = ANY(%s)
                ORDER BY observation_id, acquisition_channel
                """,
                (
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    list(observation_ids),
                ),
            )
            acquisition_channels_by_observation: dict[str, list[str]] = {}
            for observation_id, channel in cursor.fetchall():
                acquisition_channels_by_observation.setdefault(
                    str(observation_id), []
                ).append(str(channel))
            longitudinal_risk_context = self._load_longitudinal_risk_context(
                cursor,
                episode=episode,
            )
            cursor.execute(
                """
                SELECT analysis_revision_id, revision_number
                FROM public.sleep_domain_analysis_revisions
                WHERE namespace_id = %s AND data_mode = %s
                  AND night_episode_revision_id = %s
                ORDER BY revision_number DESC
                LIMIT 1
                """,
                (
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    pinned_revision_id,
                ),
            )
            prior = cursor.fetchone()
            habit_profile = self._load_habit_profile(cursor)
            memory_state = self._load_memory_state(cursor)
        finally:
            cursor.close()

        previous_analysis_id = None if prior is None else str(prior[0])
        next_analysis_number = 1 if prior is None else int(prior[1]) + 1
        facts, observation_hash, adapters, observation_schemas = _build_facts(
            episode=episode,
            revision_id=pinned_revision_id,
            revision_number=revision_number,
            observations=observations,
            quality=quality,
            risk=risk,
            longitudinal_risk_context=longitudinal_risk_context,
            acquisition_channels_by_observation={
                observation_id: tuple(dict.fromkeys(channels))
                for observation_id, channels
                in acquisition_channels_by_observation.items()
            },
            semantics_by_observation=semantics_by_observation,
        )
        policy_versions = {
            str(name): str(value)
            for name, value in (revision_payload.get("policy_versions") or {}).items()
        }
        return LoadedProductAgentSource(
            operation_id=lease.operation_id,
            operation_json=operation_json,
            night_episode_id=night_episode_id,
            night_episode_revision_id=pinned_revision_id,
            night_episode_revision_number=revision_number,
            previous_analysis_revision_id=previous_analysis_id,
            next_analysis_revision_number=next_analysis_number,
            subject_id=subject_id,
            policy_sha256=policy_sha256,
            episode=episode,
            facts=facts,
            observation_set_sha256=observation_hash,
            adapter_versions=adapters,
            observation_schema_versions=observation_schemas,
            policy_versions=policy_versions,
            habit_profile=habit_profile,
            memory_state=memory_state,
        )

    def load_committed_shared_for_narrative(
        self,
        lease: ProductAgentLease,
        *,
        source: LoadedProductAgentSource,
    ) -> PreparedProductAgentArtifact:
        """Read the exact visible v3 attempt selected by narrative work."""

        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                SELECT shared_attempt.attempt_json,
                       shared_attempt.attempt_sha256,
                       analysis.analysis_json,
                       operation.operation_json,
                       operation.policy_sha256
                FROM public.sleep_domain_operations AS operation
                JOIN public.backend_product_attempts AS shared_attempt
                  ON shared_attempt.product_attempt_id =
                       operation.operation_json ->> 'shared_product_attempt_id'
                 AND shared_attempt.namespace_id = operation.namespace_id
                 AND shared_attempt.data_mode = operation.data_mode
                 AND shared_attempt.namespace_generation =
                       operation.namespace_generation
                 AND shared_attempt.subject_id = operation.subject_id
                 AND shared_attempt.night_episode_revision_id =
                       operation.operation_json ->> 'night_episode_revision_id'
                JOIN public.sleep_domain_analysis_revisions AS analysis
                  ON analysis.analysis_revision_id =
                       operation.operation_json ->> 'shared_analysis_revision_id'
                 AND analysis.namespace_id = operation.namespace_id
                 AND analysis.data_mode = operation.data_mode
                 AND analysis.subject_id = operation.subject_id
                 AND analysis.night_episode_revision_id =
                       shared_attempt.night_episode_revision_id
                WHERE operation.operation_id = %s
                  AND operation.namespace_id = %s
                  AND operation.data_mode = %s
                  AND operation.namespace_generation = %s
                  AND operation.subject_id = %s
                  AND operation.run_id IS NOT DISTINCT FROM %s
                  AND operation.arm_id IS NOT DISTINCT FROM %s
                  AND operation.operation_type = 'product.elder_narrative.v1'
                  AND operation.queue_name = 'product_agent'
                  AND operation.status = 'running'
                  AND operation.lease_generation = %s
                  AND operation.fencing_token = %s
                  AND operation.worker_instance = %s
                  AND operation.attempt_count = %s
                  AND operation.lease_expires_at > clock_timestamp()
                  AND shared_attempt.operation_id =
                       operation.operation_json ->> 'shared_operation_id'
                  AND shared_attempt.attempt_state = 'committed'
                  AND shared_attempt.query_visible = TRUE
                  AND shared_attempt.authorization_epoch = %s
                  AND shared_attempt.privacy_epoch = %s
                  AND shared_attempt.retrieval_policy_epoch = %s
                FOR SHARE OF shared_attempt, analysis
                """,
                (
                    *self._lease_scope_params(lease),
                    lease.attempt_sequence,
                    self.scope.authorization_epoch,
                    self.scope.privacy_epoch,
                    self.scope.retrieval_policy_epoch,
                ),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            raise ProductAgentStaleSource(
                "elder narrative committed shared source is unavailable"
            )
        artifact = PreparedProductAgentArtifact.model_validate(
            _json_value(row[0])
        )
        operation_json = _json_value(row[3])
        analysis_json = _json_value(row[2])
        persisted_shared = analysis_json.get("shared_analysis")
        if (
            artifact.schema_version != "product_agent_prepared_attempt.v3"
            or artifact.attempt_sha256 != str(row[1])
            or artifact.operation_id
            != _required_string(operation_json, "shared_operation_id")
            or artifact.product_attempt_id
            != _required_string(operation_json, "shared_product_attempt_id")
            or artifact.analysis.analysis_revision_id
            != _required_string(operation_json, "shared_analysis_revision_id")
            or artifact.night_episode_id != source.night_episode_id
            or artifact.night_episode_revision_id
            != source.night_episode_revision_id
            or artifact.policy_sha256 != str(row[4])
            or artifact.shared_analysis is None
            or artifact.shared_analysis.shared_analysis_sha256
            != _required_string(operation_json, "shared_analysis_sha256")
            or not isinstance(persisted_shared, Mapping)
            or persisted_shared.get("shared_analysis_sha256")
            != artifact.shared_analysis.shared_analysis_sha256
        ):
            raise ProductAgentStaleSource(
                "elder narrative committed shared binding changed"
            )
        projection_value = operation_json.get("elder_projection")
        if not isinstance(projection_value, Mapping):
            raise ProductAgentStaleSource(
                "elder narrative projection source is unavailable"
            )
        elder_projection = RoleProjection.model_validate(projection_value)
        projection_manifest_sha256 = _required_string(
            operation_json,
            "projection_manifest_sha256",
        )
        if (
            artifact.desired_analysis_sha256 is None
            or elder_projection.role is not ReportRole.ELDER
            or elder_projection.source_shared_analysis_sha256
            != artifact.shared_analysis.shared_analysis_sha256
            or elder_projection.projection_sha256
            != _required_string(
                operation_json,
                "elder_projection_content_sha256",
            )
            or role_projection_identity_sha256(
                desired_analysis_sha256=artifact.desired_analysis_sha256,
                role=ReportRole.ELDER,
                projection_sha256=elder_projection.projection_sha256,
                projection_manifest_sha256=projection_manifest_sha256,
            )
            != _required_string(operation_json, "elder_projection_sha256")
        ):
            raise ProductAgentStaleSource(
                "elder narrative projection binding changed"
            )
        return artifact

    def revalidate_provider_source_fence(
        self,
        lease: ProductAgentLease,
        *,
        source: LoadedProductAgentSource,
        operation_type: str,
    ) -> tuple[HabitProfileState, GovernedMemoryState]:
        """Pure short-transaction source check immediately before HTTP."""

        if operation_type not in {
            PRODUCT_AGENT_OPERATION,
            PRODUCT_REPORT_RUN_OPERATION,
            PRODUCT_SHARED_ANALYSIS_OPERATION,
            PRODUCT_ELDER_NARRATIVE_OPERATION,
        }:
            raise ProductAgentInvariantError(
                "provider fence received a non-model Product operation"
            )
        self._lock_and_validate_epochs()
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                SELECT operation.operation_json, operation.policy_sha256,
                       episode.current_revision_id,
                       quality.assessment_id, quality.assessment_json,
                       risk.current_risk_id, risk.risk_json
                FROM public.sleep_domain_operations AS operation
                JOIN public.sleep_domain_night_episodes AS episode
                  ON episode.night_episode_id = CASE
                       WHEN operation.operation_type =
                            'product.elder_narrative.v1'
                       THEN operation.operation_json ->> 'night_episode_id'
                       ELSE operation.target_resource_id
                     END
                 AND episode.namespace_id = operation.namespace_id
                 AND episode.data_mode = operation.data_mode
                 AND episode.namespace_generation = operation.namespace_generation
                 AND episode.subject_id = operation.subject_id
                JOIN public.sleep_domain_current_quality AS quality
                  ON quality.namespace_id = episode.namespace_id
                 AND quality.data_mode = episode.data_mode
                 AND quality.subject_id = episode.subject_id
                 AND quality.night_episode_id = episode.night_episode_id
                JOIN public.sleep_domain_current_risk AS risk
                  ON risk.namespace_id = episode.namespace_id
                 AND risk.data_mode = episode.data_mode
                 AND risk.subject_id = episode.subject_id
                 AND risk.night_episode_id = episode.night_episode_id
                WHERE operation.operation_id = %s
                  AND operation.namespace_id = %s
                  AND operation.data_mode = %s
                  AND operation.namespace_generation = %s
                  AND operation.subject_id = %s
                  AND operation.run_id IS NOT DISTINCT FROM %s
                  AND operation.arm_id IS NOT DISTINCT FROM %s
                  AND operation.lease_generation = %s
                  AND operation.fencing_token = %s
                  AND operation.worker_instance = %s
                  AND operation.operation_type = %s
                  AND operation.queue_name = 'product_agent'
                  AND operation.status = 'running'
                  AND operation.attempt_count = %s
                  AND operation.lease_expires_at > clock_timestamp()
                  AND episode.date_state = 'finalized'
                  AND episode.date_conflict = FALSE
                FOR SHARE OF operation, episode, quality, risk
                """,
                (
                    *self._lease_scope_params(lease),
                    operation_type,
                    lease.attempt_sequence,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise ProductAgentStaleSource(
                    "Product source fence changed before provider send"
                )
            operation_json = _json_value(row[0])
            quality = DeterministicQualityAssessment.model_validate(
                _json_value(row[4])
            )
            risk = CurrentRisk.model_validate(_json_value(row[6]))
            if (
                operation_json != source.operation_json
                or str(row[1]) != source.policy_sha256
                or str(row[2]) != source.night_episode_revision_id
                or str(row[3])
                != _required_string(
                    source.operation_json,
                    "quality_assessment_id",
                )
                or str(row[5])
                != _required_string(source.operation_json, "current_risk_id")
                or quality.model_dump(mode="json")
                != source.facts.deterministic_quality
                or risk.model_dump(mode="json")
                != source.facts.deterministic_risk
            ):
                raise ProductAgentStaleSource(
                    "Product revision/quality/risk fence changed before provider send"
                )
            habit_profile = self._load_habit_profile(cursor)
            memory_state = self._load_memory_state(cursor)
        finally:
            cursor.close()
        return habit_profile, memory_state

    def _load_habit_profile(self, cursor: Any) -> HabitProfileState:
        subject_id = self.scope.subject_id
        if subject_id is None:
            raise ProductAgentInvariantError(
                "Habit Profile load requires an exact subject"
            )
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
            (
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.run_id,
                self.scope.arm_id,
                self.scope.subject_id,
            ),
        )
        rows = cursor.fetchall()
        revisions: list[HabitFact] = []
        for expected_version, row in enumerate(rows, 1):
            if int(row[0]) != expected_version:
                raise ProductAgentInvariantError(
                    "Habit Profile state version is not contiguous"
                )
            fact = HabitFact.model_validate(_json_value(row[1]))
            expected_hash = stable_hash(
                fact.model_dump(mode="json", exclude={"fact_hash"})
            )
            if fact.fact_hash != str(row[2]) or fact.fact_hash != expected_hash:
                raise ProductAgentInvariantError(
                    "Habit Profile revision integrity check failed"
                )
            revisions.append(fact)
        return HabitProfileState(
            subject_id=subject_id,
            version=len(revisions),
            revisions=tuple(revisions),
        )

    def _load_memory_state(self, cursor: Any) -> GovernedMemoryState:
        subject_id = self.scope.subject_id
        if subject_id is None:
            raise ProductAgentInvariantError(
                "Governed Memory load requires an exact subject"
            )
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
            (
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.run_id,
                self.scope.arm_id,
                self.scope.subject_id,
            ),
        )
        rows = cursor.fetchall()
        revisions: list[GovernedMemoryItemV2] = []
        for expected_version, row in enumerate(rows, 1):
            if int(row[0]) != expected_version:
                raise ProductAgentInvariantError(
                    "Governed Memory state version is not contiguous"
                )
            revision = GovernedMemoryItemV2.model_validate(_json_value(row[1]))
            if stable_hash(revision) != str(row[2]):
                raise ProductAgentInvariantError(
                    "Governed Memory revision integrity check failed"
                )
            revisions.append(revision)
        return GovernedMemoryState(
            subject_id=subject_id,
            version=len(revisions),
            revisions=tuple(revisions),
        )

    def _load_longitudinal_risk_context(
        self,
        cursor: Any,
        *,
        episode: NightEpisodeV2,
    ) -> ProductLongitudinalRiskContext | None:
        """Read the recent exact revisions and derive a non-diagnostic watch."""

        if episode.episode_local_date is None:
            return None
        cursor.execute(
            """
            SELECT item.episode_local_date, item.current_revision_id,
                   revision.revision_json
            FROM public.sleep_domain_night_episodes AS item
            JOIN public.sleep_domain_night_episode_revisions AS revision
              ON revision.night_episode_revision_id = item.current_revision_id
             AND revision.night_episode_id = item.night_episode_id
             AND revision.namespace_id = item.namespace_id
             AND revision.data_mode = item.data_mode
             AND revision.namespace_generation = item.namespace_generation
             AND revision.subject_id = item.subject_id
            WHERE item.namespace_id = %s
              AND item.data_mode = %s
              AND item.namespace_generation = %s
              AND COALESCE(item.run_id, '') = COALESCE(%s, '')
              AND COALESCE(item.arm_id, '') = COALESCE(%s, '')
              AND item.subject_id = %s
              AND item.date_state = 'finalized'
              AND item.date_conflict = FALSE
              AND item.episode_local_date <= %s
            ORDER BY item.episode_local_date DESC
            LIMIT 3
            """,
            (
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.run_id,
                self.scope.arm_id,
                self.scope.subject_id,
                episode.episode_local_date,
            ),
        )
        rows = list(reversed(cursor.fetchall()))
        if len(rows) < 3:
            return None
        observation_ids_by_revision: list[tuple[str, ...]] = []
        all_observation_ids: list[str] = []
        for row in rows:
            revision_payload = _json_value(row[2])
            observation_ids = tuple(
                str(value)
                for value in revision_payload.get("observation_ids", ())
            )
            if not observation_ids or len(observation_ids) != len(
                set(observation_ids)
            ):
                raise ProductAgentInvariantError(
                    "longitudinal Product source has no exact observation set"
                )
            observation_ids_by_revision.append(observation_ids)
            all_observation_ids.extend(observation_ids)
        cursor.execute(
            """
            SELECT observation_id, observation_json
            FROM public.sleep_domain_canonical_observations
            WHERE namespace_id = %s AND data_mode = %s
              AND subject_id = %s AND observation_id = ANY(%s)
            """,
            (
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.subject_id,
                all_observation_ids,
            ),
        )
        observations = {
            str(row[0]): SleepObservation.model_validate(_json_value(row[1]))
            for row in cursor.fetchall()
        }
        if set(observations) != set(all_observation_ids):
            raise ProductAgentInvariantError(
                "longitudinal Product source has missing canonical observations"
            )
        summaries: list[ProductNightVitalSummary] = []
        for row, observation_ids in zip(rows, observation_ids_by_revision):
            heart = [
                observation.payload.value
                for observation_id in observation_ids
                if isinstance(
                    (observation := observations[observation_id]).payload,
                    HeartRatePayload,
                )
            ]
            respiratory = [
                observation.payload.value
                for observation_id in observation_ids
                if isinstance(
                    (observation := observations[observation_id]).payload,
                    RespiratoryRatePayload,
                )
            ]
            if not heart or not respiratory:
                return None
            summaries.append(
                ProductNightVitalSummary(
                    local_sleep_date=row[0],
                    night_episode_revision_ref=(
                        f"night_episode_revision:{episode.data_mode.value}:"
                        f"{row[1]}"
                    ),
                    heart_rate_center=sum(heart) / len(heart),
                    respiratory_rate_center=(
                        sum(respiratory) / len(respiratory)
                    ),
                    heart_rate_sample_count=len(heart),
                    respiratory_rate_sample_count=len(respiratory),
                )
            )
        return build_longitudinal_vital_risk_context(tuple(summaries))

    def complete_report_request(
        self,
        lease: ProductAgentLease,
        *,
        source: LoadedProductAgentSource,
        state: Literal["unusable_blocked", "urgent_handled"],
        completed_at: datetime,
    ) -> bool:
        """Close a gated report request without creating Product model work."""

        self._lock_and_validate_epochs()
        cursor = self.connection.cursor()
        try:
            operation_json = self._lock_report_request_fence(cursor, lease)
            if (
                _required_string(operation_json, "night_episode_revision_id")
                != source.night_episode_revision_id
            ):
                raise ProductAgentStaleSource(
                    "report request source changed before deterministic closure"
                )
            (
                current_revision_id,
                current_quality_id,
                current_quality,
                current_risk_id,
                current_risk,
            ) = self._lock_current_report_gate(cursor, source=source)
            gate_matches = (
                current_revision_id == source.night_episode_revision_id
                and current_quality_id
                == _required_string(
                    source.operation_json,
                    "quality_assessment_id",
                )
                and current_risk_id
                == _required_string(source.operation_json, "current_risk_id")
                and current_quality.model_dump(mode="json")
                == source.facts.deterministic_quality
                and current_risk.model_dump(mode="json")
                == source.facts.deterministic_risk
                and (
                    (
                        state == "urgent_handled"
                        and current_risk.health_escalation_allowed
                    )
                    or (
                        state == "unusable_blocked"
                        and not current_risk.health_escalation_allowed
                        and current_quality.data_sufficiency
                        not in {
                            DataSufficiency.SUFFICIENT,
                            DataSufficiency.PARTIAL,
                        }
                    )
                )
            )
            if not gate_matches:
                self._reroute_changed_report_gate(
                    cursor,
                    lease,
                    operation_json=operation_json,
                    current_revision_id=current_revision_id,
                    current_quality_id=current_quality_id,
                    current_risk_id=current_risk_id,
                    rerouted_at=completed_at,
                )
                return False
            terminal = {
                **operation_json,
                "report_result": {
                    "schema_version": "product_report_request_result.v1",
                    "state": state,
                    "gate": (
                        "urgent" if state == "urgent_handled" else "unusable"
                    ),
                },
            }
            self._fail_request_compatibility(
                cursor,
                request_operation_id=lease.operation_id,
                request_json=operation_json,
                error_code=(
                    "product_report_urgent_handled"
                    if state == "urgent_handled"
                    else "product_report_unusable_blocked"
                ),
                failed_at=completed_at,
            )
            cursor.execute(
                """
                UPDATE public.sleep_domain_operations
                SET status = 'succeeded', outcome_class = 'succeeded',
                    operation_json = %s::jsonb, updated_at = %s,
                    cas_version = cas_version + 1,
                    lease_owner = NULL, lease_expires_at = NULL,
                    fencing_token = NULL, worker_instance = NULL,
                    heartbeat_at = NULL
                WHERE operation_id = %s AND namespace_id = %s
                  AND data_mode = %s AND namespace_generation = %s
                  AND subject_id = %s AND status = 'running'
                  AND lease_generation = %s AND fencing_token = %s
                  AND worker_instance = %s
                  AND lease_expires_at > clock_timestamp()
                """,
                (
                    _json(terminal),
                    completed_at,
                    lease.operation_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.subject_id,
                    lease.lease_generation,
                    lease.fencing_token,
                    lease.worker_instance,
                ),
            )
            if cursor.rowcount != 1:
                raise ProductAgentLeaseLost(
                    "report request fence expired before deterministic closure"
                )
        finally:
            cursor.close()
        return True

    def fail_compatibility_bridge(
        self,
        lease: ProductAgentLease,
        *,
        operation_type: str,
        error_code: str,
        outcome_unknown: bool,
        failed_at: datetime,
    ) -> None:
        """Fail automatic legacy wrappers without making them executable."""

        if operation_type not in {
            PRODUCT_REPORT_RUN_OPERATION,
            PRODUCT_SHARED_ANALYSIS_OPERATION,
        }:
            return
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                SELECT operation_json
                FROM public.sleep_domain_operations
                WHERE operation_id = %s AND namespace_id = %s
                  AND data_mode = %s AND namespace_generation = %s
                  AND subject_id = %s
                  AND run_id IS NOT DISTINCT FROM %s
                  AND arm_id IS NOT DISTINCT FROM %s
                  AND lease_generation = %s AND fencing_token = %s
                  AND worker_instance = %s
                  AND operation_type = %s
                  AND queue_name = 'product_agent'
                  AND status = 'running'
                  AND attempt_count = %s
                  AND lease_expires_at > clock_timestamp()
                FOR SHARE
                """,
                (
                    *self._lease_scope_params(lease),
                    operation_type,
                    lease.attempt_sequence,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise ProductAgentLeaseLost(
                    "failed Product work lost its compatibility fence"
                )
            operation_json = _json_value(row[0])
            if operation_type == PRODUCT_REPORT_RUN_OPERATION:
                self._fail_request_compatibility(
                    cursor,
                    request_operation_id=lease.operation_id,
                    request_json=operation_json,
                    error_code=error_code,
                    outcome_unknown=outcome_unknown,
                    failed_at=failed_at,
                )
            else:
                self._fail_compatibilities_for_shared(
                    cursor,
                    shared_operation_id=lease.operation_id,
                    error_code=error_code,
                    outcome_unknown=outcome_unknown,
                    failed_at=failed_at,
                )
        finally:
            cursor.close()

    def _fail_request_compatibility(
        self,
        cursor: Any,
        *,
        request_operation_id: str,
        request_json: Mapping[str, Any],
        error_code: str,
        outcome_unknown: bool = False,
        failed_at: datetime,
    ) -> None:
        compatibility_id = request_json.get(
            "compatibility_product_agent_operation_id"
        )
        if compatibility_id is None:
            return
        if not isinstance(compatibility_id, str) or not compatibility_id:
            raise ProductAgentInvariantError(
                "report request has an invalid compatibility operation"
            )
        expected_revision = _required_string(
            request_json,
            "night_episode_revision_id",
        )
        cursor.execute(
            """
            SELECT status, operation_json
            FROM public.sleep_domain_operations
            WHERE operation_id = %s AND namespace_id = %s
              AND data_mode = %s AND namespace_generation = %s
              AND subject_id = %s
              AND run_id IS NOT DISTINCT FROM %s
              AND arm_id IS NOT DISTINCT FROM %s
              AND operation_type = 'product_agent'
              AND queue_name = 'product_agent_compatibility'
              AND target_resource_key = %s
            FOR UPDATE
            """,
            (
                compatibility_id,
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.subject_id,
                self.scope.run_id,
                self.scope.arm_id,
                expected_revision,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise ProductAgentInvariantError(
                "automatic report lost its compatibility operation"
            )
        status = str(row[0])
        compatibility_json = _json_value(row[1])
        if (
            compatibility_json.get("compatibility_mode")
            != "shared_analysis_bridge.v1"
            or compatibility_json.get("report_request_operation_id")
            != request_operation_id
            or compatibility_json.get("night_episode_revision_id")
            != expected_revision
        ):
            raise ProductAgentInvariantError(
                "automatic report compatibility binding changed"
            )
        if status in {
            "failed",
            "cancelled",
            "succeeded",
            "outcome_unknown",
        }:
            return
        if status not in {"pending", "retry"}:
            raise ProductAgentInvariantError(
                "automatic report compatibility operation became claimable"
            )
        terminal = {
            **compatibility_json,
            "result": {
                "schema_version": "product_agent_compatibility_result.v1",
                "outcome": (
                    "outcome_unknown" if outcome_unknown else "failed"
                ),
                "error_code": error_code,
            },
        }
        cursor.execute(
            """
            UPDATE public.sleep_domain_operations
            SET status = %s, outcome_class = %s,
                error_code = %s, operation_json = %s::jsonb,
                updated_at = %s, cas_version = cas_version + 1
            WHERE operation_id = %s
              AND status IN ('pending', 'retry')
              AND queue_name = 'product_agent_compatibility'
            """,
            (
                "outcome_unknown" if outcome_unknown else "failed",
                (
                    "outcome_unknown"
                    if outcome_unknown
                    else "terminal_failure"
                ),
                error_code,
                _json(terminal),
                failed_at,
                compatibility_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ProductAgentConflict(
                "automatic report compatibility failure CAS lost"
            )

    def _fail_compatibilities_for_shared(
        self,
        cursor: Any,
        *,
        shared_operation_id: str,
        error_code: str,
        outcome_unknown: bool,
        failed_at: datetime,
    ) -> None:
        cursor.execute(
            """
            SELECT operation_id, operation_json
            FROM public.sleep_domain_operations
            WHERE namespace_id = %s AND data_mode = %s
              AND namespace_generation = %s
              AND run_id IS NOT DISTINCT FROM %s
              AND arm_id IS NOT DISTINCT FROM %s
              AND subject_id = %s
              AND operation_type = 'product.report.run.v1'
              AND operation_json #>> '{report_result,shared_operation_id}' = %s
            FOR SHARE
            """,
            (
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.run_id,
                self.scope.arm_id,
                self.scope.subject_id,
                shared_operation_id,
            ),
        )
        requests = tuple(cursor.fetchall())
        for request_id, request_json in requests:
            self._fail_request_compatibility(
                cursor,
                request_operation_id=str(request_id),
                request_json=_json_value(request_json),
                error_code=error_code,
                outcome_unknown=outcome_unknown,
                failed_at=failed_at,
            )

    def _complete_request_compatibility(
        self,
        cursor: Any,
        *,
        request_operation_id: str,
        request_json: Mapping[str, Any],
        product_result: Mapping[str, Any],
        completed_at: datetime,
    ) -> None:
        compatibility_id = request_json.get(
            "compatibility_product_agent_operation_id"
        )
        if compatibility_id is None:
            return
        if not isinstance(compatibility_id, str) or not compatibility_id:
            raise ProductAgentInvariantError(
                "report request has an invalid compatibility operation"
            )
        expected_revision = _required_string(
            request_json,
            "night_episode_revision_id",
        )
        if (
            product_result.get("schema_version") != "product_agent_result.v1"
            or product_result.get("night_episode_revision_id")
            != expected_revision
            or not isinstance(product_result.get("analysis_revision_id"), str)
            or not isinstance(product_result.get("role_view_ids"), list)
            or len(cast(list[Any], product_result["role_view_ids"])) != 3
        ):
            raise ProductAgentInvariantError(
                "shared result cannot satisfy the compatibility contract"
            )
        cursor.execute(
            """
            SELECT status, operation_json
            FROM public.sleep_domain_operations
            WHERE operation_id = %s AND namespace_id = %s
              AND data_mode = %s AND namespace_generation = %s
              AND subject_id = %s
              AND run_id IS NOT DISTINCT FROM %s
              AND arm_id IS NOT DISTINCT FROM %s
              AND operation_type = 'product_agent'
              AND queue_name = 'product_agent_compatibility'
              AND target_resource_key = %s
            FOR UPDATE
            """,
            (
                compatibility_id,
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.subject_id,
                self.scope.run_id,
                self.scope.arm_id,
                expected_revision,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise ProductAgentInvariantError(
                "automatic report lost its compatibility operation"
            )
        status = str(row[0])
        compatibility_json = _json_value(row[1])
        if (
            compatibility_json.get("compatibility_mode")
            != "shared_analysis_bridge.v1"
            or compatibility_json.get("report_request_operation_id")
            != request_operation_id
            or compatibility_json.get("night_episode_revision_id")
            != expected_revision
        ):
            raise ProductAgentInvariantError(
                "automatic report compatibility binding changed"
            )
        terminal = {**compatibility_json, "result": dict(product_result)}
        if status == "succeeded":
            if compatibility_json.get("result") != dict(product_result):
                raise ProductAgentConflict(
                    "automatic report compatibility result changed"
                )
            return
        if status not in {"pending", "retry"}:
            raise ProductAgentConflict(
                "automatic report compatibility cannot become ready"
            )
        cursor.execute(
            """
            UPDATE public.sleep_domain_operations
            SET status = 'succeeded', outcome_class = 'succeeded',
                error_code = NULL, operation_json = %s::jsonb,
                updated_at = %s, cas_version = cas_version + 1
            WHERE operation_id = %s
              AND status IN ('pending', 'retry')
              AND queue_name = 'product_agent_compatibility'
            """,
            (
                _json(terminal),
                completed_at,
                compatibility_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ProductAgentConflict(
                "automatic report compatibility success CAS lost"
            )

    def _complete_compatibilities_for_shared(
        self,
        cursor: Any,
        *,
        shared_operation_id: str,
        product_result: Mapping[str, Any],
        elder_narrative_operation_id: str | None,
        elder_narrative_operation_created: bool,
        completed_at: datetime,
    ) -> None:
        cursor.execute(
            """
            SELECT operation_id, operation_json
            FROM public.sleep_domain_operations
            WHERE namespace_id = %s AND data_mode = %s
              AND namespace_generation = %s
              AND run_id IS NOT DISTINCT FROM %s
              AND arm_id IS NOT DISTINCT FROM %s
              AND subject_id = %s
              AND operation_type = 'product.report.run.v1'
              AND operation_json #>> '{report_result,shared_operation_id}' = %s
            FOR UPDATE
            """,
            (
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.run_id,
                self.scope.arm_id,
                self.scope.subject_id,
                shared_operation_id,
            ),
        )
        requests = tuple(cursor.fetchall())
        for request_id, request_json in requests:
            request_payload = _json_value(request_json)
            report_result = request_payload.get("report_result")
            if not isinstance(report_result, Mapping):
                raise ProductAgentInvariantError(
                    "linked report request has no typed result"
                )
            enriched_report_result = {
                **report_result,
                "state": "ready",
                **(
                    {
                        "elder_narrative_operation_id": (
                            elder_narrative_operation_id
                        ),
                        "elder_narrative_operation_created": bool(
                            elder_narrative_operation_created
                            and report_result.get(
                                "shared_operation_created"
                            )
                            is True
                        ),
                    }
                    if elder_narrative_operation_id is not None
                    else {}
                ),
            }
            enriched_request = {
                **request_payload,
                "report_result": enriched_report_result,
            }
            cursor.execute(
                """
                UPDATE public.sleep_domain_operations
                SET operation_json = %s::jsonb, updated_at = %s,
                    cas_version = cas_version + 1
                WHERE operation_id = %s
                  AND operation_type = 'product.report.run.v1'
                  AND status = 'succeeded'
                """,
                (
                    _json(enriched_request),
                    completed_at,
                    str(request_id),
                ),
            )
            if cursor.rowcount != 1:
                raise ProductAgentConflict(
                    "linked report request enrichment lost its fence"
                )
            self._complete_request_compatibility(
                cursor,
                request_operation_id=str(request_id),
                request_json=enriched_request,
                product_result=product_result,
                completed_at=completed_at,
            )

    def reserve_shared_analysis(
        self,
        lease: ProductAgentLease,
        *,
        source: LoadedProductAgentSource,
        desired_analysis_sha256: str,
        consumed_context_sha256: str,
        runtime_manifest: Mapping[str, Any],
        runtime_manifest_sha256: str,
        projection_manifest: Mapping[str, Any],
        projection_manifest_sha256: str,
        narrative_manifest: Mapping[str, Any],
        narrative_manifest_sha256: str,
        routed_at: datetime,
    ) -> ProductReportRouteResult:
        """Atomically converge a report request on canonical shared work."""

        cursor = self.connection.cursor()
        try:
            request_json = self._lock_report_request_fence(cursor, lease)
            if (
                _required_string(request_json, "night_episode_revision_id")
                != source.night_episode_revision_id
            ):
                raise ProductAgentStaleSource(
                    "report request source changed before shared reservation"
                )
            habit_profile, memory_state = self.revalidate_provider_source_fence(
                lease,
                source=source,
                operation_type=PRODUCT_REPORT_RUN_OPERATION,
            )
            (
                current_context_sha256,
                current_runtime_manifest_sha256,
                current_desired_analysis_sha256,
            ) = _recompute_shared_identity(
                scope=self.scope,
                source=source,
                habit_profile=habit_profile,
                memory_state=memory_state,
                as_of=routed_at,
                runtime_manifest=runtime_manifest,
            )
            if (
                current_context_sha256 != consumed_context_sha256
                or current_runtime_manifest_sha256
                != runtime_manifest_sha256
                or current_desired_analysis_sha256
                != desired_analysis_sha256
            ):
                raise ProductAgentStaleSource(
                    "report source/context/manifest changed before reservation"
                )
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (desired_analysis_sha256,),
            )
            cursor.execute(
                """
                SELECT operation_id, status, operation_json, attempt_count
                FROM public.sleep_domain_operations
                WHERE namespace_id = %s AND data_mode = %s
                  AND namespace_generation = %s
                  AND COALESCE(run_id, '') = COALESCE(%s, '')
                  AND COALESCE(arm_id, '') = COALESCE(%s, '')
                  AND subject_id = %s
                  AND operation_type = 'product.shared_analysis.v1'
                  AND semantic_key = %s AND protocol_version >= 2
                FOR UPDATE
                """,
                (
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.run_id,
                    self.scope.arm_id,
                    self.scope.subject_id,
                    desired_analysis_sha256,
                ),
            )
            existing = cursor.fetchone()
            shared_created = existing is None
            existing_operation_json: dict[str, Any] | None = None
            if existing is None:
                shared_operation_id = self.id_generator(routed_at)
                workload = _workload_snapshot(self.scope, "product_agent")
                shared_json = {
                    "schema_version": "backend_operation.v2",
                    "command_type": PRODUCT_SHARED_ANALYSIS_OPERATION,
                    "target_id": source.night_episode_id,
                    "wake_date": source.facts.local_sleep_date,
                    "night_episode_id": source.night_episode_id,
                    "night_episode_revision_id": (
                        source.night_episode_revision_id
                    ),
                    "quality_assessment_id": _required_string(
                        source.operation_json,
                        "quality_assessment_id",
                    ),
                    "current_risk_id": _required_string(
                        source.operation_json,
                        "current_risk_id",
                    ),
                    "desired_analysis_sha256": desired_analysis_sha256,
                    "consumed_context_sha256": consumed_context_sha256,
                    "runtime_manifest": dict(runtime_manifest),
                    "runtime_manifest_sha256": runtime_manifest_sha256,
                    "invocation_generation": 1,
                    "projection_manifest_sha256": (
                        projection_manifest_sha256
                    ),
                    "source_report_request_id": source.operation_id,
                    "authorization_snapshot": workload,
                }
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
                      available_at, max_attempts,
                      workload_authorization_snapshot_json, policy_sha256
                    ) VALUES (
                      %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s,
                      %s, 'pending', 0, 0, %s::jsonb, %s, %s, 2,
                      %s, %s, %s, 'uuidv7', 'system', %s,
                      'product_agent', 0, %s, 5, %s::jsonb, %s
                    )
                    """,
                    (
                        shared_operation_id,
                        self.scope.namespace_id,
                        self.scope.data_mode,
                        PRODUCT_SHARED_ANALYSIS_OPERATION,
                        self.scope.subject_id,
                        self.scope.service_principal_id,
                        source.night_episode_id,
                        source.facts.local_sleep_date,
                        desired_analysis_sha256,
                        desired_analysis_sha256,
                        _json(shared_json),
                        routed_at,
                        routed_at,
                        self.scope.namespace_generation,
                        self.scope.run_id,
                        self.scope.arm_id,
                        desired_analysis_sha256,
                        routed_at,
                        _json(workload),
                        source.policy_sha256,
                    ),
                )
                shared_status = "pending"
            else:
                shared_operation_id = str(existing[0])
                shared_status = str(existing[1])
                existing_operation_json = _json_value(existing[2])
                existing_attempt_count = int(existing[3])
                if shared_status in {
                    "failed",
                    "cancelled",
                    "dead_letter",
                    "outcome_unknown",
                }:
                    cursor.execute(
                        """
                        SELECT EXISTS (
                          SELECT 1
                          FROM public.backend_product_attempts
                          WHERE operation_id = %s
                            AND namespace_id = %s AND data_mode = %s
                            AND namespace_generation = %s
                            AND run_id IS NOT DISTINCT FROM %s
                            AND arm_id IS NOT DISTINCT FROM %s
                            AND subject_id = %s
                            AND attempt_state = 'committed'
                            AND query_visible = TRUE
                        )
                        """,
                        (
                            shared_operation_id,
                            self.scope.namespace_id,
                            self.scope.data_mode,
                            self.scope.namespace_generation,
                            self.scope.run_id,
                            self.scope.arm_id,
                            self.scope.subject_id,
                        ),
                    )
                    has_visible_artifact = bool(cursor.fetchone()[0])
                    cursor.execute(
                        """
                        SELECT current_state
                        FROM public.backend_invocations
                        WHERE operation_id = %s
                          AND namespace_id = %s AND data_mode = %s
                          AND namespace_generation = %s
                          AND run_id IS NOT DISTINCT FROM %s
                          AND arm_id IS NOT DISTINCT FROM %s
                          AND subject_id = %s
                          AND invocation_kind = 'model'
                        ORDER BY reserved_at, invocation_id
                        """,
                        (
                            shared_operation_id,
                            self.scope.namespace_id,
                            self.scope.data_mode,
                            self.scope.namespace_generation,
                            self.scope.run_id,
                            self.scope.arm_id,
                            self.scope.subject_id,
                        ),
                    )
                    invocation_states = tuple(
                        str(row[0]) for row in cursor.fetchall()
                    )
                    ambiguous_prior_send = any(
                        state
                        in {
                            "send_started",
                            "outcome_possible",
                            "outcome_unknown",
                        }
                        for state in invocation_states
                    )
                    if (
                        not has_visible_artifact
                        and not ambiguous_prior_send
                    ):
                        generation = _optional_positive_int(
                            existing_operation_json,
                            "invocation_generation",
                            default=1,
                        )
                        if invocation_states and invocation_states[-1] == (
                            "known_failed"
                        ):
                            generation += 1
                        existing_operation_json = {
                            **existing_operation_json,
                            "invocation_generation": generation,
                        }
                        cursor.execute(
                            """
                            UPDATE public.sleep_domain_operations
                            SET status = 'pending', outcome_class = NULL,
                                error_code = NULL, available_at = %s,
                                updated_at = %s,
                                cas_version = cas_version + 1,
                                lease_owner = NULL, lease_expires_at = NULL,
                                fencing_token = NULL, worker_instance = NULL,
                                heartbeat_at = NULL, dead_lettered_at = NULL,
                                operation_json = %s::jsonb,
                                max_attempts = GREATEST(
                                  max_attempts,
                                  %s
                                )
                            WHERE operation_id = %s
                              AND status IN (
                                'failed', 'cancelled', 'dead_letter',
                                'outcome_unknown'
                              )
                            """,
                            (
                                routed_at,
                                routed_at,
                                _json(existing_operation_json),
                                existing_attempt_count
                                + PRODUCT_CANONICAL_RETRY_BUDGET,
                                shared_operation_id,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise ProductAgentConflict(
                                "canonical shared retry reset lost its fence"
                            )
                        shared_status = "pending"

            elder_narrative_operation_id: str | None = None
            elder_narrative_operation_created = False
            if shared_status == "succeeded":
                if existing_operation_json is None:
                    raise ProductAgentInvariantError(
                        "succeeded shared operation has no terminal artifact"
                    )
                (
                    elder_narrative_operation_id,
                    elder_narrative_operation_created,
                ) = self._reserve_narrative_for_succeeded_shared(
                    cursor,
                    shared_operation_id=shared_operation_id,
                    shared_operation_json=existing_operation_json,
                    source_operation_json=request_json,
                    wake_date=date.fromisoformat(source.facts.local_sleep_date),
                    reserved_at=routed_at,
                    projection_manifest=projection_manifest,
                    projection_manifest_sha256=(
                        projection_manifest_sha256
                    ),
                    narrative_manifest=narrative_manifest,
                    narrative_manifest_sha256=narrative_manifest_sha256,
                    source=source,
                )
                ready_result = existing_operation_json.get("result")
                if not isinstance(ready_result, Mapping):
                    raise ProductAgentInvariantError(
                        "succeeded shared operation has no compatibility result"
                    )
                self._complete_request_compatibility(
                    cursor,
                    request_operation_id=lease.operation_id,
                    request_json=request_json,
                    product_result=ready_result,
                    completed_at=routed_at,
                )

            report_state: Literal["pending", "ready"] = (
                "ready" if shared_status == "succeeded" else "pending"
            )
            terminal_request = {
                **request_json,
                "report_result": {
                    "schema_version": "product_report_request_result.v1",
                    "state": report_state,
                    "gate": "analyzable",
                    "shared_operation_id": shared_operation_id,
                    "shared_operation_created": shared_created,
                    "desired_analysis_sha256": desired_analysis_sha256,
                    "consumed_context_sha256": consumed_context_sha256,
                    "runtime_manifest_sha256": runtime_manifest_sha256,
                    "elder_narrative_operation_id": (
                        elder_narrative_operation_id
                    ),
                    "elder_narrative_operation_created": (
                        elder_narrative_operation_created
                    ),
                },
            }
            cursor.execute(
                """
                UPDATE public.sleep_domain_operations
                SET status = 'succeeded', outcome_class = 'succeeded',
                    operation_json = %s::jsonb, updated_at = %s,
                    cas_version = cas_version + 1,
                    lease_owner = NULL, lease_expires_at = NULL,
                    fencing_token = NULL, worker_instance = NULL,
                    heartbeat_at = NULL
                WHERE operation_id = %s AND namespace_id = %s
                  AND data_mode = %s AND namespace_generation = %s
                  AND subject_id = %s AND status = 'running'
                  AND lease_generation = %s AND fencing_token = %s
                  AND worker_instance = %s
                  AND lease_expires_at > clock_timestamp()
                """,
                (
                    _json(terminal_request),
                    routed_at,
                    lease.operation_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.subject_id,
                    lease.lease_generation,
                    lease.fencing_token,
                    lease.worker_instance,
                ),
            )
            if cursor.rowcount != 1:
                raise ProductAgentLeaseLost(
                    "report request fence expired before shared reservation"
                )
        finally:
            cursor.close()
        return ProductReportRouteResult(
            operation_id=lease.operation_id,
            state=report_state,
            shared_operation_id=shared_operation_id,
            shared_operation_created=shared_created,
            elder_narrative_operation_id=elder_narrative_operation_id,
            elder_narrative_operation_created=(
                elder_narrative_operation_created
            ),
        )

    def _reserve_narrative_for_succeeded_shared(
        self,
        cursor: Any,
        *,
        shared_operation_id: str,
        shared_operation_json: Mapping[str, Any],
        source_operation_json: Mapping[str, Any],
        wake_date: date,
        reserved_at: datetime,
        projection_manifest: Mapping[str, Any],
        projection_manifest_sha256: str,
        narrative_manifest: Mapping[str, Any],
        narrative_manifest_sha256: str,
        source: LoadedProductAgentSource,
    ) -> tuple[str, bool]:
        committed_shared = self._load_committed_v3_shared_attempt(
            cursor,
            operation_id=shared_operation_id,
            operation_json=shared_operation_json,
        )
        if committed_shared.shared_analysis is None:
            raise ProductAgentInvariantError(
                "succeeded shared operation has no shared analysis"
            )
        elder_message_atoms = build_elder_message_atoms(
            committed_shared.shared_analysis,
            source.facts.elder_presentation_facts(),
        )
        refreshed_projections, projection_identities = (
            self._refresh_role_projections_for_succeeded_shared(
                cursor,
                artifact=committed_shared,
                projection_manifest=projection_manifest,
                projection_manifest_sha256=projection_manifest_sha256,
                refreshed_at=reserved_at,
                elder_message_atoms=elder_message_atoms,
            )
        )
        elder_projection = next(
            projection
            for projection in refreshed_projections
            if projection.role is ReportRole.ELDER
        )
        return self._reserve_elder_narrative(
            cursor,
            artifact=committed_shared,
            source_operation_json=source_operation_json,
            wake_date=wake_date,
            reserved_at=reserved_at,
            elder_projection=elder_projection,
            elder_projection_identity_sha256=(
                projection_identities[ReportRole.ELDER.value]
            ),
            projection_manifest_sha256=projection_manifest_sha256,
            narrative_manifest=narrative_manifest,
            narrative_manifest_sha256=narrative_manifest_sha256,
            elder_message_atoms=elder_message_atoms,
        )

    def _load_committed_v3_shared_attempt(
        self,
        cursor: Any,
        *,
        operation_id: str,
        operation_json: Mapping[str, Any],
    ) -> PreparedProductAgentArtifact:
        result = operation_json.get("result")
        if not isinstance(result, Mapping):
            raise ProductAgentInvariantError(
                "succeeded shared operation has no typed result"
            )
        product_attempt_id = _required_string(result, "product_attempt_id")
        analysis_revision_id = _required_string(
            result,
            "analysis_revision_id",
        )
        cursor.execute(
            """
            SELECT attempt_json, attempt_sha256
            FROM public.backend_product_attempts
            WHERE product_attempt_id = %s AND operation_id = %s
              AND namespace_id = %s AND data_mode = %s
              AND namespace_generation = %s
              AND run_id IS NOT DISTINCT FROM %s
              AND arm_id IS NOT DISTINCT FROM %s
              AND subject_id = %s
              AND attempt_state = 'committed' AND query_visible = TRUE
              AND authorization_epoch = %s AND privacy_epoch = %s
              AND retrieval_policy_epoch = %s
            FOR SHARE
            """,
            (
                product_attempt_id,
                operation_id,
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.run_id,
                self.scope.arm_id,
                self.scope.subject_id,
                self.scope.authorization_epoch,
                self.scope.privacy_epoch,
                self.scope.retrieval_policy_epoch,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise ProductAgentStaleSource(
                "succeeded shared operation has no visible committed attempt"
            )
        artifact = PreparedProductAgentArtifact.model_validate(
            _json_value(row[0])
        )
        if (
            artifact.schema_version != "product_agent_prepared_attempt.v3"
            or artifact.attempt_sha256 != str(row[1])
            or artifact.operation_id != operation_id
            or artifact.product_attempt_id != product_attempt_id
            or artifact.analysis.analysis_revision_id != analysis_revision_id
            or artifact.shared_analysis is None
        ):
            raise ProductAgentInvariantError(
                "succeeded shared operation is not bound to a v3 artifact"
            )
        return artifact

    def _refresh_role_projections_for_succeeded_shared(
        self,
        cursor: Any,
        *,
        artifact: PreparedProductAgentArtifact,
        projection_manifest: Mapping[str, Any],
        projection_manifest_sha256: str,
        refreshed_at: datetime,
        elder_message_atoms: tuple[ElderMessageAtom, ...],
    ) -> tuple[
        tuple[RoleProjection, RoleProjection, RoleProjection],
        dict[str, str],
    ]:
        """Refresh the three deterministic role rows without model work."""

        if (
            artifact.schema_version != "product_agent_prepared_attempt.v3"
            or artifact.shared_analysis is None
            or artifact.desired_analysis_sha256 is None
            or stable_hash(projection_manifest)
            != projection_manifest_sha256
        ):
            raise ProductAgentInvariantError(
                "projection refresh has no valid committed shared source"
            )
        projections = build_shared_role_projections(
            artifact.shared_analysis,
            elder_message_atoms=elder_message_atoms,
        )
        projection_identities = {
            projection.role.value: role_projection_identity_sha256(
                desired_analysis_sha256=artifact.desired_analysis_sha256,
                role=projection.role,
                projection_sha256=projection.projection_sha256,
                projection_manifest_sha256=projection_manifest_sha256,
            )
            for projection in projections
        }
        cursor.execute(
            """
            SELECT episode_local_date, assignment_basis, current_revision_id
            FROM public.sleep_domain_night_episodes
            WHERE night_episode_id = %s AND namespace_id = %s
              AND data_mode = %s AND namespace_generation = %s
              AND subject_id = %s
              AND run_id IS NOT DISTINCT FROM %s
              AND arm_id IS NOT DISTINCT FROM %s
              AND date_state = 'finalized' AND date_conflict = FALSE
            FOR SHARE
            """,
            (
                artifact.night_episode_id,
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.subject_id,
                self.scope.run_id,
                self.scope.arm_id,
            ),
        )
        episode_row = cursor.fetchone()
        if (
            episode_row is None
            or str(episode_row[2]) != artifact.night_episode_revision_id
        ):
            raise ProductAgentStaleSource(
                "projection refresh source revision is no longer current"
            )
        for projection in projections:
            old_run = next(
                item
                for item in artifact.role_runs
                if item.role.value == projection.role.value
            )
            view = _role_view_from_projection(
                analysis=artifact.analysis,
                projection=projection,
                product_episode_id=old_run.product_episode_id,
                role_view_id=old_run.role_view.role_view_id,
                generated_at=refreshed_at,
            )
            public_today = _public_today_projection(
                analysis=artifact.analysis,
                view=view,
                projection_version=artifact.source_state_version,
                episode_local_date=episode_row[0],
                assignment_basis=str(episode_row[1]),
                committed_at=refreshed_at,
            )
            cursor.execute(
                """
                UPDATE public.sleep_domain_analysis_role_views
                SET status = %s, product_agent_episode_id = %s,
                    view_json = %s::jsonb, generated_at = %s,
                    source_fact_snapshot_sha256 = %s,
                    source_state_version = %s,
                    authorization_epoch = %s, privacy_epoch = %s,
                    retrieval_policy_epoch = %s, policy_sha256 = %s,
                    projection_sha256 = %s,
                    public_schema_version = 'product_sleep_today.v1',
                    public_today_json = %s::jsonb,
                    public_projection_sha256 = %s,
                    public_committed_at = %s
                WHERE role_view_id = %s AND namespace_id = %s
                  AND data_mode = %s AND namespace_generation = %s
                  AND run_id IS NOT DISTINCT FROM %s
                  AND arm_id IS NOT DISTINCT FROM %s
                  AND subject_id = %s AND analysis_revision_id = %s
                  AND night_episode_id = %s
                  AND night_episode_revision_id = %s AND role = %s
                """,
                (
                    view.status.value,
                    view.product_agent_episode_id,
                    projection.model_dump_json(),
                    refreshed_at,
                    artifact.shared_analysis.fact_snapshot_hash,
                    artifact.source_state_version,
                    self.scope.authorization_epoch,
                    self.scope.privacy_epoch,
                    self.scope.retrieval_policy_epoch,
                    artifact.policy_sha256,
                    projection_identities[projection.role.value],
                    _json(public_today),
                    stable_hash(public_today),
                    refreshed_at,
                    view.role_view_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.run_id,
                    self.scope.arm_id,
                    self.scope.subject_id,
                    artifact.analysis.analysis_revision_id,
                    artifact.night_episode_id,
                    artifact.night_episode_revision_id,
                    projection.role.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ProductAgentConflict(
                    "deterministic role projection refresh lost its row fence"
                )
        return projections, projection_identities

    def _reserve_elder_narrative(
        self,
        cursor: Any,
        *,
        artifact: PreparedProductAgentArtifact,
        source_operation_json: Mapping[str, Any],
        wake_date: date,
        reserved_at: datetime,
        elder_projection: RoleProjection | None = None,
        elder_projection_identity_sha256: str | None = None,
        projection_manifest_sha256: str | None = None,
        narrative_manifest: Mapping[str, Any] | None = None,
        narrative_manifest_sha256: str | None = None,
        elder_message_atoms: tuple[ElderMessageAtom, ...] = (),
    ) -> tuple[str, bool]:
        """Reserve/reuse narrative work inside the shared commit transaction."""

        shared = artifact.shared_analysis
        resolved_manifest = (
            artifact.elder_narrative_manifest
            if narrative_manifest is None
            else dict(narrative_manifest)
        )
        resolved_manifest_sha256 = (
            artifact.elder_narrative_manifest_sha256
            if narrative_manifest_sha256 is None
            else narrative_manifest_sha256
        )
        if (
            shared is None
            or resolved_manifest is None
            or resolved_manifest_sha256 is None
        ):
            raise ProductAgentInvariantError(
                "v3 shared commit has no elder narrative identity"
            )
        elder = next(
            item for item in artifact.role_runs if item.role is AnalysisRole.ELDER
        )
        resolved_elder_projection = (
            elder.role_projection
            if elder_projection is None
            else elder_projection
        )
        resolved_projection_identity = (
            None
            if artifact.projection_identities is None
            else artifact.projection_identities.get(AnalysisRole.ELDER.value)
        )
        if elder_projection_identity_sha256 is not None:
            resolved_projection_identity = elder_projection_identity_sha256
        resolved_projection_manifest_sha256 = (
            artifact.projection_manifest_sha256
            if projection_manifest_sha256 is None
            else projection_manifest_sha256
        )
        if (
            resolved_elder_projection is None
            or resolved_projection_identity is None
            or resolved_projection_manifest_sha256 is None
            or resolved_elder_projection.role is not ReportRole.ELDER
            or resolved_elder_projection.source_shared_analysis_sha256
            != shared.shared_analysis_sha256
        ):
            raise ProductAgentInvariantError(
                "v3 shared commit has no elder projection"
            )
        atom_authority_sha256 = stable_hash(
            [item.model_dump(mode="json") for item in elder_message_atoms]
        )
        if (
            not elder_message_atoms
            or resolved_elder_projection.presentation_authority_sha256
            != atom_authority_sha256
        ):
            raise ProductAgentInvariantError(
                "elder message atoms differ from projection authority"
            )
        if stable_hash(resolved_manifest) != resolved_manifest_sha256:
            raise ProductAgentInvariantError(
                "elder narrative manifest hash is inconsistent"
            )
        render_identity_sha256 = _elder_narrative_identity_sha256(
            shared_analysis_sha256=shared.shared_analysis_sha256,
            elder_projection_sha256=resolved_projection_identity,
            narrative_manifest_sha256=resolved_manifest_sha256,
        )
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (render_identity_sha256,),
        )
        cursor.execute(
            """
            SELECT operation_id, status, operation_json, attempt_count
            FROM public.sleep_domain_operations
            WHERE namespace_id = %s AND data_mode = %s
              AND namespace_generation = %s
              AND run_id IS NOT DISTINCT FROM %s
              AND arm_id IS NOT DISTINCT FROM %s
              AND subject_id = %s
              AND operation_type = 'product.elder_narrative.v1'
              AND semantic_key = %s AND protocol_version >= 2
            FOR UPDATE
            """,
            (
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.run_id,
                self.scope.arm_id,
                self.scope.subject_id,
                render_identity_sha256,
            ),
        )
        existing = cursor.fetchone()
        created = existing is None
        if existing is not None:
            operation_id = str(existing[0])
            existing_status = str(existing[1])
            existing_json = _json_value(existing[2])
            attempt_count = int(existing[3])
            if existing_status in {
                "failed",
                "cancelled",
                "dead_letter",
                "outcome_unknown",
            }:
                cursor.execute(
                    """
                    SELECT EXISTS (
                      SELECT 1
                      FROM public.backend_product_attempts
                      WHERE operation_id = %s
                        AND attempt_state = 'committed'
                        AND query_visible = TRUE
                    )
                    """,
                    (operation_id,),
                )
                has_visible_artifact = bool(cursor.fetchone()[0])
                cursor.execute(
                    """
                    SELECT current_state
                    FROM public.backend_invocations
                    WHERE operation_id = %s
                      AND namespace_id = %s AND data_mode = %s
                      AND namespace_generation = %s
                      AND run_id IS NOT DISTINCT FROM %s
                      AND arm_id IS NOT DISTINCT FROM %s
                      AND subject_id = %s
                      AND invocation_kind = 'model'
                    ORDER BY reserved_at, invocation_id
                    """,
                    (
                        operation_id,
                        self.scope.namespace_id,
                        self.scope.data_mode,
                        self.scope.namespace_generation,
                        self.scope.run_id,
                        self.scope.arm_id,
                        self.scope.subject_id,
                    ),
                )
                invocation_states = tuple(
                    str(row[0]) for row in cursor.fetchall()
                )
                ambiguous_prior_send = any(
                    state
                    in {
                        "send_started",
                        "outcome_possible",
                        "outcome_unknown",
                    }
                    for state in invocation_states
                )
                if not has_visible_artifact and not ambiguous_prior_send:
                    generation = _optional_positive_int(
                        existing_json,
                        "invocation_generation",
                        default=1,
                    )
                    if invocation_states and invocation_states[-1] == (
                        "known_failed"
                    ):
                        generation += 1
                    existing_json = {
                        **existing_json,
                        "invocation_generation": generation,
                    }
                    cursor.execute(
                        """
                        UPDATE public.sleep_domain_operations
                        SET status = 'pending', outcome_class = NULL,
                            error_code = NULL, available_at = %s,
                            updated_at = %s, cas_version = cas_version + 1,
                            lease_owner = NULL, lease_expires_at = NULL,
                            fencing_token = NULL, worker_instance = NULL,
                            heartbeat_at = NULL, dead_lettered_at = NULL,
                            operation_json = %s::jsonb,
                            max_attempts = GREATEST(max_attempts, %s)
                        WHERE operation_id = %s
                          AND status IN (
                            'failed', 'cancelled', 'dead_letter',
                            'outcome_unknown'
                          )
                        """,
                        (
                            reserved_at,
                            reserved_at,
                            _json(existing_json),
                            attempt_count
                            + PRODUCT_CANONICAL_RETRY_BUDGET,
                            operation_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ProductAgentConflict(
                            "canonical narrative retry reset lost its fence"
                        )
            return operation_id, False

        operation_id = self.id_generator(reserved_at)
        workload = _workload_snapshot(self.scope, "product_agent")
        operation_json = {
            "schema_version": "backend_operation.v2",
            "command_type": PRODUCT_ELDER_NARRATIVE_OPERATION,
            "target_id": artifact.analysis.analysis_revision_id,
            "wake_date": wake_date.isoformat(),
            "night_episode_id": artifact.night_episode_id,
            "night_episode_revision_id": artifact.night_episode_revision_id,
            "quality_assessment_id": _required_string(
                source_operation_json,
                "quality_assessment_id",
            ),
            "current_risk_id": _required_string(
                source_operation_json,
                "current_risk_id",
            ),
            "shared_operation_id": artifact.operation_id,
            "shared_product_attempt_id": artifact.product_attempt_id,
            "shared_analysis_revision_id": (
                artifact.analysis.analysis_revision_id
            ),
            "shared_analysis_sha256": shared.shared_analysis_sha256,
            "elder_projection_sha256": resolved_projection_identity,
            "elder_projection_content_sha256": (
                resolved_elder_projection.projection_sha256
            ),
            "elder_projection": resolved_elder_projection.model_dump(
                mode="json"
            ),
            "elder_message_atoms": [
                item.model_dump(mode="json")
                for item in elder_message_atoms
            ],
            "projection_manifest_sha256": (
                resolved_projection_manifest_sha256
            ),
            "narrative_manifest": resolved_manifest,
            "narrative_manifest_sha256": resolved_manifest_sha256,
            "render_identity_sha256": render_identity_sha256,
            "invocation_generation": 1,
            "authorization_snapshot": workload,
        }
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
              available_at, max_attempts,
              workload_authorization_snapshot_json, policy_sha256
            ) VALUES (
              %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s,
              %s, 'pending', 0, 0, %s::jsonb, %s, %s, 2,
              %s, %s, %s, 'uuidv7', 'system', %s,
              'product_agent', 0, %s, 5, %s::jsonb, %s
            )
            """,
            (
                operation_id,
                self.scope.namespace_id,
                self.scope.data_mode,
                PRODUCT_ELDER_NARRATIVE_OPERATION,
                self.scope.subject_id,
                self.scope.service_principal_id,
                artifact.analysis.analysis_revision_id,
                wake_date.isoformat(),
                render_identity_sha256,
                render_identity_sha256,
                _json(operation_json),
                reserved_at,
                reserved_at,
                self.scope.namespace_generation,
                self.scope.run_id,
                self.scope.arm_id,
                render_identity_sha256,
                reserved_at,
                _json(workload),
                artifact.policy_sha256,
            ),
        )
        if cursor.rowcount != 1:
            raise ProductAgentConflict(
                "elder narrative operation reservation failed"
            )
        return operation_id, created

    def persist_prepared(
        self,
        lease: ProductAgentLease,
        artifact: PreparedProductAgentArtifact,
    ) -> None:
        self._validate_artifact_identity(lease, artifact)
        self._lock_and_validate_epochs()
        cursor = self.connection.cursor()
        try:
            self._lock_operation_fence(cursor, lease, artifact)
            cursor.execute(
                """
                SELECT product_attempt_id
                FROM public.backend_product_attempts
                WHERE operation_id = %s AND attempt_sequence = %s
                FOR UPDATE
                """,
                (lease.operation_id, lease.attempt_sequence),
            )
            slot = cursor.fetchone()
            if slot is not None and str(slot[0]) != artifact.product_attempt_id:
                raise ProductAgentConflict(
                    "Product attempt sequence belongs to another artifact"
                )

            existing = self._lock_prepared_artifact(cursor, artifact)
            if existing is not None:
                self._persist_existing_artifact(cursor, lease, artifact, existing)
                return
            if slot is not None:
                raise ProductAgentConflict(
                    "Product attempt slot has no matching prepared artifact"
                )

            cursor.execute(
                """
                INSERT INTO public.backend_product_attempts (
                  product_attempt_id, namespace_id, data_mode,
                  namespace_generation, run_id, arm_id, subject_id,
                  operation_id, night_episode_revision_id, attempt_sequence,
                  attempt_state, query_visible, fact_snapshot_sha256,
                  state_version, authorization_epoch, privacy_epoch,
                  retrieval_policy_epoch, policy_sha256, lease_generation,
                  fencing_token, attempt_sha256, attempt_json, prepared_at
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  'prepared', FALSE, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s::jsonb, %s
                )
                """,
                (
                    artifact.product_attempt_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.run_id,
                    self.scope.arm_id,
                    self.scope.subject_id,
                    lease.operation_id,
                    artifact.night_episode_revision_id,
                    lease.attempt_sequence,
                    artifact.source_fact_snapshot_sha256,
                    artifact.source_state_version,
                    self.scope.authorization_epoch,
                    self.scope.privacy_epoch,
                    self.scope.retrieval_policy_epoch,
                    artifact.policy_sha256,
                    lease.lease_generation,
                    lease.fencing_token,
                    artifact.attempt_sha256,
                    artifact.model_dump_json(),
                    artifact.prepared_at,
                ),
            )
            if cursor.rowcount != 1:
                raise ProductAgentConflict("Product prepared artifact insert failed")
        finally:
            cursor.close()

    def persist_failed_provider_attempt(
        self,
        lease: ProductAgentLease,
        attempt: FailedProductProviderAttempt,
    ) -> None:
        """Persist only safe aggregates; never expose a failed artifact."""

        if (
            attempt.operation_id != lease.operation_id
            or attempt.attempt_sequence != lease.attempt_sequence
        ):
            raise ProductAgentInvariantError(
                "failed provider attempt has another work identity"
            )
        self._lock_and_validate_epochs()
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                SELECT operation_json, policy_sha256
                FROM public.sleep_domain_operations
                WHERE operation_id = %s AND namespace_id = %s
                  AND data_mode = %s AND namespace_generation = %s
                  AND subject_id = %s
                  AND run_id IS NOT DISTINCT FROM %s
                  AND arm_id IS NOT DISTINCT FROM %s
                  AND operation_type = %s
                  AND queue_name = 'product_agent' AND status = 'running'
                  AND lease_generation = %s AND fencing_token = %s
                  AND worker_instance = %s AND attempt_count = %s
                  AND lease_expires_at > clock_timestamp()
                FOR UPDATE
                """,
                (
                    lease.operation_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.subject_id,
                    self.scope.run_id,
                    self.scope.arm_id,
                    attempt.operation_type,
                    lease.lease_generation,
                    lease.fencing_token,
                    lease.worker_instance,
                    lease.attempt_sequence,
                ),
            )
            operation_row = cursor.fetchone()
            if operation_row is None:
                raise ProductAgentLeaseLost(
                    "failed provider attempt lost its operation fence"
                )
            operation_json = _json_value(operation_row[0])
            if (
                str(operation_row[1]) != attempt.policy_sha256
                or _required_string(
                    operation_json,
                    "night_episode_revision_id",
                )
                != attempt.night_episode_revision_id
            ):
                raise ProductAgentConflict(
                    "failed provider attempt source identity changed"
                )
            cursor.execute(
                """
                SELECT product_attempt_id, attempt_state, query_visible,
                       attempt_sha256, attempt_json
                FROM public.backend_product_attempts
                WHERE operation_id = %s AND attempt_sequence = %s
                FOR UPDATE
                """,
                (lease.operation_id, lease.attempt_sequence),
            )
            existing = cursor.fetchone()
            attempt_state = (
                "outcome_unknown" if attempt.outcome_unknown else "abandoned"
            )
            if existing is not None:
                if (
                    str(existing[0]) == attempt.product_attempt_id
                    and str(existing[1]) == attempt_state
                    and not bool(existing[2])
                    and str(existing[3]) == attempt.attempt_sha256
                    and _json_value(existing[4])
                    == attempt.model_dump(mode="json")
                ):
                    return
                raise ProductAgentConflict(
                    "failed provider attempt slot already has another artifact"
                )
            cursor.execute(
                """
                INSERT INTO public.backend_product_attempts (
                  product_attempt_id, namespace_id, data_mode,
                  namespace_generation, run_id, arm_id, subject_id,
                  operation_id, night_episode_revision_id, attempt_sequence,
                  attempt_state, query_visible, fact_snapshot_sha256,
                  state_version, authorization_epoch, privacy_epoch,
                  retrieval_policy_epoch, policy_sha256, lease_generation,
                  fencing_token, attempt_sha256, attempt_json, prepared_at
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, FALSE, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s::jsonb, %s
                )
                """,
                (
                    attempt.product_attempt_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.run_id,
                    self.scope.arm_id,
                    self.scope.subject_id,
                    lease.operation_id,
                    attempt.night_episode_revision_id,
                    lease.attempt_sequence,
                    attempt_state,
                    attempt.source_fact_snapshot_sha256,
                    attempt.source_state_version,
                    self.scope.authorization_epoch,
                    self.scope.privacy_epoch,
                    self.scope.retrieval_policy_epoch,
                    attempt.policy_sha256,
                    lease.lease_generation,
                    lease.fencing_token,
                    attempt.attempt_sha256,
                    attempt.model_dump_json(),
                    attempt.failed_at,
                ),
            )
            if cursor.rowcount != 1:
                raise ProductAgentConflict(
                    "failed provider attempt insert failed"
                )
        finally:
            cursor.close()

    def persist_elder_narrative_prepared(
        self,
        lease: ProductAgentLease,
        artifact: PreparedElderNarrativeArtifact,
    ) -> None:
        """Persist a narrative attempt without making it query-visible."""

        self._validate_narrative_artifact_identity(lease, artifact)
        self._lock_and_validate_epochs()
        cursor = self.connection.cursor()
        try:
            self._lock_narrative_operation_fence(cursor, lease, artifact)
            cursor.execute(
                """
                SELECT product_attempt_id
                FROM public.backend_product_attempts
                WHERE operation_id = %s AND attempt_sequence = %s
                FOR UPDATE
                """,
                (lease.operation_id, lease.attempt_sequence),
            )
            slot = cursor.fetchone()
            if slot is not None and str(slot[0]) != artifact.product_attempt_id:
                raise ProductAgentConflict(
                    "elder narrative attempt sequence belongs to another artifact"
                )
            existing = self._lock_prepared_artifact(cursor, artifact)
            if existing is not None:
                self._persist_existing_artifact(cursor, lease, artifact, existing)
                return
            if slot is not None:
                raise ProductAgentConflict(
                    "elder narrative attempt slot has no matching artifact"
                )
            cursor.execute(
                """
                INSERT INTO public.backend_product_attempts (
                  product_attempt_id, namespace_id, data_mode,
                  namespace_generation, run_id, arm_id, subject_id,
                  operation_id, night_episode_revision_id, attempt_sequence,
                  attempt_state, query_visible, fact_snapshot_sha256,
                  state_version, authorization_epoch, privacy_epoch,
                  retrieval_policy_epoch, policy_sha256, lease_generation,
                  fencing_token, attempt_sha256, attempt_json, prepared_at
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  'prepared', FALSE, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s::jsonb, %s
                )
                """,
                (
                    artifact.product_attempt_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.run_id,
                    self.scope.arm_id,
                    self.scope.subject_id,
                    lease.operation_id,
                    artifact.night_episode_revision_id,
                    lease.attempt_sequence,
                    artifact.source_fact_snapshot_sha256,
                    artifact.source_state_version,
                    self.scope.authorization_epoch,
                    self.scope.privacy_epoch,
                    self.scope.retrieval_policy_epoch,
                    artifact.policy_sha256,
                    lease.lease_generation,
                    lease.fencing_token,
                    artifact.attempt_sha256,
                    artifact.model_dump_json(),
                    artifact.prepared_at,
                ),
            )
            if cursor.rowcount != 1:
                raise ProductAgentConflict(
                    "elder narrative prepared artifact insert failed"
                )
        finally:
            cursor.close()

    def commit_elder_narrative_prepared(
        self,
        lease: ProductAgentLease,
        artifact: PreparedElderNarrativeArtifact,
        *,
        source: LoadedProductAgentSource,
        committed_shared: PreparedProductAgentArtifact,
        shared_runtime_manifest: Mapping[str, Any],
        projection_manifest: Mapping[str, Any],
        narrative_manifest: Mapping[str, Any],
        committed_at: datetime,
    ) -> ProductElderNarrativeCommitResult:
        """Fence-commit only the narrative attempt and its terminal operation."""

        self._validate_narrative_artifact_identity(lease, artifact)
        self._lock_and_validate_epochs()
        cursor = self.connection.cursor()
        try:
            operation_json = self._lock_narrative_operation_fence(
                cursor,
                lease,
                artifact,
            )
            self._lock_final_context_writers(cursor)
            habit_profile, memory_state = self.revalidate_provider_source_fence(
                lease,
                source=source,
                operation_type=PRODUCT_ELDER_NARRATIVE_OPERATION,
            )
            (
                current_context_sha256,
                current_runtime_sha256,
                current_desired_sha256,
            ) = _recompute_shared_identity(
                scope=self.scope,
                source=source,
                habit_profile=habit_profile,
                memory_state=memory_state,
                as_of=committed_at,
                runtime_manifest=shared_runtime_manifest,
            )
            if (
                committed_shared.schema_version
                != "product_agent_prepared_attempt.v3"
                or committed_shared.product_attempt_id
                != artifact.shared_product_attempt_id
                or committed_shared.analysis.analysis_revision_id
                != artifact.shared_analysis_revision_id
                or current_context_sha256
                != committed_shared.consumed_context_sha256
                or current_runtime_sha256
                != committed_shared.runtime_manifest_sha256
                or current_desired_sha256
                != committed_shared.desired_analysis_sha256
                or stable_hash(narrative_manifest)
                != artifact.narrative_manifest_sha256
                or dict(narrative_manifest) != artifact.narrative_manifest
                or artifact.narrative_manifest_sha256
                != _required_string(
                    operation_json,
                    "narrative_manifest_sha256",
                )
                or stable_hash(projection_manifest)
                != _required_string(
                    operation_json,
                    "projection_manifest_sha256",
                )
            ):
                raise ProductAgentStaleSource(
                    "elder narrative source/context/manifest changed before commit"
                )
            cursor.execute(
                """
                SELECT current_revision_id
                FROM public.sleep_domain_night_episodes
                WHERE night_episode_id = %s AND namespace_id = %s
                  AND data_mode = %s AND namespace_generation = %s
                  AND subject_id = %s
                  AND run_id IS NOT DISTINCT FROM %s
                  AND arm_id IS NOT DISTINCT FROM %s
                  AND date_state = 'finalized' AND date_conflict = FALSE
                FOR SHARE
                """,
                (
                    artifact.night_episode_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.subject_id,
                    self.scope.run_id,
                    self.scope.arm_id,
                ),
            )
            episode_row = cursor.fetchone()
            if episode_row is None:
                raise ProductAgentInvariantError(
                    "elder narrative source Episode is unavailable"
                )
            if str(episode_row[0]) != artifact.night_episode_revision_id:
                raise ProductAgentStaleSource(
                    "elder narrative source revision changed before commit"
                )
            cursor.execute(
                """
                SELECT attempt_sha256, attempt_json
                FROM public.backend_product_attempts
                WHERE product_attempt_id = %s
                  AND operation_id = %s
                  AND namespace_id = %s AND data_mode = %s
                  AND namespace_generation = %s
                  AND run_id IS NOT DISTINCT FROM %s
                  AND arm_id IS NOT DISTINCT FROM %s
                  AND subject_id = %s
                  AND night_episode_revision_id = %s
                  AND attempt_state = 'committed' AND query_visible = TRUE
                  AND authorization_epoch = %s AND privacy_epoch = %s
                  AND retrieval_policy_epoch = %s
                FOR SHARE
                """,
                (
                    artifact.shared_product_attempt_id,
                    _required_string(operation_json, "shared_operation_id"),
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.run_id,
                    self.scope.arm_id,
                    self.scope.subject_id,
                    artifact.night_episode_revision_id,
                    self.scope.authorization_epoch,
                    self.scope.privacy_epoch,
                    self.scope.retrieval_policy_epoch,
                ),
            )
            shared_row = cursor.fetchone()
            if shared_row is None:
                raise ProductAgentStaleSource(
                    "elder narrative shared attempt is no longer visible"
                )
            shared_artifact = PreparedProductAgentArtifact.model_validate(
                _json_value(shared_row[1])
            )
            operation_projection_value = operation_json.get(
                "elder_projection"
            )
            if not isinstance(operation_projection_value, Mapping):
                raise ProductAgentInvariantError(
                    "elder narrative operation has no deterministic projection"
                )
            operation_projection = RoleProjection.model_validate(
                operation_projection_value
            )
            current_projection_manifest_sha256 = stable_hash(
                projection_manifest
            )
            current_projection_identity_sha256 = (
                role_projection_identity_sha256(
                    desired_analysis_sha256=_required_optional_string(
                        shared_artifact.desired_analysis_sha256,
                        "committed shared desired analysis",
                    ),
                    role=ReportRole.ELDER,
                    projection_sha256=(
                        operation_projection.projection_sha256
                    ),
                    projection_manifest_sha256=(
                        current_projection_manifest_sha256
                    ),
                )
            )
            if (
                shared_artifact.schema_version
                != "product_agent_prepared_attempt.v3"
                or shared_artifact.attempt_sha256 != str(shared_row[0])
                or shared_artifact.analysis.analysis_revision_id
                != artifact.shared_analysis_revision_id
                or shared_artifact.shared_analysis is None
                or shared_artifact.shared_analysis.shared_analysis_sha256
                != artifact.shared_analysis_sha256
                or operation_projection.role is not ReportRole.ELDER
                or operation_projection.source_shared_analysis_sha256
                != artifact.shared_analysis_sha256
                or operation_projection.projection_sha256
                != _required_string(
                    operation_json,
                    "elder_projection_content_sha256",
                )
                or current_projection_manifest_sha256
                != _required_string(
                    operation_json,
                    "projection_manifest_sha256",
                )
                or current_projection_identity_sha256
                != artifact.source_projection_sha256
                or current_projection_identity_sha256
                != _required_string(
                    operation_json,
                    "elder_projection_sha256",
                )
                or artifact.shared_analysis_sha256
                != _required_string(
                    operation_json,
                    "shared_analysis_sha256",
                )
            ):
                raise ProductAgentStaleSource(
                    "elder narrative shared attempt binding changed"
                )
            cursor.execute(
                """
                SELECT attempt_sha256, attempt_state, query_visible,
                       lease_generation, fencing_token
                FROM public.backend_product_attempts
                WHERE product_attempt_id = %s AND operation_id = %s
                  AND attempt_sequence = %s
                FOR UPDATE
                """,
                (
                    artifact.product_attempt_id,
                    lease.operation_id,
                    lease.attempt_sequence,
                ),
            )
            attempt_row = cursor.fetchone()
            if attempt_row is None or (
                str(attempt_row[0]) != artifact.attempt_sha256
                or str(attempt_row[1]) != "prepared"
                or bool(attempt_row[2])
                or int(attempt_row[3]) != lease.lease_generation
                or str(attempt_row[4]) != lease.fencing_token
            ):
                raise ProductAgentConflict(
                    "prepared elder narrative attempt changed before commit"
                )
            cursor.execute(
                """
                UPDATE public.backend_product_attempts
                SET attempt_state = 'committed', query_visible = TRUE,
                    committed_at = %s
                WHERE product_attempt_id = %s AND operation_id = %s
                  AND attempt_sequence = %s AND attempt_state = 'prepared'
                  AND query_visible = FALSE AND attempt_sha256 = %s
                  AND lease_generation = %s AND fencing_token = %s
                """,
                (
                    committed_at,
                    artifact.product_attempt_id,
                    lease.operation_id,
                    lease.attempt_sequence,
                    artifact.attempt_sha256,
                    lease.lease_generation,
                    lease.fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ProductAgentConflict(
                    "prepared elder narrative commit CAS failed"
                )
            terminal_operation = {
                **operation_json,
                "result": {
                    "schema_version": "product_elder_narrative_result.v1",
                    "narrative": artifact.narrative.model_dump(mode="json"),
                    "provider_usage": artifact.provider_usage.model_dump(
                        mode="json"
                    ),
                },
            }
            cursor.execute(
                """
                UPDATE public.sleep_domain_operations
                SET status = 'succeeded', outcome_class = 'succeeded',
                    operation_json = %s::jsonb, updated_at = %s,
                    cas_version = cas_version + 1,
                    lease_owner = NULL, lease_expires_at = NULL,
                    fencing_token = NULL, worker_instance = NULL,
                    heartbeat_at = NULL
                WHERE operation_id = %s AND namespace_id = %s
                  AND data_mode = %s AND namespace_generation = %s
                  AND subject_id = %s AND status = 'running'
                  AND lease_generation = %s AND fencing_token = %s
                  AND worker_instance = %s
                  AND lease_expires_at > clock_timestamp()
                """,
                (
                    _json(terminal_operation),
                    committed_at,
                    lease.operation_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.subject_id,
                    lease.lease_generation,
                    lease.fencing_token,
                    lease.worker_instance,
                ),
            )
            if cursor.rowcount != 1:
                raise ProductAgentLeaseLost(
                    "elder narrative operation fence expired before commit"
                )
        finally:
            cursor.close()
        return ProductElderNarrativeCommitResult(
            operation_id=lease.operation_id,
            product_attempt_id=artifact.product_attempt_id,
            state=artifact.narrative.state.value,
            narrative=artifact.narrative,
            provider_usage=artifact.provider_usage,
        )

    def commit_prepared(
        self,
        lease: ProductAgentLease,
        artifact: PreparedProductAgentArtifact,
        *,
        committed_at: datetime,
        source: LoadedProductAgentSource | None = None,
        runtime_manifest: Mapping[str, Any] | None = None,
        projection_manifest: Mapping[str, Any] | None = None,
    ) -> ProductAgentCommitResult:
        self._validate_artifact_identity(lease, artifact)
        self._lock_and_validate_epochs()
        elder_narrative_operation_id: str | None = None
        elder_narrative_operation_created = False
        cursor = self.connection.cursor()
        try:
            operation_json = self._lock_operation_fence(cursor, lease, artifact)
            self._lock_analysis_publication(cursor, artifact)
            if artifact.schema_version == "product_agent_prepared_attempt.v3":
                if (
                    source is None
                    or runtime_manifest is None
                    or projection_manifest is None
                ):
                    raise ProductAgentInvariantError(
                        "v3 final commit has no complete source/runtime fence"
                    )
                self._lock_final_context_writers(cursor)
                habit_profile, memory_state = (
                    self.revalidate_provider_source_fence(
                        lease,
                        source=source,
                        operation_type=PRODUCT_SHARED_ANALYSIS_OPERATION,
                    )
                )
                (
                    current_context_sha256,
                    current_runtime_sha256,
                    current_desired_sha256,
                ) = _recompute_shared_identity(
                    scope=self.scope,
                    source=source,
                    habit_profile=habit_profile,
                    memory_state=memory_state,
                    as_of=committed_at,
                    runtime_manifest=runtime_manifest,
                )
                if (
                    current_context_sha256
                    != artifact.consumed_context_sha256
                    or current_runtime_sha256
                    != artifact.runtime_manifest_sha256
                    or current_desired_sha256
                    != artifact.desired_analysis_sha256
                    or dict(runtime_manifest) != artifact.runtime_manifest
                    or dict(projection_manifest)
                    != artifact.projection_manifest
                    or stable_hash(projection_manifest)
                    != artifact.projection_manifest_sha256
                    or current_context_sha256
                    != _required_string(
                        operation_json,
                        "consumed_context_sha256",
                    )
                    or current_runtime_sha256
                    != _required_string(
                        operation_json,
                        "runtime_manifest_sha256",
                    )
                    or current_desired_sha256
                    != _required_string(
                        operation_json,
                        "desired_analysis_sha256",
                    )
                ):
                    raise ProductAgentStaleSource(
                        "shared source/context/manifest changed before final commit"
                    )
            cursor.execute(
                """
                SELECT current_revision_id, episode_local_date,
                       assignment_basis
                FROM public.sleep_domain_night_episodes
                WHERE night_episode_id = %s AND namespace_id = %s
                  AND data_mode = %s AND namespace_generation = %s
                  AND subject_id = %s
                  AND COALESCE(run_id, '') = COALESCE(%s, '')
                  AND COALESCE(arm_id, '') = COALESCE(%s, '')
                  AND date_state = 'finalized' AND date_conflict = FALSE
                FOR UPDATE
                """,
                (
                    artifact.night_episode_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.subject_id,
                    self.scope.run_id,
                    self.scope.arm_id,
                ),
            )
            episode_row = cursor.fetchone()
            if episode_row is None:
                raise ProductAgentInvariantError(
                    "Product source Episode is unavailable"
                )
            if str(episode_row[0]) != artifact.night_episode_revision_id:
                raise ProductAgentStaleSource(
                    "Product source Episode revision changed before commit"
                )
            if episode_row[1] is None or episode_row[2] is None:
                raise ProductAgentInvariantError(
                    "Product source Episode has no committed date contract"
                )
            cursor.execute(
                """
                SELECT attempt_sha256, attempt_state, query_visible,
                       lease_generation, fencing_token
                FROM public.backend_product_attempts
                WHERE product_attempt_id = %s AND operation_id = %s
                  AND attempt_sequence = %s
                FOR UPDATE
                """,
                (
                    artifact.product_attempt_id,
                    lease.operation_id,
                    lease.attempt_sequence,
                ),
            )
            attempt_row = cursor.fetchone()
            if attempt_row is None or (
                str(attempt_row[0]) != artifact.attempt_sha256
                or str(attempt_row[1]) != "prepared"
                or bool(attempt_row[2])
                or int(attempt_row[3]) != lease.lease_generation
                or str(attempt_row[4]) != lease.fencing_token
            ):
                raise ProductAgentConflict(
                    "prepared Product attempt changed before commit"
                )
            if artifact.schema_version == "product_agent_prepared_attempt.v3":
                artifact = self._rebase_shared_analysis_parent(
                    cursor,
                    lease,
                    artifact,
                )
            else:
                self._validate_analysis_parent(cursor, artifact.analysis)
            analysis = artifact.analysis
            analysis_json = (
                analysis.model_dump(mode="json")
                if artifact.schema_version == "product_agent_prepared_attempt.v2"
                else {
                    "schema_version": "shared_night_analysis.v1",
                    "analysis_revision": analysis.model_dump(mode="json"),
                    "desired_analysis_sha256": artifact.desired_analysis_sha256,
                    "consumed_context_sha256": artifact.consumed_context_sha256,
                    "runtime_manifest_sha256": artifact.runtime_manifest_sha256,
                    "projection_manifest": artifact.projection_manifest,
                    "projection_manifest_sha256": (
                        artifact.projection_manifest_sha256
                    ),
                    "projection_identities": artifact.projection_identities,
                    "provider_usage": artifact.provider_usage.model_dump(
                        mode="json"
                    ),
                    **(
                        {
                            "shadow_comparison": (
                                artifact.shadow_comparison.model_dump(mode="json")
                            )
                        }
                        if artifact.shadow_comparison is not None
                        else {}
                    ),
                    "shared_analysis": (
                        None
                        if artifact.shared_analysis is None
                        else artifact.shared_analysis.model_dump(mode="json")
                    ),
                }
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_analysis_revisions (
                  analysis_revision_id, namespace_id, data_mode,
                  night_episode_id, night_episode_revision_id, subject_id,
                  revision_number, parent_analysis_revision_id,
                  analysis_run_id, analysis_json, created_at
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
                )
                """,
                (
                    analysis.analysis_revision_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    analysis.night_episode_id,
                    analysis.night_episode_revision_id,
                    analysis.subject_id,
                    analysis.revision_number,
                    analysis.parent_analysis_revision_id,
                    analysis.analysis_run_id,
                    _json(analysis_json),
                    committed_at,
                ),
            )
            persisted_receipt_ids: set[str] = set()
            for role_run in artifact.role_runs:
                request_receipts = role_run.fact_snapshot.memory_read_receipt_refs
                personalization = role_run.personalization
                if (
                    artifact.schema_version
                    == "product_agent_prepared_attempt.v2"
                    and tuple(
                        item.receipt_id
                        for item in personalization.memory_read_receipts
                    )
                    != request_receipts
                ):
                    raise ProductAgentInvariantError(
                        "prepared Memory receipt binding drifted"
                    )
                for receipt in personalization.memory_read_receipts:
                    if receipt.receipt_id in persisted_receipt_ids:
                        continue
                    persisted_receipt_ids.add(receipt.receipt_id)
                    cursor.execute(
                        """
                        INSERT INTO public.backend_memory_read_receipts_v2 (
                          receipt_id, namespace_id, data_mode,
                          namespace_generation, run_id, arm_id, subject_id,
                          product_episode_id, requesting_agent, purpose,
                          query_sha256, result_sha256, receipt_sha256,
                          authorization_epoch, privacy_epoch, receipt_json,
                          completed_at
                        ) VALUES (
                          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s::jsonb, %s
                        )
                        """,
                        (
                            receipt.receipt_id,
                            self.scope.namespace_id,
                            self.scope.data_mode,
                            self.scope.namespace_generation,
                            self.scope.run_id,
                            self.scope.arm_id,
                            self.scope.subject_id,
                            role_run.product_episode_id,
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
            for role_run in artifact.role_runs:
                view = role_run.role_view
                persisted_view_json = (
                    view.model_dump_json()
                    if role_run.role_projection is None
                    else role_run.role_projection.model_dump_json()
                )
                projection_sha256 = (
                    stable_hash(view.model_dump(mode="json"))
                    if role_run.role_projection is None
                    else _required_projection_identity(
                        artifact,
                        role_run.role,
                    )
                )
                public_today = _public_today_projection(
                    analysis=analysis,
                    view=view,
                    projection_version=artifact.source_state_version,
                    episode_local_date=episode_row[1],
                    assignment_basis=str(episode_row[2]),
                    committed_at=committed_at,
                )
                cursor.execute(
                    """
                    INSERT INTO public.sleep_domain_analysis_role_views (
                      role_view_id, namespace_id, data_mode,
                      analysis_revision_id, night_episode_id,
                      night_episode_revision_id, subject_id, role, status,
                      product_agent_episode_id, view_json, generated_at,
                      protocol_version, namespace_generation, run_id, arm_id,
                      source_fact_snapshot_sha256, source_state_version,
                      authorization_epoch, privacy_epoch,
                      retrieval_policy_epoch, policy_sha256,
                      projection_sha256, public_schema_version,
                      public_today_json, public_projection_sha256,
                      public_committed_at
                    ) VALUES (
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s::jsonb, %s, 2, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, 'product_sleep_today.v1', %s::jsonb,
                      %s, %s
                    )
                    """,
                    (
                        view.role_view_id,
                        self.scope.namespace_id,
                        self.scope.data_mode,
                        view.analysis_revision_id,
                        view.night_episode_id,
                        view.night_episode_revision_id,
                        view.subject_id,
                        view.role.value,
                        view.status.value,
                        view.product_agent_episode_id,
                        persisted_view_json,
                        committed_at,
                        self.scope.namespace_generation,
                        self.scope.run_id,
                        self.scope.arm_id,
                        role_run.fact_snapshot.fact_snapshot_hash,
                        artifact.source_state_version,
                        self.scope.authorization_epoch,
                        self.scope.privacy_epoch,
                        self.scope.retrieval_policy_epoch,
                        artifact.policy_sha256,
                        projection_sha256,
                        json.dumps(
                            public_today,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        stable_hash(public_today),
                        committed_at,
                    ),
                )
            cursor.execute(
                """
                UPDATE public.backend_product_attempts
                SET attempt_state = 'committed', query_visible = TRUE,
                    committed_at = %s
                WHERE product_attempt_id = %s AND operation_id = %s
                  AND attempt_sequence = %s AND attempt_state = 'prepared'
                  AND query_visible = FALSE AND attempt_sha256 = %s
                  AND lease_generation = %s AND fencing_token = %s
                """,
                (
                    committed_at,
                    artifact.product_attempt_id,
                    lease.operation_id,
                    lease.attempt_sequence,
                    artifact.attempt_sha256,
                    lease.lease_generation,
                    lease.fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ProductAgentConflict(
                    "prepared Product attempt commit CAS failed"
                )
            if artifact.schema_version == "product_agent_prepared_attempt.v3":
                assert source is not None
                assert artifact.shared_analysis is not None
                (
                    elder_narrative_operation_id,
                    elder_narrative_operation_created,
                ) = self._reserve_elder_narrative(
                    cursor,
                    artifact=artifact,
                    source_operation_json=operation_json,
                    wake_date=episode_row[1],
                    reserved_at=committed_at,
                    elder_message_atoms=build_elder_message_atoms(
                        artifact.shared_analysis,
                        source.facts.elder_presentation_facts(),
                    ),
                )
            induction_operation_id = self.id_generator(committed_at)
            induction_manifest_id = self.id_generator(committed_at)
            induction_manifest = {
                "schema_version": "induction_manifest.v1",
                "manifest_id": induction_manifest_id,
                "source_product_operation_id": lease.operation_id,
                "analysis_revision_id": analysis.analysis_revision_id,
                "night_episode_id": analysis.night_episode_id,
                "night_episode_revision_id": analysis.night_episode_revision_id,
                "analysis_status": analysis.status.value,
                "source_fact_snapshot_sha256": (
                    artifact.source_fact_snapshot_sha256
                ),
                "policy_sha256": artifact.policy_sha256,
                "role_projections": [
                    {
                        "role": item.role.value,
                        "role_view_id": item.role_view.role_view_id,
                        "projection_sha256": (
                            _required_projection_identity(
                                artifact,
                                item.role,
                            )
                            if artifact.schema_version
                            == "product_agent_prepared_attempt.v3"
                            else stable_hash(
                                item.role_view.model_dump(mode="json")
                            )
                        ),
                    }
                    for item in artifact.role_runs
                ],
                "namespace_generation": self.scope.namespace_generation,
                "authorization_epoch": self.scope.authorization_epoch,
                "privacy_epoch": self.scope.privacy_epoch,
                "retrieval_policy_epoch": self.scope.retrieval_policy_epoch,
                "projector_version": "deterministic_personalization.v1",
                "synthetic_non_release": self.scope.data_mode == "replay",
            }
            induction_manifest_sha256 = stable_hash(induction_manifest)
            induction_semantic_key = stable_hash(
                {
                    "stage": "induction",
                    "analysis_revision_id": analysis.analysis_revision_id,
                    "manifest_sha256": induction_manifest_sha256,
                }
            )
            induction_workload = _workload_snapshot(self.scope, "induction")
            induction_operation = {
                "schema_version": "induction_operation.v1",
                "manifest_id": induction_manifest_id,
                "manifest_sha256": induction_manifest_sha256,
                "analysis_revision_id": analysis.analysis_revision_id,
                "night_episode_revision_id": analysis.night_episode_revision_id,
                "projector_version": "deterministic_personalization.v1",
                "authorization_snapshot": induction_workload,
            }
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
                  available_at, max_attempts,
                  workload_authorization_snapshot_json, policy_sha256
                ) VALUES (
                  %s, %s, %s, 'induction', %s, %s, NULL, %s, %s, %s, %s,
                  'pending', 0, 0, %s::jsonb, %s, %s, 2, %s, %s, %s,
                  'uuidv7', 'system', %s, 'induction', 10, %s, 5,
                  %s::jsonb, %s
                )
                """,
                (
                    induction_operation_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    analysis.subject_id,
                    self.scope.service_principal_id,
                    induction_manifest_id,
                    analysis.analysis_revision_id,
                    induction_semantic_key,
                    induction_semantic_key,
                    _json(induction_operation),
                    committed_at,
                    committed_at,
                    self.scope.namespace_generation,
                    self.scope.run_id,
                    self.scope.arm_id,
                    induction_semantic_key,
                    committed_at,
                    _json(induction_workload),
                    artifact.policy_sha256,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_induction_manifests_v2 (
                  manifest_id, operation_id, source_product_operation_id,
                  namespace_id, data_mode, namespace_generation, run_id,
                  arm_id, subject_id, analysis_revision_id,
                  night_episode_revision_id, source_fact_snapshot_sha256,
                  policy_sha256, manifest_sha256, manifest_json, created_at
                ) VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s::jsonb, %s
                )
                """,
                (
                    induction_manifest_id,
                    induction_operation_id,
                    lease.operation_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.run_id,
                    self.scope.arm_id,
                    analysis.subject_id,
                    analysis.analysis_revision_id,
                    analysis.night_episode_revision_id,
                    artifact.source_fact_snapshot_sha256,
                    artifact.policy_sha256,
                    induction_manifest_sha256,
                    _json(induction_manifest),
                    committed_at,
                ),
            )
            terminal_operation = {
                **operation_json,
                "result_ref": analysis.analysis_revision_id,
                "result": {
                    "schema_version": "product_agent_result.v1",
                    "product_attempt_id": artifact.product_attempt_id,
                    "analysis_revision_id": analysis.analysis_revision_id,
                    "night_episode_revision_id": (
                        analysis.night_episode_revision_id
                    ),
                    "role_view_ids": [
                        item.role_view.role_view_id
                        for item in artifact.role_runs
                    ],
                    "analysis_status": analysis.status.value,
                    "induction_operation_id": induction_operation_id,
                    "induction_manifest_id": induction_manifest_id,
                    **(
                        {
                            "shadow_comparison": (
                                artifact.shadow_comparison.model_dump(mode="json")
                            )
                        }
                        if artifact.shadow_comparison is not None
                        else {}
                    ),
                    **(
                        {
                            "elder_narrative_operation_id": (
                                elder_narrative_operation_id
                            ),
                            "elder_narrative_operation_created": (
                                elder_narrative_operation_created
                            ),
                        }
                        if elder_narrative_operation_id is not None
                        else {}
                    ),
                },
            }
            cursor.execute(
                """
                UPDATE public.sleep_domain_operations
                SET status = 'succeeded', outcome_class = 'succeeded',
                    operation_json = %s::jsonb, updated_at = %s,
                    cas_version = cas_version + 1,
                    lease_owner = NULL, lease_expires_at = NULL,
                    fencing_token = NULL, worker_instance = NULL,
                    heartbeat_at = NULL
                WHERE operation_id = %s AND namespace_id = %s
                  AND data_mode = %s AND namespace_generation = %s
                  AND subject_id = %s AND status = 'running'
                  AND lease_generation = %s AND fencing_token = %s
                  AND worker_instance = %s
                  AND lease_expires_at > clock_timestamp()
                """,
                (
                    _json(terminal_operation),
                    committed_at,
                    lease.operation_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.subject_id,
                    lease.lease_generation,
                    lease.fencing_token,
                    lease.worker_instance,
                ),
            )
            if cursor.rowcount != 1:
                raise ProductAgentLeaseLost(
                    "Product operation fence expired before atomic commit"
                )
            if artifact.schema_version == "product_agent_prepared_attempt.v3":
                product_result = terminal_operation.get("result")
                if not isinstance(product_result, Mapping):
                    raise ProductAgentInvariantError(
                        "shared terminal operation omitted its typed result"
                    )
                self._complete_compatibilities_for_shared(
                    cursor,
                    shared_operation_id=lease.operation_id,
                    product_result=product_result,
                    elder_narrative_operation_id=(
                        elder_narrative_operation_id
                    ),
                    elder_narrative_operation_created=(
                        elder_narrative_operation_created
                    ),
                    completed_at=committed_at,
                )
            event_type = (
                "AGENT_ANALYSIS_READY"
                if analysis.status == AnalysisStatus.READY
                else "AGENT_ANALYSIS_DEGRADED"
            )
            event_id = self.id_generator(committed_at)
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_domain_outbox (
                  event_id, namespace_id, data_mode, event_type,
                  aggregate_type, aggregate_id, aggregate_version,
                  per_aggregate_sequence, subject_id, operation_id,
                  status, available_at, event_json, created_at,
                  protocol_version, namespace_generation, run_id, arm_id
                ) VALUES (
                  %s, %s, %s, %s, 'AnalysisRevision', %s, %s, 1,
                  %s, %s, 'committed', %s, %s::jsonb, %s,
                  2, %s, %s, %s
                )
                """,
                (
                    event_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    event_type,
                    analysis.analysis_revision_id,
                    analysis.revision_number,
                    analysis.subject_id,
                    lease.operation_id,
                    committed_at,
                    _json(
                        {
                            "schema_version": "committed_event.v2",
                            "event_id": event_id,
                            "event_type": event_type,
                            "analysis_revision_id": analysis.analysis_revision_id,
                            "night_episode_id": analysis.night_episode_id,
                            "night_episode_revision_id": (
                                analysis.night_episode_revision_id
                            ),
                            "operation_id": lease.operation_id,
                            "roles": [role.value for role in AnalysisRole],
                            "induction_operation_id": induction_operation_id,
                            "induction_manifest_id": induction_manifest_id,
                            "synthetic_non_release": (
                                self.scope.data_mode == "replay"
                            ),
                        }
                    ),
                    committed_at,
                    self.scope.namespace_generation,
                    self.scope.run_id,
                    self.scope.arm_id,
                ),
            )
        finally:
            cursor.close()
        return ProductAgentCommitResult(
            operation_id=lease.operation_id,
            product_attempt_id=artifact.product_attempt_id,
            analysis_revision_id=artifact.analysis.analysis_revision_id,
            role_view_ids=tuple(
                item.role_view.role_view_id for item in artifact.role_runs
            ),
            analysis_status=artifact.analysis.status.value,
            induction_operation_id=induction_operation_id,
            induction_manifest_id=induction_manifest_id,
            elder_narrative_operation_id=elder_narrative_operation_id,
            elder_narrative_operation_created=(
                elder_narrative_operation_created
            ),
        )

    def _lease_scope_params(self, lease: ProductAgentLease) -> tuple[Any, ...]:
        return (
            lease.operation_id,
            self.scope.namespace_id,
            self.scope.data_mode,
            self.scope.namespace_generation,
            self.scope.subject_id,
            self.scope.run_id,
            self.scope.arm_id,
            lease.lease_generation,
            lease.fencing_token,
            lease.worker_instance,
        )

    def _lock_and_validate_epochs(self) -> tuple[int, int, int]:
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                SELECT authorization_epoch, privacy_epoch,
                       retrieval_policy_epoch
                FROM public.backend_subject_epochs
                WHERE namespace_id = %s AND data_mode = %s
                  AND subject_id = %s
                FOR SHARE
                """,
                (
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.subject_id,
                ),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
        expected = (
            self.scope.authorization_epoch,
            self.scope.privacy_epoch,
            self.scope.retrieval_policy_epoch,
        )
        if row is None or tuple(int(value) for value in row) != expected:
            raise ProductAgentStaleSource(
                "Product governance epochs changed before commit"
            )
        return tuple(int(value) for value in row)  # type: ignore[return-value]

    def _lock_operation_fence(
        self,
        cursor: Any,
        lease: ProductAgentLease,
        artifact: PreparedProductAgentArtifact,
    ) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT operation_json, target_resource_id, policy_sha256
            FROM public.sleep_domain_operations
            WHERE operation_id = %s AND namespace_id = %s
              AND data_mode = %s AND namespace_generation = %s
              AND subject_id = %s
              AND run_id IS NOT DISTINCT FROM %s
              AND arm_id IS NOT DISTINCT FROM %s
              AND operation_type IN (
                'product_agent',
                'product.report.run.v1',
                'product.shared_analysis.v1'
              )
              AND queue_name = 'product_agent' AND status = 'running'
              AND lease_generation = %s AND fencing_token = %s
              AND worker_instance = %s
              AND attempt_count = %s
              AND lease_expires_at > clock_timestamp()
            FOR UPDATE
            """,
            (*self._lease_scope_params(lease), lease.attempt_sequence),
        )
        row = cursor.fetchone()
        if row is None:
            raise ProductAgentLeaseLost("Product operation fence was rejected")
        operation_json = _json_value(row[0])
        if (
            str(row[1]) != artifact.night_episode_id
            or str(row[2]) != artifact.policy_sha256
            or _required_string(operation_json, "night_episode_revision_id")
            != artifact.night_episode_revision_id
        ):
            raise ProductAgentConflict(
                "Product operation and prepared artifact identity differ"
            )
        return operation_json

    def _lock_narrative_operation_fence(
        self,
        cursor: Any,
        lease: ProductAgentLease,
        artifact: PreparedElderNarrativeArtifact,
    ) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT operation_json, target_resource_id, policy_sha256
            FROM public.sleep_domain_operations
            WHERE operation_id = %s AND namespace_id = %s
              AND data_mode = %s AND namespace_generation = %s
              AND subject_id = %s
              AND run_id IS NOT DISTINCT FROM %s
              AND arm_id IS NOT DISTINCT FROM %s
              AND operation_type = 'product.elder_narrative.v1'
              AND queue_name = 'product_agent' AND status = 'running'
              AND lease_generation = %s AND fencing_token = %s
              AND worker_instance = %s AND attempt_count = %s
              AND lease_expires_at > clock_timestamp()
            FOR UPDATE
            """,
            (*self._lease_scope_params(lease), lease.attempt_sequence),
        )
        row = cursor.fetchone()
        if row is None:
            raise ProductAgentLeaseLost(
                "elder narrative operation fence was rejected"
            )
        operation_json = _json_value(row[0])
        if (
            str(row[1]) != artifact.shared_analysis_revision_id
            or str(row[2]) != artifact.policy_sha256
            or _required_string(operation_json, "night_episode_revision_id")
            != artifact.night_episode_revision_id
            or _required_string(operation_json, "shared_product_attempt_id")
            != artifact.shared_product_attempt_id
            or _required_string(operation_json, "shared_analysis_revision_id")
            != artifact.shared_analysis_revision_id
            or _required_string(operation_json, "shared_analysis_sha256")
            != artifact.shared_analysis_sha256
            or _required_string(operation_json, "elder_projection_sha256")
            != artifact.source_projection_sha256
            or _required_string(operation_json, "narrative_manifest_sha256")
            != artifact.narrative_manifest_sha256
            or _required_string(operation_json, "render_identity_sha256")
            != artifact.render_identity_sha256
            or operation_json.get("narrative_manifest")
            != artifact.narrative_manifest
        ):
            raise ProductAgentConflict(
                "elder narrative operation and prepared artifact differ"
            )
        return operation_json

    def _lock_report_request_fence(
        self,
        cursor: Any,
        lease: ProductAgentLease,
    ) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT operation_json
            FROM public.sleep_domain_operations
            WHERE operation_id = %s AND namespace_id = %s
              AND data_mode = %s AND namespace_generation = %s
              AND subject_id = %s
              AND run_id IS NOT DISTINCT FROM %s
              AND arm_id IS NOT DISTINCT FROM %s
              AND operation_type = 'product.report.run.v1'
              AND queue_name = 'product_agent' AND status = 'running'
              AND lease_generation = %s AND fencing_token = %s
              AND worker_instance = %s AND attempt_count = %s
              AND lease_expires_at > clock_timestamp()
            FOR UPDATE
            """,
            (*self._lease_scope_params(lease), lease.attempt_sequence),
        )
        row = cursor.fetchone()
        if row is None:
            raise ProductAgentLeaseLost("report request operation fence was rejected")
        return _json_value(row[0])

    def _lock_current_report_gate(
        self,
        cursor: Any,
        *,
        source: LoadedProductAgentSource,
    ) -> tuple[
        str,
        str,
        DeterministicQualityAssessment,
        str,
        CurrentRisk,
    ]:
        cursor.execute(
            """
            SELECT episode.current_revision_id, revision.revision_json,
                   quality.assessment_id, quality.assessment_json,
                   risk.current_risk_id, risk.risk_json
            FROM public.sleep_domain_night_episodes AS episode
            JOIN public.sleep_domain_night_episode_revisions AS revision
              ON revision.night_episode_revision_id = episode.current_revision_id
             AND revision.night_episode_id = episode.night_episode_id
             AND revision.namespace_id = episode.namespace_id
             AND revision.data_mode = episode.data_mode
             AND revision.namespace_generation = episode.namespace_generation
             AND revision.subject_id = episode.subject_id
            JOIN public.sleep_domain_current_quality AS quality
              ON quality.namespace_id = episode.namespace_id
             AND quality.data_mode = episode.data_mode
             AND quality.subject_id = episode.subject_id
             AND quality.night_episode_id = episode.night_episode_id
            JOIN public.sleep_domain_current_risk AS risk
              ON risk.namespace_id = episode.namespace_id
             AND risk.data_mode = episode.data_mode
             AND risk.subject_id = episode.subject_id
             AND risk.night_episode_id = episode.night_episode_id
            WHERE episode.night_episode_id = %s
              AND episode.namespace_id = %s AND episode.data_mode = %s
              AND episode.namespace_generation = %s
              AND episode.subject_id = %s
              AND episode.run_id IS NOT DISTINCT FROM %s
              AND episode.arm_id IS NOT DISTINCT FROM %s
              AND episode.date_state = 'finalized'
              AND episode.date_conflict = FALSE
              AND revision.date_state = 'finalized'
              AND revision.date_conflict = FALSE
            FOR SHARE OF episode, revision, quality, risk
            """,
            (
                source.night_episode_id,
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.subject_id,
                self.scope.run_id,
                self.scope.arm_id,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise ProductAgentStaleSource(
                "report request has no current authoritative gate"
            )
        current_revision_id = str(row[0])
        revision_json = _json_value(row[1])
        observation_ids = tuple(
            str(value) for value in revision_json.get("observation_ids", ())
        )
        quality = DeterministicQualityAssessment.model_validate(
            _json_value(row[3])
        )
        risk = CurrentRisk.model_validate(_json_value(row[5]))
        if not observation_ids or len(observation_ids) != len(
            set(observation_ids)
        ):
            raise ProductAgentInvariantError(
                "current report gate has no exact observation set"
            )
        _require_exact_fast_path_source(
            pinned_revision_id=current_revision_id,
            observation_ids=observation_ids,
            quality=quality,
            risk=risk,
        )
        return (
            current_revision_id,
            str(row[2]),
            quality,
            str(row[4]),
            risk,
        )

    def _reroute_changed_report_gate(
        self,
        cursor: Any,
        lease: ProductAgentLease,
        *,
        operation_json: Mapping[str, Any],
        current_revision_id: str,
        current_quality_id: str,
        current_risk_id: str,
        rerouted_at: datetime,
    ) -> None:
        """Refresh a request fence and let the durable worker resolve it again."""

        if operation_json.get("compatibility_product_agent_operation_id"):
            self._fail_request_compatibility(
                cursor,
                request_operation_id=lease.operation_id,
                request_json=operation_json,
                error_code="product_report_source_superseded",
                failed_at=rerouted_at,
            )
        refreshed = {
            key: value
            for key, value in operation_json.items()
            if key
            not in {
                "compatibility_product_agent_operation_id",
                "report_result",
                "shared_operation_id",
            }
        }
        refreshed.update(
            {
                "night_episode_revision_id": current_revision_id,
                "quality_assessment_id": current_quality_id,
                "current_risk_id": current_risk_id,
                "gate_reroute_count": int(
                    operation_json.get("gate_reroute_count") or 0
                )
                + 1,
            }
        )
        cursor.execute(
            """
            UPDATE public.sleep_domain_operations
            SET status = 'retry', outcome_class = NULL,
                operation_json = %s::jsonb, available_at = %s,
                updated_at = %s, cas_version = cas_version + 1,
                lease_owner = NULL, lease_expires_at = NULL,
                fencing_token = NULL, worker_instance = NULL,
                heartbeat_at = NULL
            WHERE operation_id = %s AND namespace_id = %s
              AND data_mode = %s AND namespace_generation = %s
              AND subject_id = %s AND status = 'running'
              AND lease_generation = %s AND fencing_token = %s
              AND worker_instance = %s
              AND lease_expires_at > clock_timestamp()
            """,
            (
                _json(refreshed),
                rerouted_at,
                rerouted_at,
                lease.operation_id,
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.subject_id,
                lease.lease_generation,
                lease.fencing_token,
                lease.worker_instance,
            ),
        )
        if cursor.rowcount != 1:
            raise ProductAgentLeaseLost(
                "report request fence expired before gate reroute"
            )

    def _lock_prepared_artifact(
        self,
        cursor: Any,
        artifact: PreparedProductAgentArtifact
        | PreparedElderNarrativeArtifact,
    ) -> tuple[int, int, str, bool] | None:
        cursor.execute(
            """
            SELECT attempt_sequence, lease_generation, fencing_token, (
                     namespace_id = %s
                     AND data_mode = %s
                     AND namespace_generation = %s
                     AND run_id IS NOT DISTINCT FROM %s
                     AND arm_id IS NOT DISTINCT FROM %s
                     AND subject_id = %s
                     AND operation_id = %s
                     AND product_attempt_id = %s
                     AND night_episode_revision_id = %s
                     AND fact_snapshot_sha256 = %s
                     AND state_version = %s
                     AND authorization_epoch = %s
                     AND privacy_epoch = %s
                     AND retrieval_policy_epoch = %s
                     AND policy_sha256 = %s
                     AND attempt_sha256 = %s
                     AND attempt_json = %s::jsonb
                     AND prepared_at = %s
                     AND attempt_state = 'prepared'
                     AND query_visible = FALSE
                     AND committed_at IS NULL
                   ) AS immutable_matches
            FROM public.backend_product_attempts
            WHERE product_attempt_id = %s
            FOR UPDATE
            """,
            (
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.run_id,
                self.scope.arm_id,
                self.scope.subject_id,
                artifact.operation_id,
                artifact.product_attempt_id,
                artifact.night_episode_revision_id,
                artifact.source_fact_snapshot_sha256,
                artifact.source_state_version,
                self.scope.authorization_epoch,
                self.scope.privacy_epoch,
                self.scope.retrieval_policy_epoch,
                artifact.policy_sha256,
                artifact.attempt_sha256,
                artifact.model_dump_json(),
                artifact.prepared_at,
                artifact.product_attempt_id,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return (int(row[0]), int(row[1]), str(row[2]), bool(row[3]))

    def _persist_existing_artifact(
        self,
        cursor: Any,
        lease: ProductAgentLease,
        artifact: PreparedProductAgentArtifact
        | PreparedElderNarrativeArtifact,
        existing: tuple[int, int, str, bool],
    ) -> None:
        old_sequence, old_generation, old_token, immutable_matches = existing
        current_owner = (
            old_sequence == lease.attempt_sequence
            and old_generation == lease.lease_generation
            and old_token == lease.fencing_token
        )
        if not immutable_matches:
            raise ProductAgentConflict("prepared Product artifact content changed")
        if current_owner:
            return
        shared_business_retry = (
            isinstance(artifact, PreparedProductAgentArtifact)
            and artifact.schema_version == "product_agent_prepared_attempt.v3"
            and lease.attempt_sequence > old_sequence
        )
        if (
            (
                old_sequence != lease.attempt_sequence
                and not shared_business_retry
            )
            or (
                old_sequence == lease.attempt_sequence
                and old_generation >= lease.lease_generation
            )
            or old_token == lease.fencing_token
        ):
            raise ProductAgentConflict(
                "prepared Product artifact owner cannot be taken over"
            )

        cursor.execute(
            """
            UPDATE public.backend_product_attempts
            SET attempt_sequence = %s, lease_generation = %s,
                fencing_token = %s
            WHERE product_attempt_id = %s AND operation_id = %s
              AND attempt_sequence = %s AND lease_generation = %s
              AND fencing_token = %s AND attempt_state = 'prepared'
              AND query_visible = FALSE AND committed_at IS NULL
              AND attempt_sha256 = %s
            RETURNING attempt_sequence, lease_generation, fencing_token
            """,
            (
                lease.attempt_sequence,
                lease.lease_generation,
                lease.fencing_token,
                artifact.product_attempt_id,
                lease.operation_id,
                old_sequence,
                old_generation,
                old_token,
                artifact.attempt_sha256,
            ),
        )
        updated = cursor.fetchone()
        if updated is not None:
            return

        reread = self._lock_prepared_artifact(cursor, artifact)
        if reread is not None and reread[3] and (
            reread[0] == lease.attempt_sequence
            and reread[1] == lease.lease_generation
            and reread[2] == lease.fencing_token
        ):
            return
        raise ProductAgentConflict("prepared Product artifact takeover CAS failed")

    def _lock_analysis_publication(
        self,
        cursor: Any,
        artifact: PreparedProductAgentArtifact,
    ) -> None:
        """Serialize publishers for one exact NightEpisode revision."""

        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 43))",
            (
                ":".join(
                    (
                        "analysis-publication",
                        self.scope.namespace_id,
                        self.scope.data_mode,
                        str(self.scope.namespace_generation),
                        self.scope.run_id or "",
                        self.scope.arm_id or "",
                        self.scope.subject_id or "",
                        artifact.night_episode_revision_id,
                    )
                ),
            ),
        )

    def _lock_final_context_writers(self, cursor: Any) -> None:
        """Block Habit/Memory writers through the final identity commit."""

        for capability in PRODUCT_FINAL_CONTEXT_LOCK_ORDER:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 42))",
                (
                    ":".join(
                        (
                            "l2",
                            capability,
                            self.scope.namespace_id,
                            self.scope.data_mode,
                            str(self.scope.namespace_generation),
                            self.scope.run_id or "",
                            self.scope.arm_id or "",
                            self.scope.subject_id or "",
                        )
                    ),
                ),
            )

    def _rebase_shared_analysis_parent(
        self,
        cursor: Any,
        lease: ProductAgentLease,
        artifact: PreparedProductAgentArtifact,
    ) -> PreparedProductAgentArtifact:
        """Allocate the shared revision envelope under the publication lock.

        Provider-derived content and stable artifact identifiers are unchanged;
        only the database revision number and parent pointer may be refreshed.
        """

        cursor.execute(
            """
            SELECT analysis_revision_id, revision_number
            FROM public.sleep_domain_analysis_revisions
            WHERE namespace_id = %s AND data_mode = %s
              AND night_episode_revision_id = %s
            ORDER BY revision_number DESC
            LIMIT 1
            FOR UPDATE
            """,
            (
                self.scope.namespace_id,
                self.scope.data_mode,
                artifact.analysis.night_episode_revision_id,
            ),
        )
        prior = cursor.fetchone()
        expected_number = 1 if prior is None else int(prior[1]) + 1
        expected_parent = None if prior is None else str(prior[0])
        if (
            artifact.analysis.revision_number == expected_number
            and artifact.analysis.parent_analysis_revision_id == expected_parent
        ):
            return artifact

        old_attempt_sha256 = artifact.attempt_sha256
        analysis = artifact.analysis.model_copy(
            update={
                "revision_number": expected_number,
                "parent_analysis_revision_id": expected_parent,
            }
        )
        rebased = artifact.model_copy(update={"analysis": analysis})
        cursor.execute(
            """
            UPDATE public.backend_product_attempts
            SET attempt_sha256 = %s, attempt_json = %s::jsonb
            WHERE product_attempt_id = %s AND operation_id = %s
              AND attempt_sequence = %s AND attempt_state = 'prepared'
              AND query_visible = FALSE AND attempt_sha256 = %s
              AND lease_generation = %s AND fencing_token = %s
            """,
            (
                rebased.attempt_sha256,
                rebased.model_dump_json(),
                rebased.product_attempt_id,
                lease.operation_id,
                lease.attempt_sequence,
                old_attempt_sha256,
                lease.lease_generation,
                lease.fencing_token,
            ),
        )
        if cursor.rowcount != 1:
            raise ProductAgentConflict(
                "prepared shared attempt rebase CAS failed"
            )
        return rebased

    def _validate_analysis_parent(
        self,
        cursor: Any,
        analysis: AnalysisRevision,
    ) -> None:
        cursor.execute(
            """
            SELECT analysis_revision_id, revision_number
            FROM public.sleep_domain_analysis_revisions
            WHERE namespace_id = %s AND data_mode = %s
              AND night_episode_revision_id = %s
            ORDER BY revision_number DESC
            LIMIT 1
            FOR UPDATE
            """,
            (
                self.scope.namespace_id,
                self.scope.data_mode,
                analysis.night_episode_revision_id,
            ),
        )
        prior = cursor.fetchone()
        expected_number = 1 if prior is None else int(prior[1]) + 1
        expected_parent = None if prior is None else str(prior[0])
        if (
            analysis.revision_number != expected_number
            or analysis.parent_analysis_revision_id != expected_parent
        ):
            raise ProductAgentConflict(
                "AnalysisRevision parent changed after prepare"
            )

    def _validate_artifact_identity(
        self,
        lease: ProductAgentLease,
        artifact: PreparedProductAgentArtifact,
    ) -> None:
        if (
            artifact.operation_id != lease.operation_id
            or artifact.analysis.night_episode_id != artifact.night_episode_id
            or artifact.analysis.night_episode_revision_id
            != artifact.night_episode_revision_id
            or artifact.analysis.subject_id != self.scope.subject_id
        ):
            raise ProductAgentInvariantError(
                "prepared Product artifact has another work identity"
            )

    def _validate_narrative_artifact_identity(
        self,
        lease: ProductAgentLease,
        artifact: PreparedElderNarrativeArtifact,
    ) -> None:
        if (
            artifact.operation_id != lease.operation_id
            or self.scope.subject_id is None
            or artifact.night_episode_revision_id == ""
            or artifact.shared_analysis_revision_id == ""
        ):
            raise ProductAgentInvariantError(
                "prepared elder narrative artifact has another work identity"
            )


class ProductAgentWorkHandlerAdapter:
    """Translate a durable Operation claim into the B4 Product processor."""

    def __init__(
        self,
        *,
        processor: ProductAgentProcessor | None = None,
        processor_factory: Callable[
            [UnitOfWorkFactory[Any]], ProductAgentProcessor
        ]
        | None = None,
        model_mode: ModelMode = ModelMode.DETERMINISTIC,
    ) -> None:
        if (processor is None) == (processor_factory is None):
            raise ValueError("provide exactly one Product processor source")
        self._processor = processor
        self._processor_factory = processor_factory
        if model_mode not in {ModelMode.DETERMINISTIC, ModelMode.LIVE}:
            raise ValueError("Product Agent adapter requires a model mode")
        self._model_mode = model_mode

    def __call__(self, context: WorkContext) -> WorkResult:
        try:
            _require_product_claim(context.claim)
            scope = exact_worker_scope(
                context,
                allowed_handler="product_agent",
            )
        except (
            B3ClaimInvariantError,
            ProductAgentInvariantError,
            ValueError,
        ):
            return _terminal("invalid_product_agent_claim_scope")
        lease = ProductAgentLease(
            operation_id=context.claim.work_id,
            attempt_sequence=context.claim.attempt,
            lease_generation=context.claim.lease_generation,
            fencing_token=context.claim.fencing_token,
            worker_instance=context.claim.worker_instance,
        )
        processor: ProductAgentProcessor | Any | None = None
        operation_type = str(
            context.claim.metadata.get("operation_type") or ""
        )

        def fail_compatibility(
            error_code: str,
            *,
            outcome_unknown: bool = False,
        ) -> None:
            if isinstance(processor, ProductAgentProcessor):
                processor.fail_compatibility_bridge(
                    scope,
                    lease,
                    operation_type=operation_type,
                    error_code=error_code,
                    outcome_unknown=outcome_unknown,
                )
            elif processor is None and self._processor_factory is not None:
                failed_at = datetime.now(tz=UTC)
                with worker_uow_factory(context).begin(scope) as uow:
                    repository = PostgresProductAgentRepository(
                        uow.connection,
                        scope,
                    )
                    repository.fail_compatibility_bridge(
                        lease,
                        operation_type=operation_type,
                        error_code=error_code,
                        outcome_unknown=outcome_unknown,
                        failed_at=failed_at,
                    )
                    uow.commit()

        try:
            processor = self._resolve_processor(context)
            if operation_type == PRODUCT_REPORT_RUN_OPERATION:
                if not isinstance(processor, ProductAgentProcessor):
                    raise ProductAgentInvariantError(
                        "report request requires the production Product processor"
                    )
                routed = processor.route_report_request(scope, lease)
                return WorkResult(
                    disposition=WorkDisposition.SUCCEEDED,
                    result={
                        "state": routed.state,
                        "shared_operation_created": (
                            routed.shared_operation_created
                        ),
                        "elder_narrative_operation_created": (
                            routed.elder_narrative_operation_created
                        ),
                    },
                    finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
                )
            if operation_type == PRODUCT_ELDER_NARRATIVE_OPERATION:
                if not isinstance(processor, ProductAgentProcessor):
                    raise ProductAgentInvariantError(
                        "elder narrative requires the production Product processor"
                    )
                source = processor.load_source(scope, lease)
                committed_shared = (
                    processor.load_committed_shared_for_narrative(
                        scope,
                        lease,
                        source,
                    )
                )
                prepared_at = processor.now_factory()
                _require_aware(prepared_at, "elder_narrative_prepared_at")
                render_identity_sha256 = _required_string(
                    source.operation_json,
                    "render_identity_sha256",
                )
                invocation_generation = _optional_positive_int(
                    source.operation_json,
                    "invocation_generation",
                    default=1,
                )
                request = {
                    "schema_version": "product_elder_narrative_request.v1",
                    "render_identity_sha256": render_identity_sha256,
                    "shared_analysis_sha256": _required_string(
                        source.operation_json,
                        "shared_analysis_sha256",
                    ),
                    "elder_projection_sha256": _required_string(
                        source.operation_json,
                        "elder_projection_sha256",
                    ),
                    "narrative_manifest_sha256": _required_string(
                        source.operation_json,
                        "narrative_manifest_sha256",
                    ),
                    "invocation_generation": invocation_generation,
                    "model_mode": self._model_mode.value,
                }

                def invoke_elder_narrative() -> tuple[
                    Mapping[str, Any],
                    str | None,
                ]:
                    usage_observer = _ProductProviderUsageObserver(
                        context,
                        source_fence=lambda: (
                            processor.revalidate_provider_source_fence(
                                scope,
                                lease,
                                source=source,
                                operation_type=operation_type,
                                committed_shared=committed_shared,
                            )
                        ),
                    )
                    try:
                        with provider_transport_audit_scope(usage_observer):
                            artifact = processor.prepare_elder_narrative(
                                scope=scope,
                                source=source,
                                lease=lease,
                                committed_shared=committed_shared,
                                prepared_at=prepared_at,
                            )
                    except Exception as exc:
                        failed_usage = usage_observer.snapshot()
                        processor.persist_failed_provider_attempt(
                            scope,
                            lease,
                            source=source,
                            operation_type=operation_type,
                            provider_usage=failed_usage,
                        )
                        if failed_usage.call_count == 0:
                            raise DispatchKnownNotSent(
                                "product_provider_known_not_sent"
                            ) from exc
                        raise
                    artifact = artifact.model_copy(
                        update={"provider_usage": usage_observer.snapshot()}
                    )
                    governed_request_records = (
                        _governed_provider_request_records_for_narrative(
                            artifact
                        )
                    )
                    return (
                        {
                            "schema_version": (
                                "product_elder_narrative_response.v1"
                            ),
                            "artifact": artifact.model_dump(mode="json"),
                            "governed_provider_request_records": (
                                governed_request_records
                            ),
                        },
                        (
                            "deterministic:" + stable_hash(request)[:24]
                            if self._model_mode is ModelMode.DETERMINISTIC
                            else None
                        ),
                    )

                response = context.invocation_dispatcher().dispatch(
                    invocation_key=(
                        "product-elder-narrative:"
                        f"{render_identity_sha256}:"
                        f"g{invocation_generation}:"
                        f"{self._model_mode.value}.v1"
                    ),
                    request=request,
                    sender=invoke_elder_narrative,
                    invocation_kind=InvocationKind.MODEL,
                )
                artifact_value = response.get("artifact")
                if not isinstance(artifact_value, Mapping):
                    raise ProductAgentInvariantError(
                        "journaled elder narrative response has no artifact"
                    )
                narrative_artifact = (
                    PreparedElderNarrativeArtifact.model_validate(
                        artifact_value
                    )
                )
                narrative_result = (
                    processor.persist_and_commit_elder_narrative(
                        scope,
                        lease,
                        narrative_artifact,
                        source=source,
                        committed_shared=committed_shared,
                    )
                )
                return WorkResult(
                    disposition=WorkDisposition.SUCCEEDED,
                    result={
                        "state": narrative_result.state,
                        "narrative": narrative_result.narrative.model_dump(
                            mode="json"
                        ),
                        "provider_usage": (
                            narrative_result.provider_usage.model_dump(
                                mode="json"
                            )
                        ),
                    },
                    finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
                )
            if isinstance(processor, ProductAgentProcessor):
                source = processor.load_source(scope, lease)
                prepared_at = processor.now_factory()
                _require_aware(prepared_at, "prepared_at")
                request = {
                    "schema_version": (
                        "product_shared_analysis_request.v1"
                        if operation_type == PRODUCT_SHARED_ANALYSIS_OPERATION
                        else "product_agent_model_request.v1"
                    ),
                    "operation_id": source.operation_id,
                    "night_episode_id": source.night_episode_id,
                    "night_episode_revision_id": source.night_episode_revision_id,
                    "source_state_version": source.night_episode_revision_number,
                    "observation_set_sha256": source.observation_set_sha256,
                    "policy_sha256": source.policy_sha256,
                    "model_mode": self._model_mode.value,
                }
                invocation_key = (
                    (
                        "product-shared:"
                        + _required_string(
                            source.operation_json,
                            "desired_analysis_sha256",
                        )
                        + ":g"
                        + str(
                            _optional_positive_int(
                                source.operation_json,
                                "invocation_generation",
                                default=1,
                            )
                        )
                        if operation_type == PRODUCT_SHARED_ANALYSIS_OPERATION
                        else (
                            f"product-agent:{source.operation_id}:"
                            f"{source.night_episode_revision_id}"
                        )
                    )
                    + f":{self._model_mode.value}.v1"
                )

                def invoke_model() -> tuple[Mapping[str, Any], str | None]:
                    usage_observer = _ProductProviderUsageObserver(
                        context,
                        source_fence=lambda: (
                            processor.revalidate_provider_source_fence(
                                scope,
                                lease,
                                source=source,
                                operation_type=operation_type,
                            )
                        ),
                    )
                    try:
                        with provider_transport_audit_scope(usage_observer):
                            artifact = (
                                processor.prepare_shared(
                                    scope=scope,
                                    source=source,
                                    lease=lease,
                                    prepared_at=prepared_at,
                                )
                                if operation_type
                                == PRODUCT_SHARED_ANALYSIS_OPERATION
                                else processor.prepare(
                                    scope=scope,
                                    source=source,
                                    lease=lease,
                                    prepared_at=prepared_at,
                                )
                            )
                    except Exception as exc:
                        failed_usage = usage_observer.snapshot()
                        processor.persist_failed_provider_attempt(
                            scope,
                            lease,
                            source=source,
                            operation_type=operation_type,
                            provider_usage=failed_usage,
                        )
                        if failed_usage.call_count == 0:
                            raise DispatchKnownNotSent(
                                "product_provider_known_not_sent"
                            ) from exc
                        raise
                    if (
                        artifact.schema_version
                        == "product_agent_prepared_attempt.v3"
                    ):
                        artifact = artifact.model_copy(
                            update={"provider_usage": usage_observer.snapshot()}
                        )
                    governed_request_records = (
                        _governed_provider_request_records_for_shared(
                            artifact
                        )
                    )
                    return (
                        {
                            "schema_version": "product_agent_model_response.v1",
                            "artifact": artifact.model_dump(mode="json"),
                            "governed_provider_request_records": (
                                governed_request_records
                            ),
                        },
                        (
                            "deterministic:" + stable_hash(request)[:24]
                            if self._model_mode is ModelMode.DETERMINISTIC
                            else None
                        ),
                    )

                response = context.invocation_dispatcher().dispatch(
                    invocation_key=invocation_key,
                    request=request,
                    sender=invoke_model,
                    invocation_kind=InvocationKind.MODEL,
                )
                artifact_value = response.get("artifact")
                if not isinstance(artifact_value, Mapping):
                    raise ProductAgentInvariantError(
                        "journaled Product model response has no artifact"
                    )
                artifact = PreparedProductAgentArtifact.model_validate(
                    artifact_value
                )
                result = processor.persist_and_commit(
                    scope,
                    lease,
                    artifact,
                    source=source,
                )
            else:
                # Explicit test processors retain the narrow adapter seam; the
                # production composition always builds ProductAgentProcessor.
                result = processor.process(scope, lease)  # type: ignore[unreachable]
        except ProductAgentStaleSource:
            fail_compatibility("product_agent_stale_source")
            return _terminal("product_agent_stale_source")
        except ProductAgentLeaseLost:
            try:
                fail_compatibility("product_agent_lease_or_source_lost")
            except ProductAgentLeaseLost:
                # A genuinely expired lease cannot safely mutate the wrapper;
                # reconciliation owns that ambiguous outcome.
                pass
            context.mark_lease_lost()
            return WorkResult(
                disposition=WorkDisposition.OUTCOME_UNKNOWN,
                error_code="product_agent_lease_lost_reconciliation_required",
            )
        except ProductAgentInvariantError:
            fail_compatibility("product_agent_invariant_violation")
            return _terminal("product_agent_invariant_violation")
        except ProductAgentConflict:
            if context.claim.attempt >= context.claim.max_attempts:
                fail_compatibility("product_agent_conflict_exhausted")
                return _terminal("product_agent_conflict_exhausted")
            return WorkResult(
                disposition=WorkDisposition.RETRYABLE,
                error_code="product_agent_conflict",
            )
        except RetryableWorkError:
            if context.claim.attempt >= context.claim.max_attempts:
                fail_compatibility("product_agent_retry_exhausted")
            raise
        except TerminalWorkError as exc:
            fail_compatibility(exc.code)
            raise
        except OutcomeUnknownError as exc:
            fail_compatibility(exc.code, outcome_unknown=True)
            raise
        except Exception:
            # The durable runtime classifies every escaped untyped failure as
            # OUTCOME_UNKNOWN (never RETRYABLE), so linked automatic wrappers
            # must enter reconciliation immediately instead of hanging.
            fail_compatibility(
                "unclassified_handler_failure",
                outcome_unknown=True,
            )
            raise
        return WorkResult(
            disposition=WorkDisposition.SUCCEEDED,
            result={
                "operation_id": result.operation_id,
                "product_attempt_id": result.product_attempt_id,
                "analysis_revision_id": result.analysis_revision_id,
                "role_view_ids": list(result.role_view_ids),
                "analysis_status": result.analysis_status,
                "induction_operation_id": result.induction_operation_id,
                "induction_manifest_id": result.induction_manifest_id,
            },
            finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
        )

    def _resolve_processor(self, context: WorkContext) -> ProductAgentProcessor:
        if self._processor is not None:
            return self._processor
        assert self._processor_factory is not None
        return self._processor_factory(worker_uow_factory(context))


def build_product_agent_worker_handlers(
    settings: SleepBackendSettings,
) -> dict[str, WorkHandler]:
    """Compose one replay Product handler with explicit model selection."""

    if "product_agent" not in settings.worker_queues:
        return {}
    if settings.process_role != ProcessRole.WORKER:
        raise ProductAgentCompositionError(
            "Product Agent handlers require a worker profile"
        )
    if (
        settings.data_mode == BackendDataMode.LIVE
        and settings.model_mode is not ModelMode.LIVE
    ):
        raise ProductAgentCompositionError(
            "live Product Agent data requires the live model runtime"
        )
    if settings.model_mode is ModelMode.DETERMINISTIC:
        from sleepagent.runtime.deterministic_model import (
            DeterministicReplayStructuredAgentModel,
        )
        from sleepagent.runtime.factory import (
            build_deterministic_product_runtime_bundle,
        )

        runtime_bundle = build_deterministic_product_runtime_bundle(
            model=DeterministicReplayStructuredAgentModel(
                deployment_mode=settings.deployment_mode.value,
                data_mode=settings.data_mode.value,
            )
        )
    elif settings.model_mode is ModelMode.LIVE:
        from sleepagent.runtime.provider import (
            PRODUCT_LLM_API_KEY_ENV,
            ProductLLMConfigurationError,
        )
        from sleepagent.runtime.factory import (
            _build_postgres_worker_product_runtime_bundle_from_env,
            product_episode_runner_is_configured,
        )

        try:
            runtime_bundle = (
                _build_postgres_worker_product_runtime_bundle_from_env()
            )
        except ProductLLMConfigurationError as exc:
            raise ProductAgentCompositionError(str(exc)) from exc
        if not product_episode_runner_is_configured(runtime_bundle.runner):
            raise ProductAgentCompositionError(
                "live Product Agent requires " + PRODUCT_LLM_API_KEY_ENV
            )
    else:
        raise ProductAgentCompositionError(
            "Product Agent model mode must be deterministic or live"
        )

    def processor_factory(
        uow_factory: UnitOfWorkFactory[Any],
    ) -> ProductAgentProcessor:
        return ProductAgentProcessor(
            uow_factory,
            runtime_bundle=runtime_bundle,
            report_pipeline_mode=settings.report_pipeline_mode,
        )

    return {
        "product_agent": ProductAgentWorkHandlerAdapter(
            processor_factory=processor_factory,
            model_mode=settings.model_mode,
        )
    }


def _require_product_claim(claim: LeaseClaim) -> None:
    if (
        claim.queue != "product_agent"
        or claim.operation_id is None
        or claim.operation_id != claim.work_id
        or claim.metadata.get("work_kind") != "operation"
        or claim.metadata.get("operation_type") not in PRODUCT_WORK_OPERATION_TYPES
        or claim.metadata.get("queue_name") != "product_agent"
    ):
        raise ProductAgentInvariantError("claim is not Product Agent work")


def _fact_snapshot_for_role(
    *,
    scope: UowScope,
    source: LoadedProductAgentSource,
    role: AnalysisRole,
    fact_snapshot_id: str,
    created_at: datetime,
    personalization: PinnedPersonalizationContext,
) -> FactSnapshot:
    local_date = date.fromisoformat(source.facts.local_sleep_date)
    longitudinal = source.facts.longitudinal_risk_context
    source_scope = SourceScope(
        kind=(
            SourceScopeKind.HISTORICAL_RANGE
            if longitudinal is not None
            else SourceScopeKind.CURRENT_NIGHT
        ),
        as_of=created_at,
        timezone_name=source.facts.timezone_name,
        date_start=(
            longitudinal.date_start
            if longitudinal is not None
            else local_date
        ),
        date_end=local_date,
        valid_night_count=(
            longitudinal.valid_night_count
            if longitudinal is not None
            else 1
        ),
    )
    internal_subject = stable_hash(
        {
            "data_mode": source.facts.data_mode.value,
            "subject_id": source.subject_id,
        }
    )
    return FactSnapshot.create(
        fact_snapshot_id=fact_snapshot_id,
        binding=AuthenticatedBinding(
            actor_id=f"workload:{scope.service_principal_id}",
            subject_id=f"subject:{internal_subject[:32]}",
            role=role.value,
            authorization_scope=(
                "read_sleep_data",
                "read_device_data",
                "draft_material",
            ),
        ),
        source_scope=source_scope,
        canonical_data_version=source.facts.canonical_data_version,
        active_constraint_codes=tuple(
            sorted(
                {
                    *source.facts.deterministic_quality.get("reason_codes", ()),
                    *source.facts.deterministic_risk.get("reason_codes", ()),
                }
            )
        ),
        habit_profile_version=personalization.habit_profile_version,
        habit_profile_hash=personalization.habit_profile_hash,
        memory_context_version=personalization.memory_state_version,
        memory_read_receipt_refs=tuple(
            item.receipt_id for item in personalization.memory_read_receipts
        ),
        memory_read_receipt_hashes=tuple(
            str(item.receipt_hash)
            for item in personalization.memory_read_receipts
        ),
        source_refs=(
            *source.facts.agent_source_refs(),
            *(
                (
                    f"habit-profile:{personalization.habit_profile_version}:"
                    f"{personalization.habit_profile_hash}",
                )
                if personalization.habit_profile_hash is not None
                else ()
            ),
            *(item.fact_id for item in personalization.habit_facts),
            *(item.receipt_id for item in personalization.memory_read_receipts),
            *(
                handle.handle_id
                for receipt in personalization.memory_read_receipts
                for handle in receipt.handles
            ),
        ),
        created_at=created_at,
    )


def _fact_snapshot_for_shared(
    *,
    scope: UowScope,
    source: LoadedProductAgentSource,
    fact_snapshot_id: str,
    created_at: datetime,
    consumed_context_sha256: str,
    personalization: PinnedPersonalizationContext,
) -> FactSnapshot:
    """Build one system-bound snapshot shared by every role projection."""

    local_date = date.fromisoformat(source.facts.local_sleep_date)
    longitudinal = source.facts.longitudinal_risk_context
    stable_as_of = (
        source.episode.wake_at
        or source.episode.deterministic_close_deadline_at
    )
    source_scope = SourceScope(
        kind=(
            SourceScopeKind.HISTORICAL_RANGE
            if longitudinal is not None
            else SourceScopeKind.CURRENT_NIGHT
        ),
        as_of=stable_as_of,
        timezone_name=source.facts.timezone_name,
        date_start=(
            longitudinal.date_start if longitudinal is not None else local_date
        ),
        date_end=local_date,
        valid_night_count=(
            longitudinal.valid_night_count if longitudinal is not None else 1
        ),
    )
    internal_subject = stable_hash(
        {
            "data_mode": source.facts.data_mode.value,
            "subject_id": source.subject_id,
        }
    )
    selected_habit_hash = (
        None
        if not personalization.habit_facts
        else stable_hash(
            {
                "schema_version": "selected_habit_facts.v1",
                "facts": sorted(
                    (
                        {
                            "fact_id": item.fact_id,
                            "fact_hash": item.fact_hash,
                        }
                        for item in personalization.habit_facts
                    ),
                    key=lambda item: (item["fact_id"], item["fact_hash"]),
                ),
            }
        )
    )
    return FactSnapshot.create(
        fact_snapshot_id=fact_snapshot_id,
        binding=AuthenticatedBinding(
            actor_id=f"workload:{scope.service_principal_id}",
            subject_id=f"subject:{internal_subject[:32]}",
            role="system",
            authorization_scope=(
                "read_sleep_data",
                "read_device_data",
                "draft_material",
            ),
        ),
        source_scope=source_scope,
        canonical_data_version=source.facts.canonical_data_version,
        active_constraint_codes=tuple(
            sorted(
                {
                    *source.facts.deterministic_quality.get("reason_codes", ()),
                    *source.facts.deterministic_risk.get("reason_codes", ()),
                }
            )
        ),
        habit_profile_version=(1 if selected_habit_hash is not None else 0),
        habit_profile_hash=selected_habit_hash,
        memory_context_version=0,
        memory_read_receipt_refs=(),
        memory_read_receipt_hashes=(),
        source_refs=(
            *source.facts.agent_source_refs(),
            *(item.fact_id for item in personalization.habit_facts),
            f"consumed-context:sha256:{consumed_context_sha256}",
        ),
        created_at=stable_as_of,
    )


def _personalization_for_product_episode(
    *,
    scope: UowScope,
    source: LoadedProductAgentSource,
    product_episode_id: str,
    as_of: datetime,
) -> PinnedPersonalizationContext:
    _require_worker_subject_scope(scope)
    privacy_epoch = cast(int, scope.privacy_epoch)
    authorization_epoch = cast(int, scope.authorization_epoch)
    current_habits = source.habit_profile.current(as_of)
    habit_hash = (
        None
        if source.habit_profile.version == 0
        else stable_hash(
            {
                "subject_id": source.subject_id,
                "profile_version": source.habit_profile.version,
                "fact_hashes": [item.fact_hash for item in current_habits],
            }
        )
    )
    receipts: list[MemoryReadReceipt] = []
    for requesting_agent, purpose, concepts in (
        (
            AgentId.EVIDENCE_REASONING,
            MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT,
            EVIDENCE_MEMORY_CONCEPT_IDS,
        ),
        (
            AgentId.CARE_STRATEGY,
            MemoryPurpose.CARE_PREFERENCE_CONTEXT,
            CARE_MEMORY_CONCEPT_IDS,
        ),
    ):
        query = resolve_memory_query(
            MemoryQueryIntent(
                purpose=purpose,
                concept_ids=concepts,
                source_scope_kind=SourceScopeKind.HISTORICAL_RANGE,
                max_items=4,
                token_budget=800,
            ),
            invocation_id=(
                f"personalization:{product_episode_id}:"
                f"{requesting_agent.value}"
            ),
            actor_id=f"workload:{scope.service_principal_id}",
            actor_role="system",
            subject_id=source.subject_id,
            requesting_agent=requesting_agent,
            authorization_scope=("memory:read",),
            as_of=as_of,
            privacy_epoch=privacy_epoch,
            authorization_epoch=authorization_epoch,
        )
        receipts.append(
            select_memory_slice(query, source.memory_state, now=as_of)
        )
    return PinnedPersonalizationContext(
        subject_id=source.subject_id,
        habit_profile_version=source.habit_profile.version,
        habit_profile_hash=habit_hash,
        habit_facts=current_habits,
        memory_state_version=source.memory_state.version,
        memory_read_receipts=tuple(receipts),
    )


def _workload_snapshot(scope: UowScope, handler: str) -> dict[str, Any]:
    return {
        "schema_version": "workload_authorization_snapshot.v1",
        "workload_principal_id": scope.service_principal_id,
        "namespace_id": scope.namespace_id,
        "namespace_generation": scope.namespace_generation,
        "data_mode": scope.data_mode,
        "run_id": scope.run_id,
        "arm_id": scope.arm_id,
        "subject_id": scope.subject_id,
        "purpose": scope.purpose,
        "allowed_handler": handler,
        "authorization_epoch": scope.authorization_epoch,
        "privacy_epoch": scope.privacy_epoch,
        "retrieval_policy_epoch": scope.retrieval_policy_epoch,
    }


def _evidence_claim_material(
    product: AcceptedWorkProduct,
) -> tuple[tuple[dict[str, Any], ...], dict[str, str]]:
    packet = EvidencePacket.model_validate(product.payload)
    material_by_id = {
        claim.claim_id: {
            "semantic": claim.semantic.value,
            "metric_id": claim.metric_id,
            "source_kind": claim.source_kind.value,
            "evidence_refs": sorted(claim.evidence_refs),
            "claim_strength": claim.claim_strength,
        }
        for claim in packet.claims
    }
    signatures = {
        claim_id: stable_hash(material)
        for claim_id, material in material_by_id.items()
    }
    return tuple(sorted(material_by_id.values(), key=stable_hash)), signatures


def _care_semantic_material(product: AcceptedWorkProduct | None) -> Any:
    if product is None:
        return None
    strategy = CareStrategy.model_validate(product.payload)
    action = strategy.primary_action
    return {
        "disposition": strategy.disposition,
        "transition_confirmation_required": (
            strategy.transition_confirmation_required
        ),
        "primary_action": (
            None
            if action is None
            else action.model_dump(
                mode="json",
                exclude={"candidate_id", "candidate_hash"},
            )
        ),
        "coordination_candidates": sorted(
            (
                item.model_dump(
                    mode="json",
                    exclude={"candidate_id", "dedupe_key"},
                )
                for item in strategy.coordination_candidates
            ),
            key=stable_hash,
        ),
    }


def _accepted_product_for_agent(
    products: list[AcceptedWorkProduct],
    agent_id: AgentId,
) -> AcceptedWorkProduct | None:
    matches = tuple(item for item in products if item.agent_id is agent_id)
    if len(matches) > 1:
        raise ProductAgentInvariantError(
            f"shadow legacy result has duplicate {agent_id.value} products"
        )
    return None if not matches else matches[0]


def _shared_metric_material(shared: SharedNightAnalysis) -> tuple[dict[str, Any], ...]:
    return tuple(
        sorted(
            (
                {
                    "metric_id": fact.metric_id,
                    "value": fact.value,
                    "unit": fact.unit,
                    "window": fact.window,
                }
                for fact in shared.semantic_facts
                if fact.fact_kind == "direct_metric"
            ),
            key=stable_hash,
        )
    )


def _source_metric_material(source: LoadedProductAgentSource) -> tuple[dict[str, Any], ...]:
    summary = source.facts.deterministic_night_summary()
    metrics: list[dict[str, Any]] = []
    for metric_id, unit in (
        ("sleep_window_minutes", "minutes"),
        ("bed_exit_count", "count"),
    ):
        value = summary.get(metric_id)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics.append(
                {
                    "metric_id": metric_id,
                    "value": value,
                    "unit": unit,
                    "window": "authoritative_sleep_window",
                }
            )
    stages = summary.get("stage_minutes")
    if isinstance(stages, Mapping):
        for stage, value in stages.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics.append(
                    {
                        "metric_id": f"sleep_stage.{stage}_minutes",
                        "value": value,
                        "unit": "minutes",
                        "window": "authoritative_sleep_window",
                    }
                )
    vital_centers = summary.get("vital_centers")
    if isinstance(vital_centers, Mapping):
        for metric_id, unit in (
            ("heart_rate", "bpm"),
            ("respiratory_rate", "breaths_per_minute"),
        ):
            value = vital_centers.get(metric_id)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics.append(
                    {
                        "metric_id": f"{metric_id}_mean",
                        "value": value,
                        "unit": unit,
                        "window": "authoritative_sleep_window",
                    }
                )
    return tuple(sorted(metrics, key=stable_hash))


def build_report_shadow_comparison(
    *,
    legacy: PreparedProductAgentArtifact,
    shared: PreparedProductAgentArtifact,
    source: LoadedProductAgentSource,
) -> ReportShadowComparison:
    """Compare structured semantics; never compare localized wording."""

    if (
        legacy.schema_version != "product_agent_prepared_attempt.v2"
        or shared.schema_version != "product_agent_prepared_attempt.v3"
        or shared.shared_analysis is None
        or shared.shadow_comparison is not None
        or legacy.night_episode_revision_id != shared.night_episode_revision_id
        or legacy.night_episode_revision_id != source.night_episode_revision_id
    ):
        raise ProductAgentInvariantError("shadow comparison paths are not comparable")

    legacy_claims: list[dict[str, Any]] = []
    legacy_visible: dict[str, tuple[str, ...]] = {}
    legacy_source_refs: dict[str, tuple[str, ...]] = {}
    legacy_care: list[Any] = []
    for role_run in legacy.role_runs:
        assert role_run.result is not None
        evidence = _accepted_product_for_agent(
            role_run.result.accepted_work_products,
            AgentId.EVIDENCE_REASONING,
        )
        if evidence is None:
            claim_material: tuple[dict[str, Any], ...] = ()
            claim_signatures: dict[str, str] = {}
        else:
            claim_material, claim_signatures = _evidence_claim_material(evidence)
        legacy_claims.extend(claim_material)
        legacy_source_refs[role_run.role.value] = tuple(
            sorted(
                {
                    str(ref)
                    for claim in claim_material
                    for ref in claim["evidence_refs"]
                }
            )
        )
        publication_refs = (
            ()
            if role_run.result.publication is None
            else tuple(role_run.result.publication.claim_refs)
        )
        legacy_visible[role_run.role.value] = tuple(
            sorted(
                claim_signatures.get(ref, "unresolved:" + stable_hash(ref))
                for ref in publication_refs
            )
        )
        legacy_care.append(
            _care_semantic_material(
                _accepted_product_for_agent(
                    role_run.result.accepted_work_products,
                    AgentId.CARE_STRATEGY,
                )
            )
        )

    shared_claim_material, shared_claim_signatures = _evidence_claim_material(
        shared.shared_analysis.evidence
    )
    shared_visible = {
        role_run.role.value: tuple(
            sorted(
                shared_claim_signatures.get(
                    ref,
                    "unresolved:" + stable_hash(ref),
                )
                for ref in (
                    ()
                    if role_run.role_projection is None
                    else role_run.role_projection.claim_refs
                )
            )
        )
        for role_run in shared.role_runs
    }
    legacy_fact_material = tuple(
        sorted(
            {stable_hash(item): item for item in legacy_claims}.values(),
            key=stable_hash,
        )
    )
    shared_fact_material = tuple(sorted(shared_claim_material, key=stable_hash))
    risk_summary = source.facts.provider_risk_summary()
    shared_risk = {
        "risk_state": shared.shared_analysis.source.risk_state,
        "reason_codes": sorted(shared.shared_analysis.source.risk_reason_codes),
    }
    source_risk = {
        "risk_state": str(risk_summary.get("risk_state") or "unknown"),
        "reason_codes": sorted(
            str(item) for item in risk_summary.get("reason_codes", ())
        ),
    }
    shared_care = _care_semantic_material(shared.shared_analysis.care)
    shared_claim_source_refs = tuple(
        sorted(
            {
                str(ref)
                for claim in shared_claim_material
                for ref in claim["evidence_refs"]
            }
        )
    )
    shared_source_refs = {
        item.role.value: shared_claim_source_refs
        for item in shared.role_runs
    }
    quality_state = (
        "partial"
        if legacy.analysis.data_sufficiency is DataSufficiency.PARTIAL
        else "good"
    )
    return ReportShadowComparison.create(
        legacy_attempt_sha256=legacy.attempt_sha256,
        shared_analysis_sha256=shared.shared_analysis.shared_analysis_sha256,
        category_material={
            "fact_identities": (legacy_fact_material, shared_fact_material),
            "metric_values_units": (
                _source_metric_material(source),
                _shared_metric_material(shared.shared_analysis),
            ),
            "quality_status": (
                {
                    "data_sufficiency": legacy.analysis.data_sufficiency.value,
                    "quality_state": quality_state,
                },
                {
                    "data_sufficiency": (
                        shared.shared_analysis.source.data_sufficiency
                    ),
                    "quality_state": shared.shared_analysis.source.quality_state,
                },
            ),
            "risk_classification": (source_risk, shared_risk),
            "care_candidate_semantics": (
                tuple(
                    sorted(
                        {
                            stable_hash(item): item
                            for item in legacy_care
                            if item is not None
                        }.values(),
                        key=stable_hash,
                    )
                ),
                (() if shared_care is None else (shared_care,)),
            ),
            "role_visible_fact_sets": (legacy_visible, shared_visible),
            "source_references": (legacy_source_refs, shared_source_refs),
        },
    )


def _desired_analysis_sha256(
    *,
    scope: UowScope,
    source: LoadedProductAgentSource,
    consumed_context_sha256: str,
    runtime_manifest_sha256: str,
) -> str:
    reporting_time_authority = _desired_reporting_time_authority(source)
    return stable_hash(
        {
            "schema_version": "desired_shared_analysis.v1",
            "namespace_id": scope.namespace_id,
            "namespace_generation": scope.namespace_generation,
            "data_mode": scope.data_mode,
            "run_id": scope.run_id,
            "arm_id": scope.arm_id,
            "night_episode_id": source.night_episode_id,
            "night_episode_revision_id": source.night_episode_revision_id,
            "night_episode_revision_number": (
                source.night_episode_revision_number
            ),
            "canonical_data_version": source.facts.canonical_data_version,
            "observation_set_sha256": source.observation_set_sha256,
            "quality": source.facts.provider_quality_summary(),
            "risk": source.facts.provider_risk_summary(),
            **(
                {"reporting_time_authority": reporting_time_authority}
                if reporting_time_authority is not None
                else {}
            ),
            # The operation policy hash binds the authenticated caller/role.
            # Desired shared work instead pins only role-neutral source policy.
            "source_policy_versions": dict(sorted(source.policy_versions.items())),
            "consumed_context_sha256": consumed_context_sha256,
            "authorization_epoch": scope.authorization_epoch,
            "privacy_epoch": scope.privacy_epoch,
            "retrieval_policy_epoch": scope.retrieval_policy_epoch,
            "runtime_manifest_sha256": runtime_manifest_sha256,
        }
    )


def _desired_reporting_time_authority(
    source: LoadedProductAgentSource,
) -> dict[str, Any] | None:
    """Pin reporting time for real loaded sources; tolerate legacy test shims."""

    episode = getattr(source, "episode", None)
    facts = getattr(source, "facts", None)
    timezone_name = getattr(facts, "timezone_name", None)
    local_sleep_date = getattr(facts, "local_sleep_date", None)
    if episode is None or timezone_name is None or local_sleep_date is None:
        return None
    start = getattr(episode, "bed_at", None) or getattr(
        episode, "collection_start_at", None
    )
    end = getattr(episode, "wake_at", None) or getattr(
        episode, "deterministic_close_deadline_at", None
    )
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return {
        "timezone_name": timezone_name,
        "authoritative_start_at_utc": start.astimezone(timezone.utc),
        "authoritative_end_at_utc": end.astimezone(timezone.utc),
        "local_sleep_date": local_sleep_date,
    }


def _recompute_shared_identity(
    *,
    scope: UowScope,
    source: LoadedProductAgentSource,
    habit_profile: HabitProfileState,
    memory_state: GovernedMemoryState,
    as_of: datetime,
    runtime_manifest: Mapping[str, Any],
) -> tuple[str, str, str]:
    """Recompute the exact selected-context and analysis identity.

    The helper is deliberately side-effect free so the provider pre-send fence
    and the final commit fence apply byte-identical identity semantics.
    """

    current_source = replace(
        source,
        habit_profile=habit_profile,
        memory_state=memory_state,
    )
    personalization = _personalization_for_product_episode(
        scope=scope,
        source=current_source,
        product_episode_id=(
            "identity-fence:"
            f"{source.night_episode_revision_id}"
        ),
        as_of=as_of,
    )
    consumed_context_sha256 = _consumed_context_sha256(
        personalization,
        authorization_epoch=cast(int, scope.authorization_epoch),
        privacy_epoch=cast(int, scope.privacy_epoch),
        retrieval_policy_epoch=cast(int, scope.retrieval_policy_epoch),
    )
    runtime_manifest_sha256 = stable_hash(runtime_manifest)
    desired_analysis_sha256 = _desired_analysis_sha256(
        scope=scope,
        source=current_source,
        consumed_context_sha256=consumed_context_sha256,
        runtime_manifest_sha256=runtime_manifest_sha256,
    )
    return (
        consumed_context_sha256,
        runtime_manifest_sha256,
        desired_analysis_sha256,
    )


def _consumed_context_sha256(
    personalization: PinnedPersonalizationContext,
    *,
    authorization_epoch: int,
    privacy_epoch: int,
    retrieval_policy_epoch: int,
) -> str:
    """Hash only selected L2 meaning, never volatile receipt identities."""

    habit_facts = sorted(
        (
            {
                "fact_id": fact.fact_id,
                "fact_hash": fact.fact_hash,
            }
            for fact in personalization.habit_facts
        ),
        key=lambda item: (item["fact_id"], item["fact_hash"]),
    )
    memory_slices = sorted(
        (
            {
                "requesting_agent": receipt.requesting_agent.value,
                "purpose": receipt.purpose.value,
                "query_intent": {
                    "source_scope_kind": (
                        SourceScopeKind.HISTORICAL_RANGE.value
                    ),
                    "max_items": 4,
                    "token_budget": 800,
                    "concept_ids": (
                        list(EVIDENCE_MEMORY_CONCEPT_IDS)
                        if receipt.requesting_agent
                        == AgentId.EVIDENCE_REASONING
                        else list(CARE_MEMORY_CONCEPT_IDS)
                    ),
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
            for receipt in personalization.memory_read_receipts
        ),
        key=lambda item: (item["requesting_agent"], item["purpose"]),
    )
    return stable_hash(
        {
            "schema_version": "consumed_product_context.v1",
            "habit_facts": habit_facts,
            "memory_slices": memory_slices,
            "authorization_epoch": authorization_epoch,
            "privacy_epoch": privacy_epoch,
            "retrieval_policy_epoch": retrieval_policy_epoch,
        }
    )


def _shared_runtime_manifest(
    runtime_bundle: ProductRuntimeBundle,
    *,
    source: LoadedProductAgentSource,
) -> dict[str, Any]:
    """Pin only the role-neutral analysis semantics used before projection."""

    agent_ids = (
        AgentId.EVIDENCE_REASONING,
        AgentId.CARE_STRATEGY,
        AgentId.SAFETY_REVIEW,
    )
    subject_ref = _product_runtime_subject_ref(source)
    roster = runtime_bundle.roster.as_mapping()
    registry_manifest = product_agent_manifest()
    tool_allowlist = cast(
        Mapping[str, list[str]],
        registry_manifest["tool_invocation_allowlist"],
    )
    tool_definitions = cast(
        Mapping[str, Mapping[str, Any]],
        registry_manifest["tool_definitions"],
    )
    analysis_tool_names = sorted(
        {
            tool_name
            for agent_id in agent_ids
            for tool_name in tool_allowlist[agent_id.value]
        }
    )
    agents: dict[str, dict[str, Any]] = {}
    for agent_id in agent_ids:
        agent = roster[agent_id]
        skill_id = agent.select_skill(
            EpisodeType.MORNING_REVIEW,
            doctor_material=False,
        )
        package = runtime_bundle.skill_registry.released(
            skill_id,
            agent_id,
            subject_id=subject_ref,
        )
        profile = runtime_bundle.agent_profiles[agent_id]
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
            "model": _runtime_model_pin(agent.model),
        }
    sleepcare = runtime_bundle.roster.sleepcare
    sleepcare_profile = runtime_bundle.agent_profiles[AgentId.SLEEP_CARE]
    sleepcare_control_skills = []
    for skill_id in ("plan_episode", "evaluate_work_product"):
        package = runtime_bundle.skill_registry.released(
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
    catalog = runtime_bundle.policies.care_catalog
    catalog_material = {
        "definitions": [
            item.model_dump(mode="json")
            for item in catalog.list_definitions()
        ],
        "delivery_policy": catalog.delivery_policy.model_dump(mode="json"),
    }
    return build_shared_analysis_runtime_manifest(
        runner_version=PRODUCT_EPISODE_RUNNER_VERSION,
        prompt_compiler_version=(
            runtime_bundle.prompt_compiler.compiler_version
        ),
        safety_policy_version=PRODUCT_SAFETY_POLICY_VERSION,
        sleepcare_control={
            "profile": {
                "profile_id": sleepcare_profile.profile_id,
                "version": sleepcare_profile.version,
                "profile_hash": sleepcare_profile.profile_hash,
                "output_schema_id": sleepcare_profile.output_schema_id,
            },
            "skills": sleepcare_control_skills,
            "model": _runtime_model_pin(sleepcare.planning_model),
        },
        agents=agents,
        tools={
            name: dict(tool_definitions[name]) for name in analysis_tool_names
        },
        care_catalog=catalog_material,
    )


def _elder_narrative_runtime_manifest(
    runtime_bundle: ProductRuntimeBundle,
    *,
    source: LoadedProductAgentSource,
) -> dict[str, Any]:
    """Pin only the optional SleepCare elder rendering semantics."""

    sleepcare = runtime_bundle.roster.sleepcare
    skill_id = sleepcare.select_skill(
        EpisodeType.MORNING_REVIEW,
        doctor_material=False,
    )
    package = runtime_bundle.skill_registry.released(
        skill_id,
        AgentId.SLEEP_CARE,
        subject_id=_product_runtime_subject_ref(source),
    )
    profile = runtime_bundle.agent_profiles[AgentId.SLEEP_CARE]
    return build_elder_narrative_runtime_manifest(
        runner_version=PRODUCT_EPISODE_RUNNER_VERSION,
        prompt_compiler_version=(
            runtime_bundle.prompt_compiler.compiler_version
        ),
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
        model=_runtime_model_pin(sleepcare.model),
        content_plan_assembly=bool(
            getattr(sleepcare, "_content_plan_assembly", False)
        ),
    )


def _runtime_model_pin(model: Any) -> dict[str, Any]:
    config = getattr(model, "config", None)
    return build_safe_model_pin(
        implementation=(
            f"{type(model).__module__}.{type(model).__qualname__}"
        ),
        provider=str(getattr(model, "provider", "unknown")),
        model_id=str(getattr(model, "model_id", "unknown")),
        temperature=(
            getattr(config, "temperature", None) if config is not None else None
        ),
        max_output_tokens=(
            getattr(config, "max_output_tokens", None)
            if config is not None
            else None
        ),
        thinking_type=(
            getattr(config, "thinking_type", None) if config is not None else None
        ),
        retry=(getattr(config, "retry", None) if config is not None else None),
        timeout_seconds=(
            getattr(config, "timeout_seconds", None)
            if config is not None
            else None
        ),
        provider_endpoint=(
            getattr(config, "base_url", None) if config is not None else None
        ),
        deployment_mode=getattr(model, "deployment_mode", None),
        data_mode=getattr(model, "data_mode", None),
    )


def _product_runtime_subject_ref(source: LoadedProductAgentSource) -> str:
    internal_subject = stable_hash(
        {
            "data_mode": source.facts.data_mode.value,
            "subject_id": source.subject_id,
        }
    )
    return f"subject:{internal_subject[:32]}"


def _elder_narrative_identity_sha256(
    *,
    shared_analysis_sha256: str,
    elder_projection_sha256: str,
    narrative_manifest_sha256: str,
) -> str:
    return stable_hash(
        {
            "schema_version": "elder_narrative_request.v1",
            "shared_analysis_sha256": shared_analysis_sha256,
            "elder_projection_sha256": elder_projection_sha256,
            "render_manifest_sha256": narrative_manifest_sha256,
        }
    )


def _role_view_from_result(
    *,
    analysis: AnalysisRevision,
    role: AnalysisRole,
    product_episode_id: str,
    result: ProductEpisodeRunResult,
    source_refs: tuple[str, ...],
    role_view_id: str,
    generated_at: datetime,
) -> AnalysisRoleView:
    publication = result.publication
    failure_codes = tuple(dict.fromkeys(result.receipt.failure_codes))
    if (
        result.receipt.status == EpisodeStatus.COMPLETE
        and result.receipt.execution_mode == ExecutionMode.INTELLIGENT
        and publication is not None
        and publication.audience_role == role.value
    ):
        status = RoleViewStatus.READY
    elif result.receipt.status == EpisodeStatus.BLOCKED or publication is None:
        status = RoleViewStatus.BLOCKED
        failure_codes = failure_codes or ("ROLE_VIEW_BLOCKED",)
    else:
        status = RoleViewStatus.DEGRADED
        failure_codes = failure_codes or ("ROLE_VIEW_DEGRADED",)
    return AnalysisRoleView(
        role_view_id=role_view_id,
        analysis_revision_id=analysis.analysis_revision_id,
        night_episode_id=analysis.night_episode_id,
        night_episode_revision_id=analysis.night_episode_revision_id,
        data_mode=analysis.data_mode,
        subject_id=analysis.subject_id,
        role=role,
        status=status,
        product_agent_episode_id=product_episode_id,
        execution_mode=result.receipt.execution_mode.value,
        content=None if publication is None else publication.text,
        context_notice=(
            None if publication is None else publication.context_notice
        ),
        claim_refs=(
            () if publication is None else tuple(publication.claim_refs)
        ),
        source_refs=source_refs,
        failure_codes=failure_codes,
        generated_at=generated_at,
    )


def _role_view_from_projection(
    *,
    analysis: AnalysisRevision,
    projection: RoleProjection,
    product_episode_id: str,
    role_view_id: str,
    generated_at: datetime,
) -> AnalysisRoleView:
    role = AnalysisRole(projection.role.value)
    blocked = projection.state is RoleProjectionState.POLICY_BLOCKED
    return AnalysisRoleView(
        role_view_id=role_view_id,
        analysis_revision_id=analysis.analysis_revision_id,
        night_episode_id=analysis.night_episode_id,
        night_episode_revision_id=analysis.night_episode_revision_id,
        data_mode=analysis.data_mode,
        subject_id=analysis.subject_id,
        role=role,
        status=(RoleViewStatus.BLOCKED if blocked else RoleViewStatus.READY),
        product_agent_episode_id=product_episode_id,
        execution_mode="deterministic_only",
        content=projection.text,
        context_notice=projection.context_notice,
        claim_refs=projection.claim_refs,
        source_refs=(
            "shared_analysis:sha256:"
            f"{projection.source_shared_analysis_sha256}",
            "role_projection:sha256:"
            f"{projection.projection_sha256}",
        ),
        failure_codes=projection.failure_codes,
        generated_at=generated_at,
    )


def _public_today_projection(
    *,
    analysis: AnalysisRevision,
    view: AnalysisRoleView,
    projection_version: int,
    episode_local_date: date,
    assignment_basis: str,
    committed_at: datetime,
) -> dict[str, Any]:
    """Map one internal role view through the typed public-only allowlist."""

    if view.status == RoleViewStatus.PENDING:
        raise ProductAgentInvariantError(
            "pending role views cannot be published as /today projections"
        )
    if view.status == RoleViewStatus.READY:
        fallback_summary = "Sleep summary is ready."
        fallback_notice = "This summary reflects the latest completed night."
    elif view.status == RoleViewStatus.DEGRADED:
        fallback_summary = "A limited sleep summary is available."
        fallback_notice = "Some sleep evidence was unavailable."
    else:
        fallback_summary = "The sleep summary is currently unavailable."
        fallback_notice = "No recommendation is available from this analysis."
    content = _PublicTodayContent(
        audience=view.role,
        summary_text=view.content or fallback_summary,
        context_notice=view.context_notice or fallback_notice,
        # Public evidence references are deliberately empty in the first slice.
        # Internal claim/source references never cross this mapper.
        evidence_refs=() if view.role == AnalysisRole.DOCTOR else None,
    )
    projection = _PublicTodayProjection(
        data_mode=analysis.data_mode,
        synthetic_non_release=analysis.data_mode == DataMode.REPLAY,
        state=view.status,
        subject_ref=public_product_subject_ref(analysis.subject_id),
        role=view.role,
        episode_id=analysis.night_episode_id,
        episode_revision_id=analysis.night_episode_revision_id,
        episode_local_date=episode_local_date.isoformat(),
        assignment_basis=assignment_basis,
        analysis_revision_id=analysis.analysis_revision_id,
        projection_id=view.role_view_id,
        projection_version=projection_version,
        committed_at=committed_at.isoformat(),
        content=content,
    )
    payload = projection.model_dump(mode="json", exclude_none=True)
    if set(payload) != PUBLIC_PRODUCT_TODAY_FIELD_ALLOWLIST:
        raise ProductAgentInvariantError("public Product field allowlist drifted")
    return payload


def _build_facts(
    *,
    episode: NightEpisodeV2,
    revision_id: str,
    revision_number: int,
    observations: tuple[SleepObservation, ...],
    quality: DeterministicQualityAssessment,
    risk: CurrentRisk,
    longitudinal_risk_context: ProductLongitudinalRiskContext | None,
    acquisition_channels_by_observation: Mapping[
        str, tuple[str, ...]
    ] | None = None,
    semantics_by_observation: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[ProductRevisionFacts, str, dict[str, str], tuple[str, ...]]:
    if episode.episode_local_date is None:
        raise ProductAgentInvariantError(
            "Product source Episode has no committed canonical date"
        )
    safe_observations: list[dict[str, Any]] = []
    source_refs: list[str] = [
        f"night_episode_revision:{episode.data_mode.value}:{revision_id}"
    ]
    adapters: dict[str, str] = {}
    observation_schemas: set[str] = set()
    for observation in observations:
        projected, refs = _project_observation(
            observation,
            (semantics_by_observation or {}).get(observation.observation_id),
        )
        channels = tuple(
            dict.fromkeys(
                (acquisition_channels_by_observation or {}).get(
                    observation.observation_id,
                    (),
                )
            )
        )
        if channels:
            projected = {
                **projected,
                "acquisition_channels": list(channels),
            }
        safe_observations.append(projected)
        source_refs.extend(refs)
        adapters[observation.provenance.adapter_id] = (
            observation.provenance.adapter_version
        )
        observation_schemas.add(observation.schema_version)
    source_refs.extend(
        (
            f"quality_assessment:{quality.assessment_id}",
            f"current_risk:{risk.current_risk_id}",
        )
    )
    if longitudinal_risk_context is not None:
        source_refs.extend(
            summary.night_episode_revision_ref
            for summary in longitudinal_risk_context.night_summaries
        )
    observation_ids = tuple(item.observation_id for item in observations)
    observation_set_sha256 = stable_hash(observation_ids)
    quality_payload = quality.model_dump(mode="json")
    risk_payload = risk.model_dump(mode="json")
    version_material = {
        "night_episode_revision_id": revision_id,
        "observation_set_sha256": observation_set_sha256,
        "observations": safe_observations,
        "quality": quality_payload,
        "risk": risk_payload,
        "longitudinal_risk_context": (
            None
            if longitudinal_risk_context is None
            else longitudinal_risk_context.model_dump(mode="json")
        ),
    }
    facts = ProductRevisionFacts(
        night_episode_id=episode.night_episode_id,
        night_episode_revision_id=revision_id,
        night_episode_revision_number=revision_number,
        subject_id=episode.subject_id,
        data_mode=episode.data_mode,
        timezone_name=episode.timezone_name,
        local_sleep_date=episode.episode_local_date.isoformat(),
        episode_bed_at=episode.bed_at,
        episode_wake_at=episode.wake_at,
        data_sufficiency=quality.data_sufficiency.value,
        canonical_observations=tuple(safe_observations),
        deterministic_quality=quality_payload,
        deterministic_risk=risk_payload,
        longitudinal_risk_context=longitudinal_risk_context,
        conflict_summaries=(),
        provenance_references=tuple(dict.fromkeys(source_refs)),
        canonical_data_version=stable_hash(version_material),
    )
    return (
        facts,
        observation_set_sha256,
        adapters,
        tuple(sorted(observation_schemas)),
    )


def _require_exact_fast_path_source(
    *,
    pinned_revision_id: str,
    observation_ids: tuple[str, ...],
    quality: DeterministicQualityAssessment,
    risk: CurrentRisk,
) -> None:
    expected_observations = set(observation_ids)
    for label, source_scope in (
        ("quality", quality.source_scope),
        ("risk", risk.source_scope),
    ):
        exact = (
            source_scope.night_episode_revision_id == pinned_revision_id
            if source_scope.night_episode_revision_id is not None
            else set(source_scope.observation_ids) == expected_observations
        )
        if not exact:
            raise ProductAgentStaleSource(
                f"current {label} does not target the pinned Episode revision"
            )


def _skill_versions(
    role_material: list[
        tuple[AnalysisRole, str, FactSnapshot, ProductEpisodeRunResult]
    ],
) -> dict[str, str]:
    return {
        f"{role.value}:{invocation.agent_id.value}": invocation.skill_version
        for role, _episode_id, _snapshot, result in role_material
        for invocation in result.agent_invocations
    }


def _model_versions(
    role_material: list[
        tuple[AnalysisRole, str, FactSnapshot, ProductEpisodeRunResult]
    ],
) -> dict[str, str]:
    return {
        f"{role.value}:{invocation.agent_id.value}": invocation.model_id
        for role, _episode_id, _snapshot, result in role_material
        for invocation in result.agent_invocations
    }


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProductAgentInvariantError(f"Product operation lacks {key}")
    return value


def _optional_positive_int(
    payload: Mapping[str, Any],
    key: str,
    *,
    default: int,
) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProductAgentInvariantError(f"Product operation has invalid {key}")
    return value


def _governed_provider_request_records_for_shared(
    artifact: PreparedProductAgentArtifact,
) -> list[dict[str, str]]:
    """Return full request IDs only for the durable invocation journal."""

    records: list[AgentInvocationRecord] = []
    if artifact.shared_analysis is not None:
        records.extend(artifact.shared_analysis.agent_invocations)
    else:
        for role_run in artifact.role_runs:
            if role_run.result is not None:
                records.extend(role_run.result.agent_invocations)
    return [
        {
            "invocation_id": record.invocation_id,
            "agent_id": record.agent_id.value,
            "provider_request_id": record.provider_request_id,
        }
        for record in records
        if record.provider_request_id is not None
    ]


def _governed_provider_request_records_for_narrative(
    artifact: PreparedElderNarrativeArtifact,
) -> list[dict[str, str]]:
    """Return the render request ID only for the durable invocation journal."""

    record = artifact.narrative.invocation
    if record is None or record.provider_request_id is None:
        return []
    return [
        {
            "invocation_id": record.invocation_id,
            "agent_id": record.agent_id.value,
            "provider_request_id": record.provider_request_id,
        }
    ]


def _required_optional_string(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProductAgentInvariantError(f"Product artifact lacks {label}")
    return value


def _required_projection_identity(
    artifact: PreparedProductAgentArtifact,
    role: AnalysisRole,
) -> str:
    identities = artifact.projection_identities
    if identities is None:
        raise ProductAgentInvariantError(
            "shared artifact has no projection identities"
        )
    value = identities.get(role.value)
    if not isinstance(value, str) or len(value) != 64:
        raise ProductAgentInvariantError(
            f"shared artifact has no {role.value} projection identity"
        )
    return value


def _require_worker_subject_scope(scope: UowScope) -> None:
    if scope.process_role != "worker" or scope.subject_id is None:
        raise ValueError("Product Agent requires an exact worker subject scope")
    if any(
        value is None
        for value in (
            scope.authorization_epoch,
            scope.privacy_epoch,
            scope.retrieval_policy_epoch,
            scope.worker_instance,
        )
    ):
        raise ValueError("Product Agent scope lacks governance or worker fence")


def _terminal(error_code: str) -> WorkResult:
    return WorkResult(
        disposition=WorkDisposition.TERMINAL,
        error_code=error_code,
    )


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=lambda item: item.isoformat(),
    )


def _json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ProductAgentInvariantError("database JSON payload is not an object")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")


__all__ = [
    "LoadedProductAgentSource",
    "PostgresProductAgentRepository",
    "PreparedProductAgentArtifact",
    "PreparedRoleRun",
    "ProductAgentCommitResult",
    "ProductAgentCompositionError",
    "ProductAgentConflict",
    "ProductAgentInvariantError",
    "ProductAgentLease",
    "ProductAgentLeaseLost",
    "ProductAgentProcessor",
    "ProductAgentStaleSource",
    "ProductAgentWorkHandlerAdapter",
    "build_product_agent_worker_handlers",
]
