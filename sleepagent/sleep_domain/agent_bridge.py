"""Exact NightEpisode-revision bridge to the existing four-Agent runtime.

The bridge owns no Agent implementation.  It submits persistent Operations,
leases them in a bounded worker pool, builds a privacy-minimized FactSnapshot,
invokes the injected ``ProductEpisodeRunner``, and atomically appends one
AnalysisRevision plus elder/family/doctor views.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sleepagent.radar_agent.product_agent.contracts import (
    AuthenticatedBinding,
    EpisodeStatus,
    EpisodeType,
    ExecutionMode,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.cold_start import (
    ClaimKind,
    build_unavailable_entry_decisions,
)
from sleepagent.radar_agent.product_agent.runtime_contracts import (
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
)
from sleepagent.radar_agent.product_agent.runtime_ports import (
    ProductEpisodeRunnerPort,
)
from sleepagent.sleep_domain.contracts import (
    AgentAnalysisTrigger,
    AnalysisRevision,
    AnalysisRole,
    AnalysisRoleView,
    AnalysisStatus,
    DataSufficiency,
    DomainEvent,
    DomainEventType,
    Operation,
    OperationStatus,
    RoleViewStatus,
)
from sleepagent.sleep_domain.product_data import (
    INTERNAL_AGENT_ANALYSIS_SCOPE,
    PersistentProductDataProvider,
    ProductDataAuthorization,
    ProductRevisionFacts,
    assert_agent_safe_payload,
)
from sleepagent.sleep_domain.repository import (
    DomainNamespace,
    LeasedOperation,
    SleepDomainRepository,
)

PRODUCT_AGENT_OPERATION_PREFIX = "product_agent_analysis:"
DEFAULT_AGENT_LEASE = timedelta(minutes=10)
MODEL_UNAVAILABLE_CODE = "MODEL_UNAVAILABLE"
AGENT_EXECUTION_FAILED_CODE = "AGENT_EXECUTION_FAILED"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentWorkerBatchResult:
    completed: tuple[AnalysisRevision, ...]
    failed_operation_ids: tuple[str, ...]


class NightEpisodeAgentBridge:
    """Persistent command/worker boundary for the one Product Agent runtime."""

    def __init__(
        self,
        *,
        repository: SleepDomainRepository,
        data_provider: PersistentProductDataProvider,
        episode_runner: ProductEpisodeRunnerPort,
        lease_duration: timedelta = DEFAULT_AGENT_LEASE,
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("Agent lease_duration must be positive")
        self.repository = repository
        self.data_provider = data_provider
        self.episode_runner = episode_runner
        self.lease_duration = lease_duration

    def submit_analysis(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_revision_id: str,
        trigger: AgentAnalysisTrigger,
        service_principal_id: str,
        actor_id: str,
        idempotency_key: str,
        correlation_id: str,
        submitted_at: datetime | None = None,
    ) -> tuple[Operation, bool]:
        """Persist allowed slow-path work without invoking an Agent or model."""

        submitted_at = submitted_at or datetime.now(timezone.utc)
        if not isinstance(trigger, AgentAnalysisTrigger):
            raise ValueError("unsupported Agent analysis trigger")
        revision = self.repository.get_night_episode_revision(
            namespace,
            night_episode_revision_id=night_episode_revision_id,
        )
        if revision is None:
            raise KeyError(
                f"NightEpisodeRevision not found: {night_episode_revision_id}"
            )
        if revision.data_mode != namespace.data_mode:
            raise ValueError("NightEpisodeRevision data_mode mismatch")
        request_material = {
            "schema": "product_agent_analysis_operation.v1",
            "namespace_id": namespace.namespace_id,
            "data_mode": namespace.data_mode.value,
            "night_episode_revision_id": night_episode_revision_id,
            "night_episode_revision_number": revision.revision_number,
            "subject_id": revision.subject_id,
            "trigger": trigger.value,
            "service_principal_id": service_principal_id,
            "actor_id": actor_id,
        }
        operation_hash = stable_hash(
            {
                **request_material,
                "idempotency_key": idempotency_key,
            }
        )
        operation = Operation(
            operation_id=f"agent-operation:{operation_hash[:32]}",
            data_mode=namespace.data_mode,
            operation_type=PRODUCT_AGENT_OPERATION_PREFIX + trigger.value,
            subject_id=revision.subject_id,
            service_principal_id=service_principal_id,
            actor_id=actor_id,
            target_resource_id=night_episode_revision_id,
            idempotency_key=idempotency_key,
            request_sha256=stable_hash(request_material),
            status=OperationStatus.PENDING,
            correlation_id=correlation_id,
            created_at=submitted_at,
            updated_at=submitted_at,
        )
        return self.repository.create_operation(namespace, operation)

    def lease_next(
        self,
        namespace: DomainNamespace,
        *,
        worker_id: str,
        now: datetime | None = None,
    ) -> LeasedOperation | None:
        return self.repository.lease_next_operation(
            namespace,
            operation_type_prefix=PRODUCT_AGENT_OPERATION_PREFIX,
            worker_id=worker_id,
            now=now or datetime.now(timezone.utc),
            lease_duration=self.lease_duration,
        )

    def process_leased(
        self,
        namespace: DomainNamespace,
        leased: LeasedOperation,
        *,
        worker_id: str,
        completed_at: datetime | None = None,
    ) -> AnalysisRevision:
        operation = leased.operation
        if operation.lease_owner != worker_id:
            raise PermissionError("Agent operation is leased to another worker")
        if not operation.operation_type.startswith(PRODUCT_AGENT_OPERATION_PREFIX):
            raise ValueError("operation is not a Product Agent analysis")
        if operation.target_resource_id is None:
            raise ValueError("Agent operation requires an exact revision target")
        trigger = AgentAnalysisTrigger(
            operation.operation_type.removeprefix(PRODUCT_AGENT_OPERATION_PREFIX)
        )
        started_at = datetime.now(timezone.utc)
        revision = self.repository.get_night_episode_revision(
            namespace,
            night_episode_revision_id=operation.target_resource_id,
        )
        if revision is None:
            raise KeyError(
                f"NightEpisodeRevision not found: {operation.target_resource_id}"
            )
        episode = self.repository.get_night_episode(
            namespace,
            night_episode_id=revision.night_episode_id,
        )
        if episode is None:
            raise KeyError(f"NightEpisode not found: {revision.night_episode_id}")
        facts = self.data_provider.load_revision(
            namespace,
            night_episode_revision_id=revision.night_episode_revision_id,
            authorization=ProductDataAuthorization(
                actor_id=operation.actor_id,
                subject_id=revision.subject_id,
                role="system",
                data_mode=namespace.data_mode,
                authorization_scope=(INTERNAL_AGENT_ANALYSIS_SCOPE,),
            ),
        )
        assert_agent_safe_payload(
            facts.model_dump(mode="json"),
            expected_data_mode=namespace.data_mode,
        )
        history = self.repository.list_analysis_revisions(
            namespace,
            night_episode_revision_id=revision.night_episode_revision_id,
        )
        analysis_number = len(history) + 1
        parent_analysis_id = (
            history[-1].analysis_revision_id if history else None
        )
        analysis_id = _stable_id(
            "analysis-revision",
            namespace.namespace_id,
            revision.night_episode_revision_id,
            str(analysis_number),
            operation.operation_id,
        )

        insufficient = _is_data_insufficient(facts)
        runner_configured = _runner_is_configured(self.episode_runner)
        role_views: list[AnalysisRoleView] = []
        failure_codes: set[str] = set()
        skill_versions: dict[str, str] = {}
        model_versions: dict[str, str] = {}
        for role in AnalysisRole:
            run_id = _product_agent_episode_id(
                namespace,
                revision.night_episode_revision_id,
                analysis_number,
                role,
            )
            if not runner_configured and not insufficient:
                failure_codes.add(MODEL_UNAVAILABLE_CODE)
                role_views.append(
                    _degraded_role_view(
                        analysis_id=analysis_id,
                        revision=revision,
                        role=role,
                        run_id=run_id,
                        generated_at=started_at,
                        failure_code=MODEL_UNAVAILABLE_CODE,
                        content="模型当前不可用；该分析仍处于明确降级状态。",
                    )
                )
                continue
            try:
                request = self._build_request(
                    namespace=namespace,
                    operation=operation,
                    trigger=trigger,
                    facts=facts,
                    role=role,
                    run_id=run_id,
                    insufficient=insufficient,
                    created_at=started_at,
                )
                assert_agent_safe_payload(
                    request.tool_inputs,
                    expected_data_mode=namespace.data_mode,
                )
                result = self.episode_runner.run(request)
                role_view = _role_view_from_result(
                    analysis_id=analysis_id,
                    revision=revision,
                    role=role,
                    run_id=run_id,
                    result=result,
                    source_refs=facts.provenance_references,
                    generated_at=started_at,
                )
                role_views.append(role_view)
                failure_codes.update(role_view.failure_codes)
                for invocation in result.agent_invocations:
                    key = f"{role.value}:{invocation.agent_id.value}"
                    skill_versions[key] = invocation.skill_version
                    model_versions[key] = invocation.model_id
            except Exception as exc:
                failure_code = (
                    MODEL_UNAVAILABLE_CODE
                    if _looks_like_model_unavailable(exc)
                    else AGENT_EXECUTION_FAILED_CODE
                )
                failure_codes.add(failure_code)
                role_views.append(
                    _degraded_role_view(
                        analysis_id=analysis_id,
                        revision=revision,
                        role=role,
                        run_id=run_id,
                        generated_at=started_at,
                        failure_code=failure_code,
                        content=(
                            "模型当前不可用；该分析仍处于明确降级状态。"
                            if failure_code == MODEL_UNAVAILABLE_CODE
                            else "Agent 分析未能可靠完成；已保留确定性结果。"
                        ),
                    )
                )

        all_ready = all(
            view.status == RoleViewStatus.READY for view in role_views
        )
        if all_ready:
            analysis_status = AnalysisStatus.READY
            execution_mode = "intelligent"
        else:
            analysis_status = AnalysisStatus.DEGRADED
            execution_mode = (
                "deterministic_only" if insufficient else "safe_degraded"
            )
            if insufficient:
                failure_codes.add("DATA_INSUFFICIENT")
        primary_run_id = _product_agent_episode_id(
            namespace,
            revision.night_episode_revision_id,
            analysis_number,
            AnalysisRole.ELDER,
        )
        completed_at = completed_at or datetime.now(timezone.utc)
        analysis = AnalysisRevision(
            analysis_revision_id=analysis_id,
            night_episode_id=revision.night_episode_id,
            night_episode_revision_id=revision.night_episode_revision_id,
            night_episode_revision_number=revision.revision_number,
            data_mode=namespace.data_mode,
            subject_id=revision.subject_id,
            revision_number=analysis_number,
            parent_analysis_revision_id=parent_analysis_id,
            analysis_run_id=primary_run_id,
            observation_set_sha256=revision.observation_set_sha256,
            source_report_sha256=revision.source_report_sha256,
            adapter_versions=episode.pinned_adapter_versions,
            observation_schema_versions=(
                episode.pinned_observation_schema_versions
            ),
            policy_versions=episode.pinned_policy_versions,
            skill_versions=skill_versions,
            model_versions=model_versions,
            data_sufficiency=revision.data_sufficiency,
            status=analysis_status,
            execution_mode=execution_mode,
            failure_codes=tuple(sorted(failure_codes)),
            result_resource_id=analysis_id,
            created_at=completed_at,
        )
        model_failure = MODEL_UNAVAILABLE_CODE in failure_codes
        terminal_status = (
            OperationStatus.FAILED
            if model_failure and not insufficient
            else OperationStatus.SUCCEEDED
        )
        terminal_operation = Operation.model_validate(
            {
                **operation.model_dump(mode="python"),
                "status": terminal_status,
                "lease_owner": None,
                "lease_expires_at": None,
                "result_resource_id": analysis.analysis_revision_id,
                "error_code": MODEL_UNAVAILABLE_CODE if terminal_status == OperationStatus.FAILED else None,
                "updated_at": completed_at,
            }
        )
        event = DomainEvent(
            event_id=_stable_id(
                "domain-event",
                namespace.namespace_id,
                analysis.analysis_revision_id,
            ),
            event_type=(
                DomainEventType.AGENT_ANALYSIS_READY
                if analysis.status == AnalysisStatus.READY
                else DomainEventType.AGENT_ANALYSIS_DEGRADED
            ),
            event_version="1",
            data_mode=namespace.data_mode,
            aggregate_type="AnalysisRevision",
            aggregate_id=analysis.analysis_revision_id,
            aggregate_version=analysis.revision_number,
            per_aggregate_sequence=1,
            delivery_offset=1,
            subject_id=analysis.subject_id,
            night_episode_id=analysis.night_episode_id,
            night_episode_revision_id=analysis.night_episode_revision_id,
            operation_id=operation.operation_id,
            event_occurred_at=completed_at,
            persisted_at=completed_at,
            correlation_id=operation.correlation_id,
            causation_id=operation.operation_id,
            attributes={
                "analysis_status": analysis.status.value,
                "execution_mode": analysis.execution_mode,
                "trigger": trigger.value,
                "roles": ",".join(role.value for role in AnalysisRole),
            },
        )
        self.repository.commit_agent_analysis(
            namespace,
            analysis=analysis,
            role_views=tuple(role_views),
            terminal_operation=terminal_operation,
            expected_operation_cas_version=leased.cas_version,
            worker_id=worker_id,
            event=event,
            committed_at=completed_at,
        )
        return analysis

    def _build_request(
        self,
        *,
        namespace: DomainNamespace,
        operation: Operation,
        trigger: AgentAnalysisTrigger,
        facts: ProductRevisionFacts,
        role: AnalysisRole,
        run_id: str,
        insufficient: bool,
        created_at: datetime,
    ) -> ProductEpisodeRunRequest:
        internal_subject_id = (
            f"{namespace.namespace_id}::subject::{facts.subject_id}"
        )
        binding = AuthenticatedBinding(
            actor_id=operation.actor_id,
            subject_id=internal_subject_id,
            role=role.value,
            authorization_scope=(
                "read_sleep_data",
                "read_device_data",
                "draft_material",
            ),
        )
        care_version = self.episode_runner.commit_controller.care_store.get(
            internal_subject_id
        ).version
        memory_version = self.episode_runner.commit_controller.memory_store.get(
            internal_subject_id
        ).version
        readiness_decisions = build_unavailable_entry_decisions(
            decision_namespace=f"{run_id}:cold-start",
            claim_kind=ClaimKind.DESCRIBE_CURRENT_NIGHT,
        )
        snapshot = self.data_provider.build_fact_snapshot(
            facts,
            binding=binding,
            fact_snapshot_id=f"fact-snapshot:{stable_hash((run_id, facts.canonical_data_version))[:32]}",
            care_context_version=care_version,
            memory_context_version=memory_version,
            created_at=created_at,
            readiness_decisions=readiness_decisions,
        )
        episode_type = (
            EpisodeType.DATA_QUALITY_RECOVERY
            if insufficient
            else (
                EpisodeType.CARE_FOLLOWUP
                if trigger
                in {
                    AgentAnalysisTrigger.FEEDBACK,
                    AgentAnalysisTrigger.FOLLOW_UP,
                }
                else EpisodeType.MORNING_REVIEW
            )
        )
        return ProductEpisodeRunRequest(
            episode_id=run_id,
            episode_type=episode_type,
            objective=(
                "基于一个精确 NightEpisode revision 和已授权 Canonical "
                f"Observations，为 {role.value} 生成受证据约束的睡眠照护视图。"
            ),
            fact_snapshot=snapshot,
            runtime_readiness_decisions=readiness_decisions,
            audience_role=role.value,
            tool_inputs=facts.tool_inputs(),
            personalized=True,
            doctor_material=role == AnalysisRole.DOCTOR,
            external_action=False,
            idempotency_key=f"{operation.operation_id}:{role.value}",
        )


class AgentWorkerPool:
    """Bounded Agent-only worker pool, separate from ingestion/query paths."""

    def __init__(
        self,
        *,
        bridge: NightEpisodeAgentBridge,
        namespace: DomainNamespace,
        max_workers: int = 2,
        worker_id_prefix: str = "product-agent-worker",
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if max_workers < 1 or max_workers > 32:
            raise ValueError("max_workers must be between 1 and 32")
        if poll_interval_seconds <= 0 or poll_interval_seconds > 60:
            raise ValueError("poll interval must be in (0, 60]")
        self.bridge = bridge
        self.namespace = namespace
        self.max_workers = max_workers
        self.worker_id_prefix = worker_id_prefix
        self.poll_interval_seconds = poll_interval_seconds
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=worker_id_prefix,
        )
        self._stop_event = threading.Event()
        self._coordinator: threading.Thread | None = None

    def process_once(
        self,
        *,
        now: datetime | None = None,
    ) -> AgentWorkerBatchResult:
        now = now or datetime.now(timezone.utc)
        leased_items: list[tuple[LeasedOperation, str]] = []
        for index in range(self.max_workers):
            worker_id = f"{self.worker_id_prefix}-{index + 1}"
            leased = self.bridge.lease_next(
                self.namespace,
                worker_id=worker_id,
                now=now,
            )
            if leased is None:
                break
            leased_items.append((leased, worker_id))
        futures: list[tuple[LeasedOperation, Future[AnalysisRevision]]] = [
            (
                leased,
                self._executor.submit(
                    self.bridge.process_leased,
                    self.namespace,
                    leased,
                    worker_id=worker_id,
                ),
            )
            for leased, worker_id in leased_items
        ]
        completed: list[AnalysisRevision] = []
        failed: list[str] = []
        for leased, future in futures:
            try:
                completed.append(future.result())
            except Exception:
                failed.append(leased.operation.operation_id)
        return AgentWorkerBatchResult(
            completed=tuple(completed),
            failed_operation_ids=tuple(failed),
        )

    def start(self) -> None:
        if self._coordinator is not None and self._coordinator.is_alive():
            return
        self._stop_event.clear()
        self._coordinator = threading.Thread(
            target=self._run_loop,
            name=f"{self.worker_id_prefix}-coordinator",
            daemon=True,
        )
        self._coordinator.start()

    def stop(self, *, wait: bool = True) -> None:
        self._stop_event.set()
        coordinator = self._coordinator
        if wait and coordinator is not None:
            coordinator.join(timeout=min(60.0, self.poll_interval_seconds + 5.0))
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.process_once()
            except Exception:
                LOGGER.exception(
                    "Product Agent worker-pool coordinator iteration failed"
                )
            self._stop_event.wait(self.poll_interval_seconds)


def _role_view_from_result(
    *,
    analysis_id: str,
    revision: Any,
    role: AnalysisRole,
    run_id: str,
    result: ProductEpisodeRunResult,
    source_refs: tuple[str, ...],
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
        role_view_id=_stable_id("role-view", analysis_id, role.value),
        analysis_revision_id=analysis_id,
        night_episode_id=revision.night_episode_id,
        night_episode_revision_id=revision.night_episode_revision_id,
        data_mode=revision.data_mode,
        subject_id=revision.subject_id,
        role=role,
        status=status,
        product_agent_episode_id=run_id,
        execution_mode=result.receipt.execution_mode.value,
        content=publication.text if publication is not None else None,
        context_notice=(
            publication.context_notice if publication is not None else None
        ),
        claim_refs=(
            tuple(publication.claim_refs) if publication is not None else ()
        ),
        source_refs=source_refs,
        failure_codes=failure_codes,
        generated_at=generated_at,
    )


def _degraded_role_view(
    *,
    analysis_id: str,
    revision: Any,
    role: AnalysisRole,
    run_id: str,
    generated_at: datetime,
    failure_code: str,
    content: str,
) -> AnalysisRoleView:
    return AnalysisRoleView(
        role_view_id=_stable_id("role-view", analysis_id, role.value),
        analysis_revision_id=analysis_id,
        night_episode_id=revision.night_episode_id,
        night_episode_revision_id=revision.night_episode_revision_id,
        data_mode=revision.data_mode,
        subject_id=revision.subject_id,
        role=role,
        status=RoleViewStatus.DEGRADED,
        product_agent_episode_id=run_id,
        execution_mode="safe_degraded",
        content=content,
        context_notice="这是明确标记的 Agent slow-path 降级结果。",
        failure_codes=(failure_code,),
        generated_at=generated_at,
    )


def _runner_is_configured(runner: ProductEpisodeRunnerPort) -> bool:
    from sleepagent.radar_agent.product_agent.agents import ProductAgentRoster

    roster = getattr(runner, "agent_roster", None)
    if type(roster) is not ProductAgentRoster:
        return False
    models = [
        roster.sleepcare.planning_model,
        *(item.model for item in roster),
    ]
    return all(
        model is not None and bool(getattr(model, "is_configured", True))
        for model in models
    )


def _is_data_insufficient(facts: ProductRevisionFacts) -> bool:
    if facts.data_sufficiency != DataSufficiency.SUFFICIENT.value:
        return True
    return (
        facts.deterministic_quality.get("data_sufficiency")
        not in {None, DataSufficiency.SUFFICIENT.value}
    )


def _product_agent_episode_id(
    namespace: DomainNamespace,
    night_episode_revision_id: str,
    analysis_number: int,
    role: AnalysisRole,
) -> str:
    return _stable_id(
        "product-agent-episode",
        namespace.namespace_id,
        night_episode_revision_id,
        str(analysis_number),
        role.value,
    )


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:32]}"


def _looks_like_model_unavailable(exc: Exception) -> bool:
    text = f"{type(exc).__name__}:{exc}".lower()
    markers = (
        "provider",
        "model",
        "api key",
        "timeout",
        "connection",
        "unavailable",
        "llm",
    )
    return any(marker in text for marker in markers)


__all__ = [
    "AGENT_EXECUTION_FAILED_CODE",
    "DEFAULT_AGENT_LEASE",
    "MODEL_UNAVAILABLE_CODE",
    "PRODUCT_AGENT_OPERATION_PREFIX",
    "AgentWorkerBatchResult",
    "AgentWorkerPool",
    "NightEpisodeAgentBridge",
    "PersistentProductDataProvider",
]
