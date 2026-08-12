from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from sleepagent.persistence.uow import UowScope
from sleepagent.stage5_worker import (
    DemoResetWorkHandler,
    RawRetentionWorkHandler,
    SubjectForgetRetentionHandler,
)
from sleepagent.worker_runtime import (
    LeaseClaim,
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc


def _scope() -> UowScope:
    return UowScope(
        namespace_id="replay:normal-one-night",
        data_mode="replay",
        process_role="worker",
        purpose="worker",
        service_principal_id="sleepagent-worker-test",
        namespace_generation=1,
        run_id="run-1",
        arm_id="arm-1",
        subject_id="subject-1",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-1",
    )


def _snapshot() -> dict[str, object]:
    scope = _scope()
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
        "allowed_handler": "retention",
        "authorization_epoch": scope.authorization_epoch,
        "privacy_epoch": scope.privacy_epoch,
        "retrieval_policy_epoch": scope.retrieval_policy_epoch,
    }


def _claim() -> LeaseClaim:
    snapshot = _snapshot()
    return LeaseClaim(
        work_id="retention-job-1",
        queue="retention",
        namespace_id="replay:normal-one-night",
        data_mode="replay",
        namespace_generation=1,
        run_id="run-1",
        arm_id="arm-1",
        subject_id="subject-1",
        operation_version=0,
        lease_generation=3,
        fencing_token="f" * 64,
        worker_instance="worker-1",
        attempt=1,
        max_attempts=8,
        lease_deadline=datetime.now(tz=UTC) + timedelta(seconds=30),
        payload={
            "schema_version": "retention_job.v1",
            "job_kind": "scheduled_expiry",
            "retention_domain": "raw",
            "dek_generation": 1,
            "policy_version": "bounded-replay-raw.v1",
            "authorization_snapshot": snapshot,
        },
        authorization_snapshot=snapshot,
        metadata={
            "work_kind": "retention",
            "retention_domain": "raw",
            "dek_generation": 1,
            "job_kind": "scheduled_expiry",
        },
    )


def _forget_claim() -> LeaseClaim:
    claim = _claim()
    snapshot = _snapshot()
    return claim.model_copy(
        update={
            "payload": {
                "schema_version": "retention_job.v1",
                "job_kind": "subject_forget",
                "retention_domain": "raw",
                "dek_generation": 1,
                "policy_version": "bounded-replay-forget.v1",
                "reset_id": "reset-1",
                "authorization_snapshot": snapshot,
            },
            "metadata": {
                "work_kind": "retention",
                "retention_domain": "raw",
                "dek_generation": 1,
                "job_kind": "subject_forget",
            },
        }
    )


def _reset_claim() -> LeaseClaim:
    snapshot = {**_snapshot(), "allowed_handler": "demo_reset"}
    return _claim().model_copy(
        update={
            "work_id": "reset-operation-1",
            "operation_id": "reset-operation-1",
            "queue": "demo_reset",
            "payload": {
                "schema_version": "backend_operation.v2",
                "command_type": "demo.reset.v1",
                "payload": {"reset_id": "reset-1"},
                "authorization_snapshot": snapshot,
            },
            "authorization_snapshot": snapshot,
            "metadata": {
                "work_kind": "operation",
                "operation_type": "demo_reset",
                "queue_name": "demo_reset",
            },
        }
    )


class _Store:
    def __init__(self, factory: object) -> None:
        self.uow_factory = factory

    def uow_scope_for_claim(self, claim: LeaseClaim) -> UowScope:
        assert claim.namespace_id == _scope().namespace_id
        return _scope()


class _Uow:
    def __init__(self, cursor: object) -> None:
        self.connection = type("Connection", (), {"cursor": lambda _self: cursor})()
        self.committed = False

    def __enter__(self) -> "_Uow":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def commit(self) -> None:
        self.committed = True


class _Factory:
    def __init__(self, cursor: object) -> None:
        self.cursor = cursor
        self.transactions: list[_Uow] = []

    def begin(self, scope: UowScope) -> _Uow:
        assert scope == _scope()
        uow = _Uow(self.cursor)
        self.transactions.append(uow)
        return uow


class _Envelope:
    key_id = "test-kek"
    wrapping_algorithm = "AES-256-GCM-local-envelope.v1"

    def __init__(self) -> None:
        self.destroyed: list[bytes] = []

    def destroy_wrapped_key(self, wrapped: bytes, *, aad: bytes) -> str:
        assert b'"retention_domain":"raw"' in aad
        self.destroyed.append(wrapped)
        return "provider-evidence"


class _Cursor:
    def __init__(
        self,
        *,
        due: bool = True,
        normalization_terminal: bool = True,
        fence_valid: bool = True,
    ) -> None:
        self.due = due
        self.normalization_terminal = normalization_terminal
        self.fence_valid = fence_valid
        self.status = "active"
        self.row: tuple[Any, ...] | None = None
        self.rowcount = 1
        self.executions: list[str] = []

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        self.executions.append(query)
        self.rowcount = 1
        if "JOIN public.backend_retention_deks AS dek" in query and "class" in query:
            self.row = None if not self.fence_valid else (
                "scheduled_expiry",
                self.due,
                self.status,
                b"wrapped-dek",
                "test-kek",
                "AES-256-GCM-local-envelope.v1",
                "a" * 64,
            )
        elif "COALESCE(bool_and(expires_at" in query and "FROM public.backend_retention_bindings" in query:
            self.row = (2, self.due, True)
        elif "JOIN public.sleep_domain_normalization_work" in query:
            self.row = (2, self.normalization_terminal)
        elif "SET status = 'retired'" in query:
            self.status = "retired"
            self.row = None
        elif "FROM public.backend_retention_deks " in query and "FOR UPDATE" in query:
            self.row = (self.status, b"wrapped-dek")
        elif "count(retention_binding_id)" in query:
            self.row = (2, True, True)
        elif "SET wrapped_dek = NULL" in query:
            self.status = "shredded"
            self.row = None
        elif "sleepagent_finalize_retention_job" in query:
            self.row = (True,)
        else:
            self.row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def close(self) -> None:
        return None


def test_raw_expiry_waits_for_deadline_without_destroying_key() -> None:
    cursor = _Cursor(due=False)
    factory = _Factory(cursor)
    envelope = _Envelope()

    result = RawRetentionWorkHandler(envelope=envelope)(
        WorkContext(_claim(), _Store(factory), threading.Event())
    )

    assert result.disposition == WorkDisposition.RETRYABLE
    assert result.error_code == "raw_retention_not_ready"
    assert envelope.destroyed == []
    assert cursor.status == "active"
    assert len(factory.transactions) == 1


def test_raw_expiry_waits_for_terminal_normalization() -> None:
    cursor = _Cursor(normalization_terminal=False)
    factory = _Factory(cursor)
    envelope = _Envelope()

    result = RawRetentionWorkHandler(envelope=envelope)(
        WorkContext(_claim(), _Store(factory), threading.Event())
    )

    assert result.disposition == WorkDisposition.RETRYABLE
    assert result.error_code == "raw_retention_not_ready"
    assert envelope.destroyed == []
    assert cursor.status == "active"


def test_raw_expiry_stale_fence_never_calls_key_provider() -> None:
    cursor = _Cursor(fence_valid=False)
    factory = _Factory(cursor)
    envelope = _Envelope()
    lease_lost = threading.Event()

    result = RawRetentionWorkHandler(envelope=envelope)(
        WorkContext(_claim(), _Store(factory), lease_lost)
    )

    assert result.disposition == WorkDisposition.OUTCOME_UNKNOWN
    assert result.error_code == "retention_lease_lost_reconciliation_required"
    assert lease_lost.is_set()
    assert envelope.destroyed == []


def test_raw_expiry_retires_calls_provider_and_atomically_shreds() -> None:
    cursor = _Cursor()
    factory = _Factory(cursor)
    envelope = _Envelope()
    ids = iter(("receipt-1", "event-1"))

    result = RawRetentionWorkHandler(
        envelope=envelope,
        id_generator=lambda: next(ids),
    )(WorkContext(_claim(), _Store(factory), threading.Event()))

    assert result.disposition == WorkDisposition.SUCCEEDED
    assert result.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
    assert result.result["destroyed_object_count"] == 2
    assert result.result["derived_data_retained"] is True
    assert envelope.destroyed == [b"wrapped-dek"]
    assert cursor.status == "shredded"
    assert len(factory.transactions) == 2
    assert all(transaction.committed for transaction in factory.transactions)
    assert any("backend_shred_receipts" in query for query in cursor.executions)
    assert any("backend_retention_events" in query for query in cursor.executions)
    assert any("sleepagent_finalize_retention_job" in query for query in cursor.executions)


class _ForgetCursor:
    def __init__(self) -> None:
        self.row: tuple[Any, ...] | None = None
        self.executions: list[str] = []

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        self.executions.append(query)
        if "sleepagent_prepare_demo_reset_key" in query:
            self.row = (
                "reset-1",
                1,
                "raw",
                1,
                "test-kek",
                "AES-256-GCM-local-envelope.v1",
                b"wrapped-dek",
                "a" * 64,
                2,
            )
        elif "sleepagent_commit_demo_reset_key" in query:
            self.row = (True,)
        else:
            self.row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def close(self) -> None:
        return None


def test_subject_forget_checkpoints_provider_outside_transactions() -> None:
    cursor = _ForgetCursor()
    factory = _Factory(cursor)
    envelope = _Envelope()
    ids = iter(("prepare-event", "key-receipt", "commit-event"))

    result = SubjectForgetRetentionHandler(
        envelope=envelope,
        id_generator=lambda: next(ids),
    )(WorkContext(_forget_claim(), _Store(factory), threading.Event()))

    assert result.disposition == WorkDisposition.SUCCEEDED
    assert result.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
    assert result.result["reset_id"] == "reset-1"
    assert envelope.destroyed == [b"wrapped-dek"]
    assert len(factory.transactions) == 2
    assert all(transaction.committed for transaction in factory.transactions)


class _ResetCursor:
    def __init__(self, *, succeeded: int) -> None:
        self.succeeded = succeeded
        self.row: tuple[Any, ...] | None = None

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        if "FROM public.backend_demo_resets_v2" in query:
            self.row = (2, 2, self.succeeded, 0)
        elif "sleepagent_wait_demo_reset" in query:
            self.row = (True,)
        elif "sleepagent_complete_demo_reset" in query:
            self.row = (
                {
                    "schema_version": "demo_reset_receipt.v1",
                    "domain_outcomes": [],
                    "reason_code": "replay_subject_forget_completed",
                },
            )
        else:
            self.row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def close(self) -> None:
        return None


def test_reset_root_waits_for_all_key_jobs_then_commits_final_receipt() -> None:
    pending_factory = _Factory(_ResetCursor(succeeded=1))
    pending = DemoResetWorkHandler()(
        WorkContext(_reset_claim(), _Store(pending_factory), threading.Event())
    )
    assert pending.disposition == WorkDisposition.RETRYABLE
    assert pending.error_code == "subject_forget_in_progress"
    assert pending.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
    assert pending_factory.transactions[0].committed is True

    complete_factory = _Factory(_ResetCursor(succeeded=2))
    completed = DemoResetWorkHandler(id_generator=lambda: "reset-receipt-1")(
        WorkContext(_reset_claim(), _Store(complete_factory), threading.Event())
    )
    assert completed.disposition == WorkDisposition.SUCCEEDED
    assert completed.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
    assert completed.result["reset_receipt_id"] == "reset-receipt-1"
