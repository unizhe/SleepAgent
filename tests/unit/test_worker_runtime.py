from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest

from sleepagent.process import DatabaseAttestation, SleepBackendRuntime
from sleepagent.config import (
    DataMode,
    DeploymentMode,
    ModelMode,
    ProcessRole,
    ProviderMode,
    SleepBackendSettings,
)
from sleepagent.persistence.migrations import (
    EXPECTED_MIGRATION_IDENTITIES,
    LATEST_SCHEMA_VERSION,
    MIGRATION_MANIFEST_SHA256,
)
from sleepagent.workers.runtime import (
    DurableWorkerRuntime,
    InvocationDispatcher,
    InvocationKind,
    InvocationRecord,
    InvocationState,
    LeaseClaim,
    OutcomeUnknownError,
    RetryableWorkError,
    WorkDisposition,
    WorkKind,
    WorkResult,
    _final_status,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc


class Pool:
    def open(self) -> None:
        return None

    def close(self) -> None:
        return None


class Store:
    def __init__(self) -> None:
        self.claims: dict[str, list[LeaseClaim]] = {}
        self.current_fence: dict[str, tuple[int, str]] = {}
        self.claim_order: list[str] = []
        self.finalized: list[tuple[LeaseClaim, WorkResult]] = []
        self.heartbeat_result = True
        self.invocations: dict[str, InvocationRecord] = {}
        self.send_started = 0

    def add(self, claim: LeaseClaim) -> None:
        self.claims.setdefault(claim.queue, []).append(claim)
        self.current_fence[claim.work_id] = (
            claim.lease_generation,
            claim.fencing_token,
        )

    def valid(self, claim: LeaseClaim) -> bool:
        return self.current_fence.get(claim.work_id) == (
            claim.lease_generation,
            claim.fencing_token,
        )

    def claim(self, *, queue: str, worker_instance: str, lease_seconds: int):
        del worker_instance, lease_seconds
        self.claim_order.append(queue)
        values = self.claims.get(queue, [])
        return values.pop(0) if values else None

    def heartbeat(self, claim: LeaseClaim, *, lease_seconds: int) -> bool:
        del lease_seconds
        return self.heartbeat_result and self.valid(claim)

    def checkpoint(self, claim, *, checkpoint_type, payload) -> bool:
        del checkpoint_type, payload
        return self.valid(claim)

    def finalize(self, claim: LeaseClaim, result: WorkResult) -> bool:
        if not self.valid(claim):
            return False
        self.finalized.append((claim, result))
        self.current_fence.pop(claim.work_id)
        return True

    def reserve_invocation(
        self,
        claim,
        *,
        invocation_kind,
        invocation_key,
        request_sha256,
    ) -> InvocationRecord:
        existing = self.invocations.get(invocation_key)
        if existing is not None:
            return existing
        value = InvocationRecord(
            invocation_id=f"inv-{len(self.invocations) + 1}",
            invocation_key=invocation_key,
            invocation_kind=invocation_kind,
            work_id=claim.work_id,
            lease_generation=claim.lease_generation,
            request_sha256=request_sha256,
            state=InvocationState.RESERVED,
        )
        self.invocations[invocation_key] = value
        return value

    def mark_invocation_send_started(self, claim, record) -> bool:
        if not self.valid(claim):
            return False
        self.send_started += 1
        self.invocations[record.invocation_key] = record.model_copy(
            update={"state": InvocationState.SEND_STARTED}
        )
        return True

    def finalize_invocation(
        self,
        claim,
        record,
        *,
        state,
        provider_request_id,
        response,
        error_code,
    ) -> bool:
        if not self.valid(claim):
            return False
        self.invocations[record.invocation_key] = record.model_copy(
            update={
                "state": state,
                "provider_request_id": provider_request_id,
                "response": response,
                "error_code": error_code,
            }
        )
        return True


def _claim(queue: str, *, generation: int = 1) -> LeaseClaim:
    return LeaseClaim(
        work_id=f"work-{queue}",
        operation_id=f"operation-{queue}",
        queue=queue,
        namespace_id="replay:test",
        data_mode="replay",
        namespace_generation=1,
        run_id="run-test",
        arm_id="arm-test",
        subject_id="subject-1",
        operation_version=0,
        lease_generation=generation,
        fencing_token=secrets.token_hex(32),
        worker_instance="worker-test",
        attempt=generation,
        max_attempts=5,
        lease_deadline=datetime.now(tz=UTC) + timedelta(seconds=30),
        payload={},
        authorization_snapshot={
            "authorization_epoch": 1,
            "privacy_epoch": 1,
            "retrieval_policy_epoch": 1,
        },
        metadata={
            "work_kind": (
                "delivery" if queue == "delivery" else "operation"
            )
        },
    )


def _runtime(queues: tuple[str, ...]) -> SleepBackendRuntime:
    settings = SleepBackendSettings(
        profile="test-worker",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.WORKER,
        data_mode=DataMode.REPLAY,
        database_dsn="postgresql://worker:secret@postgres/replay_db",
        database_identity="replay_db",
        database_role="sleepagent_worker_replay",
        service_principal_id="sleepagent-worker-test",
        database_scope=DataMode.REPLAY,
        namespace_prefixes=("replay:test",),
        worker_queues=queues,
        provider_mode=ProviderMode.FAKE,
        model_mode=ModelMode.DETERMINISTIC,
        signing_key_ref="test:signing",
        encryption_key_ref="test:encryption",
    )
    return SleepBackendRuntime(
        settings,
        pool=Pool(),
        uow_factory=object(),
        attestor=lambda: DatabaseAttestation(
            database_identity="replay_db",
            database_role="sleepagent_worker_replay",
            schema_version=LATEST_SCHEMA_VERSION,
            migrations_clean=True,
            migration_manifest_sha256=MIGRATION_MANIFEST_SHA256,
            applied_migration_identities=EXPECTED_MIGRATION_IDENTITIES,
        ),
        worker_handlers={queue: object() for queue in queues},
    )


def test_journey_retry_budget_exhaustion_uses_supported_terminal_state() -> None:
    claim = _claim("replay_journey").model_copy(
        update={
            "attempt": 5,
            "max_attempts": 5,
            "metadata": {"work_kind": "journey"},
        }
    )

    assert _final_status(
        WorkKind.JOURNEY,
        claim,
        WorkResult(
            disposition=WorkDisposition.RETRYABLE,
            error_code="scenario_contract_invalid",
        ),
    ) == "failed"


def test_fast_path_is_claimed_before_model_slow_path() -> None:
    store = Store()
    store.add(_claim("product_agent"))
    store.add(_claim("fast_path"))
    calls: list[str] = []
    worker = DurableWorkerRuntime(
        _runtime(("product_agent", "fast_path")),
        store=store,
        handlers={
            "product_agent": lambda context: WorkResult(
                disposition=WorkDisposition.SUCCEEDED
            ),
            "fast_path": lambda context: (
                calls.append(context.claim.queue)
                or WorkResult(disposition=WorkDisposition.SUCCEEDED)
            ),
        },
        lease_seconds=3,
        heartbeat_interval_seconds=0.5,
    )

    assert worker.run_once() is True
    assert calls == ["fast_path"]
    assert store.claim_order == ["fast_path"]


def test_first_slice_leaf_queues_are_claimed_before_root_journey_polling() -> None:
    store = Store()
    for queue in ("replay_journey", "product_agent"):
        store.add(_claim(queue))
    calls: list[str] = []
    worker = DurableWorkerRuntime(
        _runtime(("replay_journey", "product_agent")),
        store=store,
        handlers={
            queue: (
                lambda context: calls.append(context.claim.queue)
                or WorkResult(disposition=WorkDisposition.SUCCEEDED)
            )
            for queue in ("replay_journey", "product_agent")
        },
        lease_seconds=3,
        heartbeat_interval_seconds=0.5,
    )

    assert worker.run_once() is True
    assert calls == ["product_agent"]
    assert store.claim_order == ["product_agent"]


def test_stale_worker_cannot_finalize_after_reclaim() -> None:
    store = Store()
    stale = _claim("product_agent", generation=1)
    current = stale.model_copy(
        update={
            "lease_generation": 2,
            "fencing_token": secrets.token_hex(32),
            "attempt": 2,
        }
    )
    store.add(stale)
    store.current_fence[stale.work_id] = (
        current.lease_generation,
        current.fencing_token,
    )
    worker = DurableWorkerRuntime(
        _runtime(("product_agent",)),
        store=store,
        handlers={
            "product_agent": lambda context: WorkResult(
                disposition=WorkDisposition.SUCCEEDED,
                result={"semantic_result": "stale"},
            )
        },
        lease_seconds=3,
        heartbeat_interval_seconds=0.5,
    )

    worker.run_once()

    assert store.finalized == []


def test_heartbeat_loss_prevents_final_commit() -> None:
    store = Store()
    store.add(_claim("product_agent"))
    store.heartbeat_result = False

    def slow_handler(context) -> WorkResult:
        time.sleep(0.08)
        return WorkResult(disposition=WorkDisposition.SUCCEEDED)

    worker = DurableWorkerRuntime(
        _runtime(("product_agent",)),
        store=store,
        handlers={"product_agent": slow_handler},
        lease_seconds=3,
        heartbeat_interval_seconds=0.02,
    )

    worker.run_once()

    assert store.finalized == []


def test_send_started_unknown_is_never_blindly_resent() -> None:
    store = Store()
    claim = _claim("delivery")
    store.add(claim)
    calls = 0

    def ambiguous_sender():
        nonlocal calls
        calls += 1
        raise ConnectionError("connection lost after write")

    first = InvocationDispatcher(
        store=store,
        claim=claim,
        lease_lost=threading.Event(),
    )
    with pytest.raises(OutcomeUnknownError):
        first.dispatch(
            invocation_key="destination:semantic-effect-1",
            request={"opaque": "payload"},
            sender=ambiguous_sender,
        )

    second = InvocationDispatcher(
        store=store,
        claim=claim,
        lease_lost=threading.Event(),
    )
    with pytest.raises(OutcomeUnknownError, match="reconciliation"):
        second.dispatch(
            invocation_key="destination:semantic-effect-1",
            request={"opaque": "payload"},
            sender=ambiguous_sender,
        )

    assert calls == 1
    assert store.send_started == 1
    assert (
        store.invocations["destination:semantic-effect-1"].state
        == InvocationState.OUTCOME_UNKNOWN
    )
    assert (
        store.invocations["destination:semantic-effect-1"].invocation_kind
        == InvocationKind.EXTERNAL_SINK
    )


def test_committed_invocation_response_is_reused_without_network_call() -> None:
    store = Store()
    claim = _claim("delivery")
    store.add(claim)
    dispatcher = InvocationDispatcher(
        store=store,
        claim=claim,
        lease_lost=threading.Event(),
    )
    calls = 0

    def sender():
        nonlocal calls
        calls += 1
        return {"delivered": True}, "provider-request-1"

    first = dispatcher.dispatch(
        invocation_key="destination:semantic-effect-2",
        request={"opaque": "payload"},
        sender=sender,
    )
    second = dispatcher.dispatch(
        invocation_key="destination:semantic-effect-2",
        request={"opaque": "payload"},
        sender=sender,
    )

    assert first == second == {"delivered": True}
    assert calls == 1


def test_process_crash_leaves_lease_for_expiry_instead_of_finalizing() -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    store = Store()
    claim = _claim("fast_path")
    store.add(claim)

    def crash_handler(context) -> WorkResult:
        del context
        raise SimulatedProcessCrash()

    worker = DurableWorkerRuntime(
        _runtime(("fast_path",)),
        store=store,
        handlers={"fast_path": crash_handler},
        lease_seconds=3,
        heartbeat_interval_seconds=0.5,
    )

    with pytest.raises(SimulatedProcessCrash):
        worker.run_once()

    assert store.finalized == []
    assert store.valid(claim) is True
    assert worker.in_flight == 0


def test_process_crash_after_dispatch_permit_stays_send_started() -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    store = Store()
    claim = _claim("delivery")
    store.add(claim)
    dispatcher = InvocationDispatcher(
        store=store,
        claim=claim,
        lease_lost=threading.Event(),
    )

    def crash_after_permit():
        raise SimulatedProcessCrash()

    with pytest.raises(SimulatedProcessCrash):
        dispatcher.dispatch(
            invocation_key="destination:semantic-effect-crash",
            request={"opaque": "payload"},
            sender=crash_after_permit,
        )

    assert (
        store.invocations["destination:semantic-effect-crash"].state
        == InvocationState.SEND_STARTED
    )


def test_retry_reclaim_runs_no_model_effect_once() -> None:
    store = Store()
    first = _claim("fast_path", generation=1)
    store.add(first)
    semantic_effects: set[str] = set()

    def no_model_handler(context) -> WorkResult:
        if context.claim.attempt == 1:
            raise RetryableWorkError("database_temporarily_unavailable")
        semantic_effects.add("fast-path:subject-1")
        return WorkResult(
            disposition=WorkDisposition.SUCCEEDED,
            result={"deterministic": True},
        )

    worker = DurableWorkerRuntime(
        _runtime(("fast_path",)),
        store=store,
        handlers={"fast_path": no_model_handler},
        lease_seconds=3,
        heartbeat_interval_seconds=0.5,
    )

    assert worker.run_once() is True
    assert store.finalized[-1][1].disposition == WorkDisposition.RETRYABLE

    reclaimed = _claim("fast_path", generation=2).model_copy(
        update={
            "work_id": first.work_id,
            "operation_id": first.operation_id,
        }
    )
    store.add(reclaimed)
    assert worker.run_once() is True

    assert store.finalized[-1][1].disposition == WorkDisposition.SUCCEEDED
    assert semantic_effects == {"fast-path:subject-1"}
