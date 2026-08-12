from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from sleepagent.backend_settings import (
    DataMode,
    DeploymentMode,
    ModelMode,
    ProcessRole,
    ProviderMode,
    SleepBackendSettings,
)
from sleepagent.persistence.uow import UowScope
from sleepagent.sleep_domain.postgres_slice import RawPayloadCipher
from sleepagent.stage3_worker import (
    EpisodeDateReconciliationHandler,
    Stage3AdvanceHandler,
    build_stage3_worker_handlers,
)
from sleepagent.worker_runtime import (
    LeaseClaim,
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc


class Store:
    def __init__(self, uow_factory: object | None = None) -> None:
        if uow_factory is not None:
            self.uow_factory = uow_factory

    def uow_scope_for_claim(self, claim: LeaseClaim) -> UowScope:
        return UowScope(
            namespace_id=claim.namespace_id,
            namespace_generation=claim.namespace_generation,
            data_mode=claim.data_mode,
            run_id=claim.run_id,
            arm_id=claim.arm_id,
            process_role="worker",
            purpose="worker",
            service_principal_id="sleepagent-worker-test",
            subject_id=claim.subject_id,
            authorization_epoch=1,
            privacy_epoch=1,
            retrieval_policy_epoch=1,
            worker_instance=claim.worker_instance,
        )


def _claim(*, queue: str = "demo_advance", seconds: object = 60) -> LeaseClaim:
    return LeaseClaim(
        work_id="advance-1",
        operation_id="advance-1",
        queue=queue,
        namespace_id="replay:worsening-vital-trend",
        data_mode="replay",
        namespace_generation=1,
        run_id="run-1",
        arm_id="arm-1",
        subject_id="subject-1",
        operation_version=0,
        lease_generation=1,
        fencing_token="f" * 64,
        worker_instance="worker-1",
        attempt=1,
        max_attempts=5,
        lease_deadline=datetime.now(tz=UTC) + timedelta(seconds=30),
        payload={"payload": {"seconds": seconds}},
        authorization_snapshot={
            "schema_version": "workload_authorization_snapshot.v1",
            "workload_principal_id": "sleepagent-worker-test",
            "namespace_id": "replay:worsening-vital-trend",
            "namespace_generation": 1,
            "data_mode": "replay",
            "run_id": "run-1",
            "arm_id": "arm-1",
            "subject_id": "subject-1",
            "purpose": "worker",
            "allowed_handler": "demo_advance",
            "authorization_epoch": 1,
            "privacy_epoch": 1,
            "retrieval_policy_epoch": 1,
        },
        metadata={
            "work_kind": "operation",
            "operation_type": "demo_advance",
        },
    )


def _reconciliation_claim() -> LeaseClaim:
    snapshot = {
        "schema_version": "workload_authorization_snapshot.v1",
        "workload_principal_id": "sleepagent-worker-test",
        "namespace_id": "replay:worsening-vital-trend",
        "namespace_generation": 1,
        "data_mode": "replay",
        "run_id": "run-1",
        "arm_id": "arm-1",
        "subject_id": "subject-1",
        "purpose": "worker",
        "allowed_handler": "reconciliation",
        "authorization_epoch": 1,
        "privacy_epoch": 1,
        "retrieval_policy_epoch": 1,
    }
    return LeaseClaim(
        work_id="reconcile-operation-1",
        operation_id="reconcile-operation-1",
        queue="reconciliation",
        namespace_id="replay:worsening-vital-trend",
        data_mode="replay",
        namespace_generation=1,
        run_id="run-1",
        arm_id="arm-1",
        subject_id="subject-1",
        operation_version=3,
        lease_generation=2,
        fencing_token="r" * 64,
        worker_instance="worker-1",
        attempt=1,
        max_attempts=5,
        lease_deadline=datetime.now(tz=UTC) + timedelta(seconds=30),
        payload={
            "schema_version": "episode_date_reconciliation_operation.v1",
            "reconciliation_id": "reconciliation-1",
            "candidate_night_episode_id": "candidate-episode-1",
            "candidate_revision_id": "candidate-revision-2",
            "conflicting_night_episode_id": "canonical-episode-1",
            "proposed_episode_local_date": "2026-01-02",
            "resolution_policy": "preserve_existing_canonical_owner.v1",
            "authorization_snapshot": snapshot,
        },
        authorization_snapshot=snapshot,
        metadata={
            "work_kind": "operation",
            "operation_type": "episode_date_reconciliation",
            "queue_name": "reconciliation",
        },
    )


class _ReconciliationCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[Any, ...]]] = []
        self.row: tuple[Any, ...] | None = None
        self.rowcount = 0

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        self.executions.append((query, params))
        self.rowcount = 1
        if "FROM public.sleep_domain_operations AS operation" in query:
            self.row = (
                "reconciliation-1",
                "candidate-episode-1",
                "canonical-episode-1",
                "candidate-revision-2",
                date(2026, 1, 2),
                "reconciliation_required",
                "conflict",
                True,
                date(2026, 1, 2),
                "candidate-revision-1",
                "canonical-revision-3",
                "finalized",
                False,
                date(2026, 1, 2),
                "a" * 64,
            )
        elif "SELECT count(*), min(night_episode_id)" in query:
            self.row = (1, "canonical-episode-1")
        elif "RETURNING cas_version" in query:
            self.row = (4,)
        else:
            self.row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def close(self) -> None:
        return None


class _ReconciliationConnection:
    def __init__(self) -> None:
        self.cursor_value = _ReconciliationCursor()

    def cursor(self) -> _ReconciliationCursor:
        return self.cursor_value


class _ReconciliationUow:
    def __init__(self, connection: _ReconciliationConnection) -> None:
        self.connection = connection
        self.committed = False

    def __enter__(self) -> "_ReconciliationUow":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def commit(self) -> None:
        self.committed = True


class _ReconciliationUowFactory:
    def __init__(self) -> None:
        self.connection = _ReconciliationConnection()
        self.uow = _ReconciliationUow(self.connection)

    def begin(self, scope: UowScope) -> _ReconciliationUow:
        assert scope.subject_id == "subject-1"
        return self.uow


def test_invalid_advance_claim_fails_before_database_access() -> None:
    handler = Stage3AdvanceHandler(
        cipher=RawPayloadCipher(b"k" * 32, key_id="test:raw")
    )
    claim = _claim(queue="product_agent")

    result = handler(WorkContext(claim, Store(), threading.Event()))

    assert result.disposition == WorkDisposition.TERMINAL
    assert result.error_code == "demo_advance_contract_invalid"


def test_stage3_composition_registers_only_the_enabled_real_queue() -> None:
    settings = SleepBackendSettings(
        profile="test-stage3-worker",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.WORKER,
        data_mode=DataMode.REPLAY,
        database_dsn="postgresql://worker:secret@postgres/replay_db",
        database_identity="replay_db",
        database_role="sleepagent_worker_replay",
        service_principal_id="sleepagent-worker-test",
        database_scope=DataMode.REPLAY,
        namespace_prefixes=("replay:",),
        worker_queues=("demo_advance",),
        provider_mode=ProviderMode.FAKE,
        model_mode=ModelMode.DETERMINISTIC,
        signing_key_ref="test:signing",
        encryption_key_ref="test:encryption",
    )

    handlers = build_stage3_worker_handlers(settings)

    assert set(handlers) == {"demo_advance"}
    assert isinstance(handlers["demo_advance"], Stage3AdvanceHandler)

    disabled = settings.model_copy(update={"worker_queues": ("ingestion",)})
    assert build_stage3_worker_handlers(disabled) == {}

    reconciliation = settings.model_copy(
        update={"worker_queues": ("reconciliation",)}
    )
    assert build_stage3_worker_handlers(reconciliation) == {}


def test_date_reconciliation_preserves_owner_and_atomically_rejects_candidate() -> None:
    factory = _ReconciliationUowFactory()
    context = WorkContext(
        _reconciliation_claim(),
        Store(factory),
        threading.Event(),
    )

    result = EpisodeDateReconciliationHandler()(context)

    assert result.disposition == WorkDisposition.SUCCEEDED
    assert result.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
    assert result.result["decision"] == "reject_candidate"
    assert result.result["candidate_promoted"] is False
    assert result.result["product_work_enqueued"] is False
    assert result.result["canonical_night_episode_revision_id"] == (
        "canonical-revision-3"
    )
    assert factory.uow.committed is True
    statements = [query for query, _params in factory.connection.cursor_value.executions]
    assert any(
        "UPDATE public.backend_episode_date_reconciliation" in query
        and "status = 'rejected'" in query
        for query in statements
    )
    assert any(
        "UPDATE public.sleep_domain_operations" in query
        and "status = 'succeeded'" in query
        for query in statements
    )
    assert not any("product_agent" in query for query in statements)


def test_date_reconciliation_rejects_drift_before_database_access() -> None:
    claim = _reconciliation_claim().model_copy(
        update={"queue": "product_agent"}
    )
    factory = _ReconciliationUowFactory()

    result = EpisodeDateReconciliationHandler()(
        WorkContext(claim, Store(factory), threading.Event())
    )

    assert result.disposition == WorkDisposition.TERMINAL
    assert result.error_code == "episode_date_reconciliation_invariant_violation"
    assert factory.connection.cursor_value.executions == []
