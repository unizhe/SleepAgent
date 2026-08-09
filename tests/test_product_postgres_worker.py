from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

import pytest

from sleepagent.radar_agent.persistence.uow import UowScope
from sleepagent.radar_agent.product_agent.contracts import (
    CommunicationDraft,
    EpisodeReceipt,
    EpisodeStatus,
    ExecutionMode,
)
from sleepagent.radar_agent.product_agent.postgres_worker import (
    LoadedProductAgentSource,
    ProductAgentCommitResult,
    ProductAgentConflict,
    ProductAgentInvariantError,
    ProductAgentLease,
    ProductAgentLeaseLost,
    ProductAgentProcessor,
    ProductAgentStaleSource,
    ProductAgentWorkHandlerAdapter,
)
from sleepagent.radar_agent.product_agent.runtime_contracts import (
    CommitFrozenConfirmedAction,
    ProductEpisodeRunRequest,
    ProductEpisodeRunResult,
)
from sleepagent.radar_agent.product_agent.runtime_factory import (
    ProductRuntimeBundle,
)
from sleepagent.radar_agent.product_agent.runtime_ports import (
    ProductEpisodeRunnerPort,
)
from sleepagent.sleep_domain.contracts import AnalysisRole, DataMode
from sleepagent.sleep_domain.episode_v2 import (
    finalize_episode_date,
    open_episode_v2,
    uuid7_from_parts,
)
from sleepagent.sleep_domain.product_data import ProductRevisionFacts
from sleepagent.worker_runtime import (
    LeaseClaim,
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc
NOW = datetime(2026, 8, 7, 0, 0, tzinfo=UTC)


def _scope(*, subject_id: str = "subject-1") -> UowScope:
    return UowScope(
        namespace_id="replay:pytest",
        data_mode="replay",
        process_role="worker",
        purpose="worker",
        service_principal_id="sleepagent-worker-test",
        namespace_generation=3,
        subject_id=subject_id,
        run_id="run-1",
        arm_id="arm-1",
        authorization_epoch=7,
        privacy_epoch=8,
        retrieval_policy_epoch=9,
        worker_instance="worker-1",
    )


def _lease() -> ProductAgentLease:
    return ProductAgentLease(
        operation_id="product-operation-1",
        attempt_sequence=2,
        lease_generation=4,
        fencing_token="f" * 64,
        worker_instance="worker-1",
    )


def _source() -> LoadedProductAgentSource:
    collection_start = NOW - timedelta(hours=10)
    provisional = open_episode_v2(
        namespace_id="replay:pytest",
        namespace_generation=3,
        data_mode=DataMode.REPLAY,
        run_id="run-1",
        arm_id="arm-1",
        subject_id="subject-1",
        opening_source_idempotency_identity="source-report-1",
        timezone_name="Asia/Shanghai",
        boundary_policy_version="wake-boundary.v1",
        collection_start_at=collection_start,
        deterministic_close_deadline_at=NOW + timedelta(hours=3),
        bed_at=collection_start,
        id_generator=lambda _at: uuid7_from_parts(1_775_692_800_000, 1),
    )
    episode = finalize_episode_date(
        provisional,
        committed_at=NOW,
        wake_at=NOW,
    )
    facts = ProductRevisionFacts(
        night_episode_id=episode.night_episode_id,
        night_episode_revision_id="night-revision-1",
        night_episode_revision_number=2,
        subject_id="subject-1",
        data_mode=DataMode.REPLAY,
        timezone_name="Asia/Shanghai",
        local_sleep_date="2026-08-07",
        data_sufficiency="sufficient",
        canonical_observations=(
            {
                "schema_version": "agent_safe_observation.v1",
                "observation_id": "observation-1",
                "data_mode": "replay",
                "metric": "sleep_duration_minutes",
                "value": 420,
            },
        ),
        deterministic_quality={
            "data_mode": "replay",
            "coverage_ratio": 0.95,
            "reason_codes": [],
        },
        deterministic_risk={
            "data_mode": "replay",
            "risk_state": "no_urgent_signal",
            "reason_codes": [],
        },
        conflict_summaries=(),
        provenance_references=(
            "night_episode_revision:replay:night-revision-1",
            "canonical_observation:observation-1",
        ),
        canonical_data_version="c" * 64,
    )
    return LoadedProductAgentSource(
        operation_id="product-operation-1",
        operation_json={
            "night_episode_revision_id": "night-revision-1",
        },
        night_episode_id=episode.night_episode_id,
        night_episode_revision_id="night-revision-1",
        night_episode_revision_number=2,
        previous_analysis_revision_id=None,
        next_analysis_revision_number=1,
        subject_id="subject-1",
        policy_sha256="p" * 64,
        episode=episode,
        facts=facts,
        observation_set_sha256="a" * 64,
        adapter_versions={"synthetic": "1.0.0"},
        observation_schema_versions=("sleep_observation.v1",),
        policy_versions={"quality": "quality.v1"},
    )


@dataclass
class _TransactionTrace:
    active: int = 0
    events: list[str] = field(default_factory=list)


class _UnitOfWork:
    def __init__(self, trace: _TransactionTrace) -> None:
        self.trace = trace
        self.connection = object()
        self.committed = False

    def __enter__(self) -> "_UnitOfWork":
        assert self.trace.active == 0
        self.trace.active += 1
        self.trace.events.append("uow-enter")
        return self

    def commit(self) -> None:
        assert self.trace.active == 1
        self.committed = True
        self.trace.events.append("uow-commit")

    def __exit__(
        self,
        exc_type: Any,
        exc: Any,
        traceback: Any,
    ) -> Literal[False]:
        del exc_type, exc, traceback
        self.trace.active -= 1
        self.trace.events.append("uow-exit")
        return False


class _UnitOfWorkFactory:
    def __init__(self, trace: _TransactionTrace) -> None:
        self.trace = trace

    def begin(self, scope: UowScope) -> _UnitOfWork:
        assert scope == _scope()
        return _UnitOfWork(self.trace)


@dataclass
class _RepositoryState:
    trace: _TransactionTrace
    source: LoadedProductAgentSource
    prepared: Any = None
    committed: Any = None


class _Repository:
    def __init__(self, state: _RepositoryState) -> None:
        self.state = state

    def load_source(self, lease: ProductAgentLease) -> LoadedProductAgentSource:
        assert self.state.trace.active == 1
        assert lease == _lease()
        self.state.trace.events.append("load")
        return self.state.source

    def persist_prepared(self, lease: ProductAgentLease, artifact: Any) -> None:
        assert self.state.trace.active == 1
        assert lease == _lease()
        self.state.trace.events.append("prepared")
        self.state.prepared = artifact

    def commit_prepared(
        self,
        lease: ProductAgentLease,
        artifact: Any,
        *,
        committed_at: datetime,
    ) -> ProductAgentCommitResult:
        assert self.state.trace.active == 1
        assert lease == _lease()
        assert committed_at == NOW + timedelta(seconds=1)
        assert artifact is self.state.prepared
        self.state.trace.events.append("commit")
        self.state.committed = artifact
        return ProductAgentCommitResult(
            operation_id=artifact.operation_id,
            product_attempt_id=artifact.product_attempt_id,
            analysis_revision_id=artifact.analysis.analysis_revision_id,
            role_view_ids=tuple(
                item.role_view.role_view_id for item in artifact.role_runs
            ),
            analysis_status=artifact.analysis.status.value,
        )


class _Runner:
    def __init__(self, trace: _TransactionTrace) -> None:
        self.trace = trace

    def run(self, request: ProductEpisodeRunRequest) -> ProductEpisodeRunResult:
        assert self.trace.active == 0, "runner executed inside a database UoW"
        assert request.audience_role is not None
        self.trace.events.append(f"run:{request.audience_role}")
        publication = CommunicationDraft(
            draft_id=f"draft:{request.audience_role}",
            audience_role=request.audience_role,
            text=f"{request.audience_role} committed role view",
            context_notice="synthetic replay only",
        )
        return ProductEpisodeRunResult(
            registry_hash="r" * 64,
            receipt=EpisodeReceipt(
                episode_id=request.episode_id,
                episode_type=request.episode_type,
                receipt_revision=1,
                terminal=True,
                execution_mode=ExecutionMode.INTELLIGENT,
                status=EpisodeStatus.COMPLETE,
                goal_achieved=True,
                fact_snapshot_id=request.fact_snapshot.fact_snapshot_id,
                fact_snapshot_hash=request.fact_snapshot.fact_snapshot_hash,
                source_scope=request.fact_snapshot.source_scope,
                final_episode_state_revision=1,
                trace_ref=f"trace:{request.audience_role}",
            ),
            publication=publication,
            publication_delivered=True,
        )

    def commit_frozen_confirmations(
        self,
        command: CommitFrozenConfirmedAction,
    ) -> ProductEpisodeRunResult:
        del command
        raise AssertionError("worker fake only supports run()")

    def process_induction_jobs(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[object]:
        del now, limit
        raise AssertionError("worker fake only supports run()")


@dataclass(frozen=True, slots=True)
class _RuntimeBundleAdapter:
    """Minimal explicit bundle boundary for worker unit tests."""

    runner: ProductEpisodeRunnerPort


def _runtime_bundle(runner: ProductEpisodeRunnerPort) -> ProductRuntimeBundle:
    return cast(ProductRuntimeBundle, _RuntimeBundleAdapter(runner=runner))


def _sequential_id_generator() -> Any:
    sequence = 0

    def generate(_at: datetime | None) -> str:
        nonlocal sequence
        sequence += 1
        return f"generated-{sequence}"

    return generate


def test_processor_closes_uow_while_running_roles_then_atomically_hands_off() -> None:
    trace = _TransactionTrace()
    state = _RepositoryState(trace=trace, source=_source())
    times = iter((NOW, NOW + timedelta(seconds=1)))
    processor = ProductAgentProcessor(
        _UnitOfWorkFactory(trace),  # type: ignore[arg-type]
        runtime_bundle=_runtime_bundle(_Runner(trace)),
        id_generator=_sequential_id_generator(),
        now_factory=lambda: next(times),
        repository_factory=lambda _connection, _scope_value: _Repository(state),  # type: ignore[arg-type,return-value]
    )

    result = processor.process(_scope(), _lease())

    business_events = [
        event
        for event in trace.events
        if event in {"load", "prepared", "commit"} or event.startswith("run:")
    ]
    assert business_events == [
        "load",
        "run:elder",
        "run:family",
        "run:doctor",
        "prepared",
        "commit",
    ]
    assert trace.active == 0
    assert trace.events.count("uow-enter") == 3
    assert trace.events.count("uow-commit") == 3
    assert trace.events.count("uow-exit") == 3
    assert state.prepared is state.committed
    assert result.analysis_revision_id == state.prepared.analysis.analysis_revision_id


def test_prepared_artifact_has_exact_three_views_bound_to_one_source_and_analysis() -> None:
    trace = _TransactionTrace()
    state = _RepositoryState(trace=trace, source=_source())
    times = iter((NOW, NOW + timedelta(seconds=1)))
    processor = ProductAgentProcessor(
        _UnitOfWorkFactory(trace),  # type: ignore[arg-type]
        runtime_bundle=_runtime_bundle(_Runner(trace)),
        id_generator=_sequential_id_generator(),
        now_factory=lambda: next(times),
        repository_factory=lambda _connection, _scope_value: _Repository(state),  # type: ignore[arg-type,return-value]
    )

    processor.process(_scope(), _lease())

    artifact = state.prepared
    assert artifact is not None
    assert len(artifact.role_runs) == 3
    assert {item.role for item in artifact.role_runs} == set(AnalysisRole)
    assert {item.role_view.role for item in artifact.role_runs} == set(AnalysisRole)
    assert {
        item.role_view.analysis_revision_id for item in artifact.role_runs
    } == {artifact.analysis.analysis_revision_id}
    assert {
        item.role_view.night_episode_revision_id for item in artifact.role_runs
    } == {state.source.night_episode_revision_id}
    assert artifact.night_episode_revision_id == (
        artifact.analysis.night_episode_revision_id
    )
    assert len({item.role_view.role_view_id for item in artifact.role_runs}) == 3


def _claim(
    *,
    snapshot_update: dict[str, Any] | None = None,
    metadata_update: dict[str, Any] | None = None,
) -> LeaseClaim:
    snapshot: dict[str, Any] = {
        "schema_version": "workload_authorization_snapshot.v1",
        "workload_principal_id": "sleepagent-worker-test",
        "namespace_id": "replay:pytest",
        "namespace_generation": 3,
        "data_mode": "replay",
        "run_id": "run-1",
        "arm_id": "arm-1",
        "subject_id": "subject-1",
        "purpose": "worker",
        "allowed_handler": "product_agent",
        "authorization_epoch": 7,
        "privacy_epoch": 8,
        "retrieval_policy_epoch": 9,
    }
    snapshot.update(snapshot_update or {})
    metadata = {
        "work_kind": "operation",
        "operation_type": "product_agent",
        "queue_name": "product_agent",
    }
    metadata.update(metadata_update or {})
    return LeaseClaim(
        work_id="product-operation-1",
        operation_id="product-operation-1",
        queue="product_agent",
        namespace_id="replay:pytest",
        data_mode="replay",
        namespace_generation=3,
        run_id="run-1",
        arm_id="arm-1",
        subject_id="subject-1",
        operation_version=3,
        lease_generation=4,
        fencing_token="f" * 64,
        worker_instance="worker-1",
        attempt=2,
        max_attempts=5,
        lease_deadline=NOW + timedelta(minutes=1),
        payload={"opaque": "product work"},
        authorization_snapshot=snapshot,
        metadata=metadata,
    )


class _Store:
    def __init__(self, *, scope: UowScope | None = None) -> None:
        self.scope = scope or _scope()
        self.uow_factory = object()

    def uow_scope_for_claim(self, claim: LeaseClaim) -> UowScope:
        del claim
        return self.scope


class _AdapterProcessor:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[UowScope, ProductAgentLease]] = []

    def process(
        self,
        scope: UowScope,
        lease: ProductAgentLease,
    ) -> ProductAgentCommitResult:
        self.calls.append((scope, lease))
        if self.failure is not None:
            raise self.failure
        return ProductAgentCommitResult(
            operation_id=lease.operation_id,
            product_attempt_id="product-attempt-1",
            analysis_revision_id="analysis-1",
            role_view_ids=("elder-view", "family-view", "doctor-view"),
            analysis_status="ready",
        )


def _context(
    *,
    claim: LeaseClaim | None = None,
    scope: UowScope | None = None,
) -> WorkContext:
    return WorkContext(
        claim=claim or _claim(),
        store=_Store(scope=scope),  # type: ignore[arg-type]
        _lease_lost=threading.Event(),
    )


def test_adapter_accepts_only_an_exact_authorized_product_scope() -> None:
    processor = _AdapterProcessor()
    handler = ProductAgentWorkHandlerAdapter(processor=processor)  # type: ignore[arg-type]

    accepted = handler(_context())
    rejected = handler(
        _context(
            claim=_claim(snapshot_update={"allowed_handler": "fast_path"})
        )
    )

    assert accepted.disposition == WorkDisposition.SUCCEEDED
    assert len(processor.calls) == 1
    accepted_scope, accepted_lease = processor.calls[0]
    assert accepted_scope == _scope()
    assert accepted_lease == _lease()
    assert rejected.disposition == WorkDisposition.TERMINAL
    assert rejected.error_code == "invalid_product_agent_claim_scope"
    assert len(processor.calls) == 1


def test_adapter_success_is_handler_owned_and_returns_all_committed_views() -> None:
    result = ProductAgentWorkHandlerAdapter(
        processor=_AdapterProcessor(),  # type: ignore[arg-type]
    )(_context())

    assert result.disposition == WorkDisposition.SUCCEEDED
    assert result.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
    assert result.result == {
        "operation_id": "product-operation-1",
        "product_attempt_id": "product-attempt-1",
        "analysis_revision_id": "analysis-1",
        "role_view_ids": ["elder-view", "family-view", "doctor-view"],
        "analysis_status": "ready",
    }


@pytest.mark.parametrize(
    ("failure", "expected_disposition", "expected_code", "lease_valid"),
    (
        (
            ProductAgentStaleSource("stale"),
            WorkDisposition.TERMINAL,
            "product_agent_stale_source",
            True,
        ),
        (
            ProductAgentLeaseLost("lost"),
            WorkDisposition.OUTCOME_UNKNOWN,
            "product_agent_lease_lost_reconciliation_required",
            False,
        ),
        (
            ProductAgentConflict("conflict"),
            WorkDisposition.RETRYABLE,
            "product_agent_conflict",
            True,
        ),
        (
            ProductAgentInvariantError("invalid"),
            WorkDisposition.TERMINAL,
            "product_agent_invariant_violation",
            True,
        ),
    ),
)
def test_adapter_maps_product_failures_to_durable_dispositions(
    failure: Exception,
    expected_disposition: WorkDisposition,
    expected_code: str,
    lease_valid: bool,
) -> None:
    context = _context()
    result = ProductAgentWorkHandlerAdapter(
        processor=_AdapterProcessor(failure),  # type: ignore[arg-type]
    )(context)

    assert result.disposition == expected_disposition
    assert result.error_code == expected_code
    assert context.lease_is_valid is lease_valid
    assert result.finalization_mode == WorkFinalizationMode.WORKER_OWNED
