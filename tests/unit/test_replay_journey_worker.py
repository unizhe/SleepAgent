from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from sleepagent.persistence.uow import UowScope
from sleepagent.simulation.journey_worker import (
    EpisodeProgress,
    FastPathProgress,
    PostgresReplayJourneyRepository,
    ProductProgress,
    ProjectionProgress,
    ReplayJourneyTerminalError,
    ReplayJourneyWorkHandler,
)
from sleepagent.worker_runtime import (
    LeaseClaim,
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc
NOW = datetime(2026, 8, 11, tzinfo=UTC)


def _scope() -> UowScope:
    return UowScope(
        namespace_id="replay:normal-one-night",
        namespace_generation=1,
        data_mode="replay",
        run_id="run-1",
        arm_id="arm-1",
        process_role="worker",
        purpose="worker",
        service_principal_id="sleepagent-worker-test",
        subject_id="subject-1",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-1",
    )


def _claim(phase: str) -> LeaseClaim:
    scope = _scope()
    snapshot = {
        "schema_version": "workload_authorization_snapshot.v1",
        "workload_principal_id": scope.service_principal_id,
        "namespace_id": scope.namespace_id,
        "namespace_generation": scope.namespace_generation,
        "data_mode": scope.data_mode,
        "run_id": scope.run_id,
        "arm_id": scope.arm_id,
        "subject_id": scope.subject_id,
        "purpose": scope.purpose,
        "allowed_handler": "replay_journey",
        "authorization_epoch": scope.authorization_epoch,
        "privacy_epoch": scope.privacy_epoch,
        "retrieval_policy_epoch": scope.retrieval_policy_epoch,
    }
    return LeaseClaim(
        work_id="journey-1",
        operation_id="root-1",
        queue="replay_journey",
        namespace_id=scope.namespace_id,
        data_mode=scope.data_mode,
        namespace_generation=scope.namespace_generation,
        run_id=scope.run_id,
        arm_id=scope.arm_id,
        subject_id=scope.subject_id,
        operation_version=3,
        lease_generation=2,
        fencing_token="f" * 64,
        worker_instance=scope.worker_instance or "worker-1",
        attempt=1,
        max_attempts=5,
        lease_deadline=NOW + timedelta(seconds=30),
        payload={
            "root_operation_id": "root-1",
            "manifest_sha256": "a" * 64,
        },
        authorization_snapshot=snapshot,
        metadata={
            "work_kind": "journey",
            "phase": phase,
            "lease_seconds": 30,
        },
    )


class _Store:
    def uow_scope_for_claim(self, claim: LeaseClaim) -> UowScope:
        del claim
        return _scope()


def _context(phase: str) -> WorkContext:
    return WorkContext(
        claim=_claim(phase),
        store=_Store(),  # type: ignore[arg-type]
        _lease_lost=threading.Event(),
    )


def _episode() -> EpisodeProgress:
    return EpisodeProgress(
        night_episode_id="night-1",
        night_episode_revision_id="revision-1",
        episode_local_date="2026-08-11",
        assignment_basis="observed_wake",
        membership_count=495,
        fast_path_operation_id="fast-1",
    )


class _Repository:
    def __init__(self) -> None:
        self.deadline = False
        self.episode: EpisodeProgress | None = _episode()
        self.fast = FastPathProgress(
            status="succeeded",
            fast_path_operation_id="fast-1",
            product_operation_id="product-1",
            quality_assessment_id="quality-1",
            current_risk_id="risk-1",
        )
        self.product = ProductProgress(
            status="succeeded",
            product_operation_id="product-1",
            analysis_revision_id="analysis-1",
        )
        self.projections = ProjectionProgress(
            analysis_revision_id="analysis-1",
            role_projection_ids={
                "elder": "elder-view",
                "family": "family-view",
                "doctor": "doctor-view",
            },
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def deadline_exceeded(self, scope: UowScope, *, journey_id: str) -> bool:
        del scope, journey_id
        return self.deadline

    def episode_progress(self, scope: UowScope) -> EpisodeProgress | None:
        del scope
        return self.episode

    def fast_path_progress(
        self,
        scope: UowScope,
        *,
        episode: EpisodeProgress,
    ) -> FastPathProgress:
        del scope
        assert episode == self.episode
        return self.fast

    def product_progress(
        self,
        scope: UowScope,
        *,
        episode: EpisodeProgress,
        product_operation_id: str,
    ) -> ProductProgress:
        del scope
        assert episode == self.episode
        assert product_operation_id == "product-1"
        return self.product

    def projection_progress(self, scope: UowScope, **values: Any) -> ProjectionProgress:
        del scope
        assert values["analysis_revision_id"] == "analysis-1"
        return self.projections

    def wait(self, scope: UowScope, **values: Any) -> None:
        del scope
        self.calls.append(("wait", values))

    def advance(self, scope: UowScope, **values: Any) -> None:
        del scope
        self.calls.append(("advance", values))

    def succeed(self, scope: UowScope, **values: Any) -> None:
        del scope
        self.calls.append(("succeed", values))


def _handler(repository: _Repository) -> ReplayJourneyWorkHandler:
    return ReplayJourneyWorkHandler(
        uow_factory=object(),  # type: ignore[arg-type]
        cipher=object(),  # type: ignore[arg-type]
        repository=repository,
        ingress=object(),
        generator=object(),
        adapter=object(),
        registry=object(),
    )


def test_waiting_episode_yields_without_consuming_failure_retry() -> None:
    repository = _Repository()
    repository.episode = None

    result = _handler(repository)(_context("waiting_episode"))

    assert result.disposition == WorkDisposition.SUCCEEDED
    assert result.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
    assert repository.calls[0][0] == "wait"
    assert repository.calls[0][1]["phase"] == "waiting_episode"


def test_each_ready_child_advances_exactly_one_durable_phase() -> None:
    cases = (
        ("waiting_episode", "waiting_fast_path", "episode_committed"),
        ("waiting_fast_path", "waiting_product", "fast_path_committed"),
        ("waiting_product", "verifying_views", "product_committed"),
    )
    for phase, next_phase, checkpoint in cases:
        repository = _Repository()

        result = _handler(repository)(_context(phase))

        assert result.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
        assert len(repository.calls) == 1
        name, values = repository.calls[0]
        assert name == "advance"
        assert values["from_phase"] == phase
        assert values["to_phase"] == next_phase
        assert values["checkpoint"] == checkpoint


def test_verifying_views_atomically_succeeds_the_same_root() -> None:
    repository = _Repository()

    result = _handler(repository)(_context("verifying_views"))

    assert result.disposition == WorkDisposition.SUCCEEDED
    assert result.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
    assert repository.calls[0][0] == "succeed"
    root_result = repository.calls[0][1]["result"]
    assert root_result["analysis_revision_id"] == "analysis-1"
    assert root_result["role_projection_ids"] == {
        "elder": "elder-view",
        "family": "family-view",
        "doctor": "doctor-view",
    }
    assert root_result["manifest_sha256"] == "a" * 64


def test_terminal_child_and_deadline_return_stable_worker_owned_codes() -> None:
    repository = _Repository()
    repository.deadline = True
    deadline = _handler(repository)(_context("waiting_episode"))

    repository = _Repository()
    repository.fast = FastPathProgress(
        status="failed",
        fast_path_operation_id="fast-1",
    )
    child = _handler(repository)(_context("waiting_fast_path"))

    assert deadline.disposition == WorkDisposition.TERMINAL
    assert deadline.error_code == "journey_deadline_exceeded"
    assert deadline.finalization_mode == WorkFinalizationMode.WORKER_OWNED
    assert child.disposition == WorkDisposition.TERMINAL
    assert child.error_code == "episode_not_committed"


def test_repository_terminal_code_is_not_collapsed_to_scenario_invalid() -> None:
    class _TerminalRepository(_Repository):
        def episode_progress(self, scope: UowScope) -> EpisodeProgress | None:
            del scope
            raise ReplayJourneyTerminalError("episode_date_conflict")

    result = _handler(_TerminalRepository())(_context("waiting_episode"))

    assert result.disposition == WorkDisposition.TERMINAL
    assert result.error_code == "episode_date_conflict"


def test_postgres_closure_terminal_code_is_preserved() -> None:
    class Diagnostic:
        message_primary = "role_projection_incomplete"

    class ClosureFailure(RuntimeError):
        sqlstate = "P0001"
        diag = Diagnostic()

    class Cursor:
        def execute(self, query: str, params: Any = None) -> None:
            del query, params
            raise ClosureFailure("database diagnostic must not become the code")

        def close(self) -> None:
            pass

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    class Uow:
        connection = Connection()

        def __enter__(self) -> "Uow":
            return self

        def __exit__(self, *args: Any) -> bool:
            return False

    class Factory:
        def begin(self, scope: UowScope) -> Uow:
            del scope
            return Uow()

    repository = PostgresReplayJourneyRepository(Factory())  # type: ignore[arg-type]

    with pytest.raises(ReplayJourneyTerminalError) as failure:
        repository.succeed(
            _scope(),
            context=_context("verifying_views"),
            result={"manifest_sha256": "a" * 64},
        )

    assert failure.value.code == "role_projection_incomplete"


def test_urgent_fast_path_is_zero_model_terminal_without_product_child() -> None:
    class Cursor:
        def __init__(self) -> None:
            self.rows = [
                (
                    "succeeded",
                    {
                        "result": {
                            "quality_assessment_id": "quality-1",
                            "current_risk_id": "risk-1",
                            "urgent": True,
                            "model_invocation_count": 0,
                            "product_agent_operation_id": None,
                        }
                    },
                ),
                (
                    {"data_sufficiency": "sufficient"},
                    {
                        "data_sufficiency": "sufficient",
                        "health_escalation_allowed": True,
                        "risk_state": "reviewed_signal",
                    },
                ),
            ]
            self.executed: list[str] = []

        def execute(self, query: str, params: Any = None) -> None:
            del params
            self.executed.append(query)

        def fetchone(self) -> Any:
            return self.rows.pop(0)

        def close(self) -> None:
            pass

    class Connection:
        def __init__(self, cursor: Cursor) -> None:
            self._cursor = cursor

        def cursor(self) -> Cursor:
            return self._cursor

    class Uow:
        def __init__(self, cursor: Cursor) -> None:
            self.connection = Connection(cursor)

        def __enter__(self) -> "Uow":
            return self

        def __exit__(self, *args: Any) -> bool:
            return False

        def commit(self) -> None:
            pass

    class Factory:
        def __init__(self, cursor: Cursor) -> None:
            self.cursor = cursor

        def begin(self, scope: UowScope) -> Uow:
            del scope
            return Uow(self.cursor)

    cursor = Cursor()
    repository = PostgresReplayJourneyRepository(Factory(cursor))  # type: ignore[arg-type]

    with pytest.raises(ReplayJourneyTerminalError) as failure:
        repository.fast_path_progress(_scope(), episode=_episode())

    assert failure.value.code == "unexpected_urgent_route"
    assert not any("operation_type = 'product_agent'" in query for query in cursor.executed)
