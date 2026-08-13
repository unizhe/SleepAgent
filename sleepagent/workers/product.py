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
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sleepagent.config import (
    DataMode as BackendDataMode,
    ModelMode,
    ProcessRole,
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
    EpisodeStatus,
    EpisodeType,
    ExecutionMode,
    FactSnapshot,
    SourceScope,
    SourceScopeKind,
    stable_hash,
)
from sleepagent.runtime.results import (
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
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
    RoleViewStatus,
    SleepObservation,
)
from sleepagent.domain.episodes import NightEpisodeV2, UUID7Generator
from sleepagent.domain.product_data import (
    ProductRevisionFacts,
    _project_observation,
)
from sleepagent.workers.runtime import (
    B3ClaimInvariantError,
    InvocationKind,
    LeaseClaim,
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


class PreparedRoleRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: AnalysisRole
    product_episode_id: str = Field(min_length=1)
    fact_snapshot: FactSnapshot
    result: ProductEpisodeRunResult
    role_view: AnalysisRoleView


class PreparedProductAgentArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["product_agent_prepared_attempt.v1"] = (
        "product_agent_prepared_attempt.v1"
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
    prepared_at: datetime

    @model_validator(mode="after")
    def exact_three_role_result(self) -> "PreparedProductAgentArtifact":
        roles = tuple(item.role for item in self.role_runs)
        if set(roles) != set(AnalysisRole) or len(roles) != len(AnalysisRole):
            raise ValueError("prepared Product attempt requires exactly three roles")
        if any(
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
            or item.result.receipt.episode_id != item.product_episode_id
            or item.result.receipt.fact_snapshot_id
            != item.fact_snapshot.fact_snapshot_id
            or item.result.receipt.fact_snapshot_hash
            != item.fact_snapshot.fact_snapshot_hash
            for item in self.role_runs
        ):
            raise ValueError("prepared role views are not bound to the analysis")
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


@dataclass(frozen=True, slots=True)
class ProductAgentCommitResult:
    operation_id: str
    product_attempt_id: str
    analysis_revision_id: str
    role_view_ids: tuple[str, ...]
    analysis_status: str
    induction_operation_id: str
    induction_manifest_id: str


class ProductAgentProcessor:
    """Prepare outside a transaction, then publish under the current fence."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        runtime_bundle: ProductRuntimeBundle,
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

        return self.persist_and_commit(scope, lease, artifact)

    def load_source(
        self,
        scope: UowScope,
        lease: ProductAgentLease,
    ) -> LoadedProductAgentSource:
        """Load the exact source in a short transaction before model dispatch."""

        _require_worker_subject_scope(scope)
        with self.uow_factory.begin(scope) as uow:
            repository = self.repository_factory(uow.connection, scope)
            source = repository.load_source(lease)
            uow.commit()
        return source

    def persist_and_commit(
        self,
        scope: UowScope,
        lease: ProductAgentLease,
        artifact: PreparedProductAgentArtifact,
    ) -> ProductAgentCommitResult:
        """Persist an invocation-journaled artifact, then CAS-publish it."""

        with self.uow_factory.begin(scope) as uow:
            repository = self.repository_factory(uow.connection, scope)
            repository.persist_prepared(lease, artifact)
            uow.commit()

        committed_at = self.now_factory()
        _require_aware(committed_at, "committed_at")
        with self.uow_factory.begin(scope) as uow:
            repository = self.repository_factory(uow.connection, scope)
            result = repository.commit_prepared(
                lease,
                artifact,
                committed_at=committed_at,
            )
            uow.commit()
        return result

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
        for role in AnalysisRole:
            product_episode_id = self.id_generator(prepared_at)
            snapshot = _fact_snapshot_for_role(
                scope=scope,
                source=source,
                role=role,
                fact_snapshot_id=self.id_generator(prepared_at),
                created_at=prepared_at,
            )
            request = ProductEpisodeRunRequest(
                episode_id=product_episode_id,
                episode_type=EpisodeType.MORNING_REVIEW,
                objective=(
                    "基于一个精确 NightEpisode revision 和已授权 Canonical "
                    f"Observations，为 {role.value} 生成受证据约束的睡眠照护视图。"
                ),
                fact_snapshot=snapshot,
                audience_role=role.value,
                tool_inputs=source.facts.tool_inputs(),
                personalized=True,
                doctor_material=role == AnalysisRole.DOCTOR,
                idempotency_key=f"{source.operation_id}:{role.value}",
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

    def load_source(self, lease: ProductAgentLease) -> LoadedProductAgentSource:
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                """
                SELECT operation.operation_json, operation.subject_id,
                       operation.policy_sha256, operation.target_resource_id,
                       episode.episode_json, episode.current_revision_id,
                       revision.revision_json, revision.revision_number,
                       quality.assessment_json, risk.risk_json
                FROM public.sleep_domain_operations AS operation
                JOIN public.sleep_domain_night_episodes AS episode
                  ON episode.night_episode_id = operation.target_resource_id
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
                  AND operation.operation_type = 'product_agent'
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
                FOR UPDATE OF operation, episode
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
        )

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

    def commit_prepared(
        self,
        lease: ProductAgentLease,
        artifact: PreparedProductAgentArtifact,
        *,
        committed_at: datetime,
    ) -> ProductAgentCommitResult:
        self._validate_artifact_identity(lease, artifact)
        self._lock_and_validate_epochs()
        cursor = self.connection.cursor()
        try:
            operation_json = self._lock_operation_fence(cursor, lease, artifact)
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
            self._validate_analysis_parent(cursor, artifact.analysis)
            analysis = artifact.analysis
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
                    analysis.model_dump_json(),
                    committed_at,
                ),
            )
            for role_run in artifact.role_runs:
                view = role_run.role_view
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
                        view.model_dump_json(),
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
                        stable_hash(view.model_dump(mode="json")),
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
                        "projection_sha256": stable_hash(
                            item.role_view.model_dump(mode="json")
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
              AND operation_type = 'product_agent'
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

    def _lock_prepared_artifact(
        self,
        cursor: Any,
        artifact: PreparedProductAgentArtifact,
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
        artifact: PreparedProductAgentArtifact,
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
        if (
            old_sequence != lease.attempt_sequence
            or old_generation >= lease.lease_generation
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
        try:
            processor = self._resolve_processor(context)
            if isinstance(processor, ProductAgentProcessor):
                source = processor.load_source(scope, lease)
                prepared_at = processor.now_factory()
                _require_aware(prepared_at, "prepared_at")
                request = {
                    "schema_version": "product_agent_model_request.v1",
                    "operation_id": source.operation_id,
                    "night_episode_id": source.night_episode_id,
                    "night_episode_revision_id": source.night_episode_revision_id,
                    "source_state_version": source.night_episode_revision_number,
                    "observation_set_sha256": source.observation_set_sha256,
                    "policy_sha256": source.policy_sha256,
                    "model_mode": self._model_mode.value,
                }
                invocation_key = (
                    f"product-agent:{source.operation_id}:"
                    f"{source.night_episode_revision_id}:"
                    f"{self._model_mode.value}.v1"
                )

                def invoke_model() -> tuple[Mapping[str, Any], str | None]:
                    artifact = processor.prepare(
                        scope=scope,
                        source=source,
                        lease=lease,
                        prepared_at=prepared_at,
                    )
                    return (
                        {
                            "schema_version": "product_agent_model_response.v1",
                            "artifact": artifact.model_dump(mode="json"),
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
                result = processor.persist_and_commit(scope, lease, artifact)
            else:
                # Explicit test processors retain the narrow adapter seam; the
                # production composition always builds ProductAgentProcessor.
                result = processor.process(scope, lease)  # type: ignore[unreachable]
        except ProductAgentStaleSource:
            return _terminal("product_agent_stale_source")
        except ProductAgentLeaseLost:
            context.mark_lease_lost()
            return WorkResult(
                disposition=WorkDisposition.OUTCOME_UNKNOWN,
                error_code="product_agent_lease_lost_reconciliation_required",
            )
        except ProductAgentInvariantError:
            return _terminal("product_agent_invariant_violation")
        except ProductAgentConflict:
            return WorkResult(
                disposition=WorkDisposition.RETRYABLE,
                error_code="product_agent_conflict",
            )
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
    if settings.data_mode != BackendDataMode.REPLAY:
        raise ProductAgentCompositionError(
            "the current Product Agent vertical slice requires replay data"
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
        or claim.metadata.get("operation_type") != "product_agent"
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
) -> FactSnapshot:
    local_date = date.fromisoformat(source.facts.local_sleep_date)
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
        source_scope=SourceScope(
            kind=SourceScopeKind.CURRENT_NIGHT,
            as_of=created_at,
            timezone_name=source.facts.timezone_name,
            date_start=local_date,
            date_end=local_date,
            valid_night_count=1,
        ),
        canonical_data_version=source.facts.canonical_data_version,
        active_constraint_codes=tuple(
            sorted(
                {
                    *source.facts.deterministic_quality.get("reason_codes", ()),
                    *source.facts.deterministic_risk.get("reason_codes", ()),
                }
            )
        ),
        source_refs=source.facts.agent_source_refs(),
        created_at=created_at,
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


def _public_today_projection(
    *,
    analysis: AnalysisRevision,
    view: AnalysisRoleView,
    projection_version: int,
    episode_local_date: date,
    assignment_basis: str,
    committed_at: datetime,
) -> dict[str, Any]:
    """Map one internal role view to the only Stage-1 public projection."""

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
    content: dict[str, Any] = {
        "audience": view.role.value,
        "summary_text": view.content or fallback_summary,
        "context_notice": view.context_notice or fallback_notice,
    }
    if view.role == AnalysisRole.DOCTOR:
        # Public evidence references are deliberately empty in the first slice.
        # Internal claim/source references never cross this mapper.
        content["evidence_refs"] = []
    return {
        "schema_version": "product_sleep_today.v1",
        "data_mode": analysis.data_mode.value,
        "synthetic_non_release": analysis.data_mode == DataMode.REPLAY,
        "state": view.status.value,
        "subject_ref": analysis.subject_id,
        "role": view.role.value,
        "episode_id": analysis.night_episode_id,
        "episode_revision_id": analysis.night_episode_revision_id,
        "episode_local_date": episode_local_date.isoformat(),
        "assignment_basis": assignment_basis,
        "analysis_revision_id": analysis.analysis_revision_id,
        "projection_id": view.role_view_id,
        "projection_version": projection_version,
        "committed_at": committed_at.isoformat(),
        "content": content,
    }


def _build_facts(
    *,
    episode: NightEpisodeV2,
    revision_id: str,
    revision_number: int,
    observations: tuple[SleepObservation, ...],
    quality: DeterministicQualityAssessment,
    risk: CurrentRisk,
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
        projected, refs = _project_observation(observation)
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
    }
    facts = ProductRevisionFacts(
        night_episode_id=episode.night_episode_id,
        night_episode_revision_id=revision_id,
        night_episode_revision_number=revision_number,
        subject_id=episode.subject_id,
        data_mode=episode.data_mode,
        timezone_name=episode.timezone_name,
        local_sleep_date=episode.episode_local_date.isoformat(),
        data_sufficiency=quality.data_sufficiency.value,
        canonical_observations=tuple(safe_observations),
        deterministic_quality=quality_payload,
        deterministic_risk=risk_payload,
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
