from __future__ import annotations

import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from sleepagent.backend_runtime import DatabaseAttestation, SleepBackendRuntime
from sleepagent.backend_settings import (
    DataMode,
    DeploymentMode,
    ModelMode,
    ProcessRole,
    ProviderMode,
    SleepBackendSettings,
)
from sleepagent.persistence.uow import UowScope
from sleepagent.sleep_domain.postgres_slice import (
    FastPathCommitResult,
    NormalizationResult,
    SleepSliceConflict,
    SleepSliceInvariantError,
    SleepSliceLeaseLost,
    SleepSliceStaleRevision,
)
from sleepagent.sleep_domain.worker_adapters import (
    B3WorkerCompositionError,
    FastPathWorkHandlerAdapter,
    NormalizationWorkHandlerAdapter,
    build_b3_worker_handlers,
)
from sleepagent.product_runtime.postgres_worker import (
    ProductAgentWorkHandlerAdapter,
)
from sleepagent.worker_runtime import (
    DurableWorkStoreError,
    DurableWorkerRuntime,
    LeaseClaim,
    ReplayNoModelHandler,
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
    WorkResult,
    _cli_handlers,
    _validate_replay_no_model_profile,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc


class _Pool:
    def open(self) -> None:
        return None

    def close(self) -> None:
        return None


def _settings(
    queues: tuple[str, ...] = ("ingestion", "fast_path"),
    *,
    deployment_mode: DeploymentMode = DeploymentMode.TEST,
    data_mode: DataMode = DataMode.REPLAY,
) -> SleepBackendSettings:
    return SleepBackendSettings(
        profile="worker-adapter-test",
        deployment_mode=deployment_mode,
        process_role=ProcessRole.WORKER,
        data_mode=data_mode,
        database_dsn="postgresql://worker:secret@postgres/sleepagent_test",
        database_identity="sleepagent_test",
        database_role="sleepagent_worker_test",
        service_principal_id="sleepagent-worker-test",
        service_credential_ref="test:worker-credential",
        database_scope=data_mode,
        namespace_prefixes=(f"{data_mode.value}:pytest",),
        worker_queues=queues,
        provider_mode=ProviderMode.FAKE,
        model_mode=ModelMode.DETERMINISTIC,
        signing_key_ref="test:signing",
        encryption_key_ref="test:encryption",
    )


def _claim(
    queue: str,
    *,
    data_mode: str = "replay",
    snapshot_update: dict[str, Any] | None = None,
    claim_update: dict[str, Any] | None = None,
) -> LeaseClaim:
    replay = data_mode == "replay"
    operation = queue == "fast_path"
    namespace_id = f"{data_mode}:pytest"
    operation_id = "operation-fast-path" if operation else None
    allowed_handler = "fast_path" if operation else "normalization"
    snapshot: dict[str, Any] = {
        "schema_version": "workload_authorization_snapshot.v1",
        "workload_principal_id": "sleepagent-worker-test",
        "namespace_id": namespace_id,
        "namespace_generation": 3,
        "data_mode": data_mode,
        "run_id": "run-1" if replay else None,
        "arm_id": "arm-1" if replay else None,
        "subject_id": "subject-1",
        "purpose": "worker",
        "allowed_handler": allowed_handler,
        "authorization_epoch": 7,
        "privacy_epoch": 8,
        "retrieval_policy_epoch": 9,
    }
    snapshot.update(snapshot_update or {})
    values: dict[str, Any] = {
        "work_id": operation_id or "normalization-work-1",
        "operation_id": operation_id,
        "queue": queue,
        "namespace_id": namespace_id,
        "data_mode": data_mode,
        "namespace_generation": 3,
        "run_id": "run-1" if replay else None,
        "arm_id": "arm-1" if replay else None,
        "subject_id": "subject-1",
        "operation_version": 4 if operation else 0,
        "lease_generation": 2,
        "fencing_token": secrets.token_hex(32),
        "worker_instance": "worker-1",
        "attempt": 2,
        "max_attempts": 5,
        "lease_deadline": datetime.now(tz=UTC) + timedelta(seconds=30),
        "payload": {"opaque": "work"},
        "authorization_snapshot": snapshot,
        "metadata": (
            {
                "work_kind": "operation",
                "operation_type": "fast_path",
                "queue_name": "fast_path",
            }
            if operation
            else {"work_kind": "normalization"}
        ),
    }
    values.update(claim_update or {})
    return LeaseClaim(**values)


def _scope(claim: LeaseClaim) -> UowScope:
    snapshot = claim.authorization_snapshot
    return UowScope(
        namespace_id=claim.namespace_id,
        data_mode=claim.data_mode,  # type: ignore[arg-type]
        process_role="worker",
        purpose="worker",
        service_principal_id="sleepagent-worker-test",
        namespace_generation=claim.namespace_generation,
        subject_id=claim.subject_id,
        run_id=claim.run_id,
        arm_id=claim.arm_id,
        authorization_epoch=int(snapshot["authorization_epoch"]),
        privacy_epoch=int(snapshot["privacy_epoch"]),
        retrieval_policy_epoch=int(snapshot["retrieval_policy_epoch"]),
        worker_instance=claim.worker_instance,
    )


class _Store:
    def __init__(self, claim: LeaseClaim | None = None) -> None:
        self.uow_factory = object()
        self.pending = [] if claim is None else [claim]
        self.finalized: list[tuple[LeaseClaim, WorkResult]] = []
        self.handler_commits = 0
        self.scope_calls = 0

    def claim(self, *, queue: str, worker_instance: str, lease_seconds: int):
        del worker_instance, lease_seconds
        if self.pending and self.pending[0].queue == queue:
            return self.pending.pop(0)
        return None

    def heartbeat(self, claim: LeaseClaim, *, lease_seconds: int) -> bool:
        del claim, lease_seconds
        return True

    def checkpoint(self, claim: LeaseClaim, *, checkpoint_type: str, payload: Any):
        del claim, checkpoint_type, payload
        return True

    def finalize(self, claim: LeaseClaim, result: WorkResult) -> bool:
        self.finalized.append((claim, result))
        return True

    def uow_scope_for_claim(self, claim: LeaseClaim) -> UowScope:
        self.scope_calls += 1
        return _scope(claim)


class _NormalizationProcessor:
    def __init__(self, store: _Store, failure: Exception | None = None) -> None:
        self.store = store
        self.failure = failure
        self.calls: list[tuple[UowScope, Any]] = []

    def process(self, scope: UowScope, lease: Any) -> NormalizationResult:
        self.calls.append((scope, lease))
        if self.failure is not None:
            raise self.failure
        self.store.handler_commits += 1
        return NormalizationResult(
            observation_id="observation-1",
            night_episode_id="episode-1",
            night_episode_revision_id="revision-1",
            fast_path_operation_id="operation-fast-path",
            date_state="finalized",
        )


class _FastPathProcessor:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[UowScope, Any]] = []

    def process(self, scope: UowScope, lease: Any) -> FastPathCommitResult:
        self.calls.append((scope, lease))
        if self.failure is not None:
            raise self.failure
        return FastPathCommitResult(
            operation_id="operation-fast-path",
            current_risk_id="risk-1",
            urgent=False,
            product_agent_operation_id="product-operation-1",
        )


def _runtime(settings: SleepBackendSettings) -> SleepBackendRuntime:
    return SleepBackendRuntime(
        settings,
        pool=_Pool(),
        uow_factory=object(),
        attestor=lambda: DatabaseAttestation(
            database_identity=settings.database_identity,
            database_role=settings.database_role,
            schema_version=1,
            migrations_clean=True,
        ),
        worker_handlers={queue: object() for queue in settings.worker_queues},
    )


def test_normalization_atomic_handoff_skips_outer_worker_finalize() -> None:
    claim = _claim("ingestion")
    store = _Store(claim)
    processor = _NormalizationProcessor(store)
    adapter = NormalizationWorkHandlerAdapter(processor=processor)
    settings = _settings(("ingestion",))
    worker = DurableWorkerRuntime(
        _runtime(settings),
        store=store,
        handlers={"ingestion": adapter},
        lease_seconds=3,
        heartbeat_interval_seconds=0.5,
    )

    assert worker.run_once() is True

    assert store.handler_commits == 1
    assert store.finalized == []
    scope, lease = processor.calls[0]
    assert scope.namespace_id == claim.namespace_id
    assert scope.namespace_generation == claim.namespace_generation
    assert scope.run_id == claim.run_id
    assert scope.arm_id == claim.arm_id
    assert scope.subject_id == claim.subject_id
    assert scope.authorization_epoch == 7
    assert scope.privacy_epoch == 8
    assert scope.retrieval_policy_epoch == 9
    assert scope.worker_instance == claim.worker_instance
    assert scope.actor_id is None
    assert scope.actor_role is None
    assert lease.work_id == claim.work_id
    assert lease.lease_generation == claim.lease_generation
    assert lease.fencing_token == claim.fencing_token
    assert lease.worker_instance == claim.worker_instance


def test_fast_path_adapter_returns_handler_owned_result_and_exact_lease() -> None:
    claim = _claim("fast_path")
    store = _Store()
    processor = _FastPathProcessor()
    result = FastPathWorkHandlerAdapter(processor=processor)(
        WorkContext(claim, store, threading.Event())
    )

    assert result.disposition == WorkDisposition.SUCCEEDED
    assert result.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
    assert result.result == {
        "operation_id": "operation-fast-path",
        "current_risk_id": "risk-1",
        "urgent": False,
        "product_agent_operation_id": "product-operation-1",
        "model_invocation_count": 0,
    }
    scope, lease = processor.calls[0]
    assert scope.service_principal_id == "sleepagent-worker-test"
    assert scope.purpose == "worker"
    assert lease.operation_id == claim.operation_id == claim.work_id
    assert lease.lease_generation == claim.lease_generation
    assert lease.fencing_token == claim.fencing_token
    assert lease.worker_instance == claim.worker_instance


@pytest.mark.parametrize(
    ("failure", "disposition", "error_code", "lease_lost"),
    [
        (
            SleepSliceStaleRevision("stale"),
            WorkDisposition.TERMINAL,
            "sleep_slice_stale_revision",
            False,
        ),
        (
            SleepSliceInvariantError("invalid"),
            WorkDisposition.TERMINAL,
            "sleep_slice_invariant_violation",
            False,
        ),
        (
            SleepSliceConflict("concurrent"),
            WorkDisposition.RETRYABLE,
            "sleep_slice_conflict",
            False,
        ),
        (
            SleepSliceLeaseLost("lost"),
            WorkDisposition.OUTCOME_UNKNOWN,
            "sleep_slice_lease_lost_reconciliation_required",
            True,
        ),
    ],
)
def test_domain_failures_have_explicit_worker_dispositions(
    failure: Exception,
    disposition: WorkDisposition,
    error_code: str,
    lease_lost: bool,
) -> None:
    claim = _claim("fast_path")
    lost = threading.Event()
    result = FastPathWorkHandlerAdapter(
        processor=_FastPathProcessor(failure)
    )(WorkContext(claim, _Store(), lost))

    assert result.disposition == disposition
    assert result.error_code == error_code
    assert result.finalization_mode == WorkFinalizationMode.WORKER_OWNED
    assert lost.is_set() is lease_lost


def test_unknown_processor_failure_is_not_swallowed_by_adapter() -> None:
    adapter = FastPathWorkHandlerAdapter(
        processor=_FastPathProcessor(ConnectionError("database acknowledgement lost"))
    )

    with pytest.raises(ConnectionError, match="acknowledgement"):
        adapter(WorkContext(_claim("fast_path"), _Store(), threading.Event()))


@pytest.mark.parametrize(
    "claim",
    [
        _claim(
            "ingestion",
            snapshot_update={"allowed_handler": "fast_path"},
        ),
        _claim(
            "ingestion",
            snapshot_update={"privacy_epoch": True},
        ),
    ],
)
def test_inexact_workload_snapshot_is_terminal_before_normalization(
    claim: LeaseClaim,
) -> None:
    store = _Store()
    processor = _NormalizationProcessor(store)

    result = NormalizationWorkHandlerAdapter(processor=processor)(
        WorkContext(claim, store, threading.Event())
    )

    assert result.disposition == WorkDisposition.TERMINAL
    assert result.error_code == "invalid_normalization_claim_scope"
    assert processor.calls == []


def test_fast_path_claim_identity_must_match_operation_and_queue_metadata() -> None:
    claim = _claim(
        "fast_path",
        claim_update={"operation_id": "different-operation"},
    )
    processor = _FastPathProcessor()

    result = FastPathWorkHandlerAdapter(processor=processor)(
        WorkContext(claim, _Store(), threading.Event())
    )

    assert result.disposition == WorkDisposition.TERMINAL
    assert result.error_code == "invalid_fast_path_claim_scope"
    assert processor.calls == []


def test_live_fast_path_scope_preserves_absent_replay_identifiers() -> None:
    claim = _claim("fast_path", data_mode="live")
    processor = _FastPathProcessor()

    result = FastPathWorkHandlerAdapter(processor=processor)(
        WorkContext(claim, _Store(), threading.Event())
    )

    assert result.disposition == WorkDisposition.SUCCEEDED
    scope, _lease = processor.calls[0]
    assert scope.data_mode == "live"
    assert scope.run_id is None
    assert scope.arm_id is None


def test_handler_owned_mode_rejects_non_success_results() -> None:
    with pytest.raises(ValidationError, match="committed successful"):
        WorkResult(
            disposition=WorkDisposition.TERMINAL,
            finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
        )


def test_cli_registry_uses_real_b3_and_product_handlers() -> None:
    handlers = _cli_handlers(
        _settings(("ingestion", "fast_path", "product_agent"))
    )

    assert isinstance(handlers["ingestion"], NormalizationWorkHandlerAdapter)
    assert isinstance(handlers["fast_path"], FastPathWorkHandlerAdapter)
    assert isinstance(handlers["product_agent"], ProductAgentWorkHandlerAdapter)


def test_development_registry_fails_closed_for_unimplemented_queue() -> None:
    settings = _settings(
        ("ingestion", "fast_path", "induction"),
        deployment_mode=DeploymentMode.DEVELOPMENT,
    )

    with pytest.raises(DurableWorkStoreError, match="induction"):
        _cli_handlers(settings)
    with pytest.raises(DurableWorkStoreError, match="restricted"):
        _validate_replay_no_model_profile(
            settings,
            {queue: ReplayNoModelHandler(queue) for queue in settings.worker_queues},
        )


def test_replay_normalizer_is_not_composed_for_live_ingestion() -> None:
    settings = _settings(
        ("ingestion",),
        deployment_mode=DeploymentMode.DEVELOPMENT,
        data_mode=DataMode.LIVE,
    )

    with pytest.raises(B3WorkerCompositionError, match="replay ingress only"):
        build_b3_worker_handlers(settings)


def test_raw_ingress_handler_is_not_part_of_b3_worker_registry() -> None:
    handlers = build_b3_worker_handlers(_settings(("raw_ingress",)))

    assert handlers == {}
