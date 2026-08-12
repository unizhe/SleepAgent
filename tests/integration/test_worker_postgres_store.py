from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import pytest

from sleepagent.backend_settings import (
    DataMode,
    DeploymentMode,
    ModelMode,
    ProcessRole,
    ProviderMode,
    SleepBackendSettings,
)
from sleepagent.persistence.uow import UowScope, WorkerClaimScope
from sleepagent.worker_runtime import (
    InvocationDispatcher,
    InvocationKind,
    InvocationRecord,
    InvocationState,
    LeaseClaim,
    OutcomeUnknownError,
    PostgresDurableWorkStore,
    WorkDisposition,
    WorkResult,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc


@dataclass(frozen=True)
class Step:
    contains: str
    row: Any = None


class ScriptedCursor:
    def __init__(self, factory: "ScriptedUowFactory", scope: object) -> None:
        self.factory = factory
        self.scope = scope
        self.row: Any = None
        self.closed = False

    def execute(self, query: str, params: Any = None) -> None:
        if not self.factory.steps:
            raise AssertionError(f"unexpected SQL: {query}")
        step = self.factory.steps.pop(0)
        normalized = " ".join(query.split())
        assert step.contains in normalized
        self.factory.calls.append((self.scope, normalized, params))
        self.row = step.row

    def fetchone(self) -> Any:
        return self.row

    def close(self) -> None:
        self.closed = True


class ScriptedUow:
    def __init__(self, factory: "ScriptedUowFactory", scope: object) -> None:
        self.factory = factory
        self.scope = scope
        self.connection = self
        self.committed = False

    def __enter__(self) -> "ScriptedUow":
        self.factory.scopes.append(self.scope)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        del exc_type, exc, traceback
        return False

    def cursor(self) -> ScriptedCursor:
        return ScriptedCursor(self.factory, self.scope)

    def commit(self) -> None:
        self.committed = True
        self.factory.commits += 1


class ScriptedUowFactory:
    def __init__(self, steps: Sequence[Step]) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[object, str, Any]] = []
        self.scopes: list[object] = []
        self.commits = 0

    def begin(self, scope: object) -> ScriptedUow:
        return ScriptedUow(self, scope)

    def assert_consumed(self) -> None:
        assert self.steps == []


def _settings() -> SleepBackendSettings:
    return SleepBackendSettings(
        profile="test-worker",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.WORKER,
        data_mode=DataMode.REPLAY,
        database_dsn="postgresql://worker:secret@postgres/replay_db",
        database_identity="replay_db",
        database_role="sleepagent_worker_replay",
        service_principal_id="sleepagent-worker-test",
        service_credential_ref="test:worker-credential",
        database_scope=DataMode.REPLAY,
        namespace_prefixes=("replay:test",),
        worker_queues=("fast_path",),
        provider_mode=ProviderMode.FAKE,
        model_mode=ModelMode.DETERMINISTIC,
        signing_key_ref="test:signing",
        encryption_key_ref="test:encryption",
    )


def _claim(*, generation: int = 2) -> LeaseClaim:
    return LeaseClaim(
        work_id="operation-1",
        operation_id="operation-1",
        queue="fast_path",
        namespace_id="replay:test",
        data_mode="replay",
        namespace_generation=3,
        run_id="run-1",
        arm_id="arm-1",
        subject_id="subject-1",
        operation_version=4,
        lease_generation=generation,
        fencing_token="11111111-1111-4111-8111-111111111111",
        worker_instance="worker-1",
        attempt=generation,
        max_attempts=5,
        lease_deadline=datetime.now(tz=UTC) + timedelta(seconds=30),
        payload={"input": "opaque"},
        authorization_snapshot={
            "authorization_epoch": 7,
            "privacy_epoch": 8,
            "retrieval_policy_epoch": 9,
        },
        metadata={
            "work_kind": "operation",
            "operation_type": "fast_path",
            "lease_seconds": 30,
        },
    )


def _delivery_claim() -> LeaseClaim:
    return _claim().model_copy(
        update={
            "work_id": "delivery-1",
            "operation_id": "operation-1",
            "queue": "delivery:family_sink",
            "operation_version": 0,
            "metadata": {
                "work_kind": "delivery",
                "destination": "family_sink",
                "lease_seconds": 30,
            },
        }
    )


def test_claim_commits_cross_scope_before_loading_exact_payload() -> None:
    deadline = datetime.now(tz=UTC) + timedelta(seconds=30)
    token = "11111111-1111-4111-8111-111111111111"
    factory = ScriptedUowFactory(
        [
            Step(
                "sleepagent_claim_operation",
                (
                    "operation-1",
                    "replay:test",
                    "replay",
                    3,
                    "run-1",
                    "arm-1",
                    "subject-1",
                    "fast_path",
                    7,
                    8,
                    9,
                    2,
                    token,
                ),
            ),
            Step(
                "FROM public.sleep_domain_operations",
                (
                    4,
                    2,
                    5,
                    deadline,
                    {"input": "opaque"},
                    {
                        "authorization_epoch": 7,
                        "privacy_epoch": 8,
                        "retrieval_policy_epoch": 9,
                    },
                    "fast_path",
                    "fast_path",
                ),
            ),
        ]
    )
    store = PostgresDurableWorkStore(_settings(), factory)

    claim = store.claim(
        queue="fast_path",
        worker_instance="worker-1",
        lease_seconds=30,
    )

    assert claim is not None
    assert claim.operation_version == 4
    assert claim.authorization_snapshot["privacy_epoch"] == 8
    assert claim.metadata["lease_seconds"] == 30
    assert factory.commits == 2
    assert isinstance(factory.scopes[0], WorkerClaimScope)
    assert factory.scopes[0].guc_values()["sleepagent.namespace_id"] == ""
    assert isinstance(factory.scopes[1], UowScope)
    assert factory.scopes[1].namespace_id == "replay:test"
    assert factory.scopes[1].namespace_generation == 3
    assert factory.scopes[1].authorization_epoch == 7
    factory.assert_consumed()


def test_retry_finalize_uses_server_deadline_and_exact_sql_signature() -> None:
    retry_at = datetime.now(tz=UTC) + timedelta(seconds=8)
    factory = ScriptedUowFactory(
        [
            Step("clock_timestamp() + make_interval", (retry_at,)),
            Step("sleepagent_finalize_operation", (True,)),
            Step("MAX(checkpoint_sequence)", (1,)),
            Step("INSERT INTO public.backend_operation_checkpoints"),
        ]
    )
    store = PostgresDurableWorkStore(_settings(), factory)

    finalized = store.finalize(
        _claim(),
        WorkResult(
            disposition=WorkDisposition.RETRYABLE,
            error_code="database_unavailable",
            retry_after_seconds=8,
        ),
    )

    assert finalized is True
    finalize_call = next(
        call for call in factory.calls if "sleepagent_finalize_operation" in call[1]
    )
    assert finalize_call[2][0:6] == (
        "operation-1",
        4,
        2,
        "11111111-1111-4111-8111-111111111111",
        "retry",
        "database_unavailable",
    )
    assert finalize_call[2][6] == retry_at
    finalize_index = factory.calls.index(finalize_call)
    checkpoint_index = next(
        index
        for index, call in enumerate(factory.calls)
        if "INSERT INTO public.backend_operation_checkpoints" in call[1]
    )
    assert finalize_index < checkpoint_index
    assert all("FOR UPDATE" not in query for _, query, _ in factory.calls)
    assert factory.commits == 1
    factory.assert_consumed()


def test_stale_fence_cannot_checkpoint_or_reach_insert() -> None:
    factory = ScriptedUowFactory(
        [Step("sleepagent_heartbeat_operation", (False,))]
    )
    store = PostgresDurableWorkStore(_settings(), factory)

    assert store.checkpoint(
        _claim(),
        checkpoint_type="prepared",
        payload={"opaque": True},
    ) is False
    assert factory.commits == 0
    assert all("INSERT" not in query for _, query, _ in factory.calls)
    factory.assert_consumed()


def test_operation_heartbeat_lock_still_rejects_stale_cas_version() -> None:
    factory = ScriptedUowFactory(
        [
            Step("sleepagent_heartbeat_operation", (True,)),
            Step("SELECT cas_version", (5,)),
        ]
    )
    store = PostgresDurableWorkStore(_settings(), factory)

    assert store.checkpoint(
        _claim(),
        checkpoint_type="prepared",
        payload={"opaque": True},
    ) is False

    assert factory.commits == 0
    assert all("FOR UPDATE" not in query for _, query, _ in factory.calls)
    factory.assert_consumed()


def test_delivery_finalize_locks_via_heartbeat_without_update_privilege() -> None:
    claim = _delivery_claim()
    factory = ScriptedUowFactory(
        [
            Step("sleepagent_heartbeat_delivery", (True,)),
            Step(
                "SELECT status FROM public.backend_delivery_intents",
                ("dispatching",),
            ),
            Step("MAX(sequence)", (1,)),
            Step("INSERT INTO public.backend_delivery_journal"),
            Step("sleepagent_finalize_delivery", (True,)),
        ]
    )
    store = PostgresDurableWorkStore(_settings(), factory)

    assert store.finalize(
        claim,
        WorkResult(disposition=WorkDisposition.SUCCEEDED),
    ) is True

    authority_queries = [
        query
        for _, query, _ in factory.calls
        if "backend_delivery_intents" in query
    ]
    assert authority_queries
    assert all("FOR UPDATE" not in query for query in authority_queries)
    assert factory.commits == 1
    factory.assert_consumed()


def test_delivery_dispatch_permit_uses_delivery_then_invocation_lock_order() -> None:
    claim = _delivery_claim()
    digest = "a" * 64
    record = InvocationRecord(
        invocation_id="invocation-1",
        invocation_key="family:effect-1",
        invocation_kind=InvocationKind.EXTERNAL_SINK,
        work_id=claim.work_id,
        lease_generation=claim.lease_generation,
        request_sha256=digest,
        state=InvocationState.RESERVED,
    )
    factory = ScriptedUowFactory(
        [
            Step("sleepagent_heartbeat_delivery", (True,)),
            Step(
                "SELECT status FROM public.backend_delivery_intents",
                ("running",),
            ),
            Step(
                "FROM public.backend_invocations WHERE invocation_id",
                (
                    "reserved",
                    1,
                    digest,
                    claim.lease_generation,
                    claim.fencing_token,
                ),
            ),
            Step("sleepagent_mark_delivery_dispatching", (True,)),
            Step("MAX(sequence)", (1,)),
            Step("INSERT INTO public.backend_delivery_journal"),
            Step("current_state = 'send_started'", (2,)),
            Step("MAX(sequence)", (2,)),
            Step("INSERT INTO public.backend_invocation_journal"),
        ]
    )
    store = PostgresDurableWorkStore(_settings(), factory)

    assert store.mark_invocation_send_started(claim, record) is True

    queries = [query for _, query, _ in factory.calls]
    heartbeat_index = next(
        index
        for index, query in enumerate(queries)
        if "sleepagent_heartbeat_delivery" in query
    )
    invocation_lock_index = next(
        index
        for index, query in enumerate(queries)
        if "backend_invocations" in query and "FOR UPDATE" in query
    )
    mark_index = next(
        index
        for index, query in enumerate(queries)
        if "sleepagent_mark_delivery_dispatching" in query
    )
    assert heartbeat_index < invocation_lock_index < mark_index
    assert all(
        "FOR UPDATE" not in query
        for query in queries
        if "backend_delivery_intents" in query
    )
    assert factory.commits == 1
    factory.assert_consumed()


def test_ambiguous_send_commits_outcome_unknown_journal() -> None:
    claim = _claim()
    request = {"opaque": "payload"}
    digest = hashlib.sha256(
        b'{"opaque":"payload"}'
    ).hexdigest()
    token = claim.fencing_token
    factory = ScriptedUowFactory(
        [
            # reserve
            Step("sleepagent_heartbeat_operation", (True,)),
            Step("SELECT cas_version", (4,)),
            Step("FROM public.backend_invocations AS invocation", None),
            Step("INSERT INTO public.backend_invocations"),
            Step("MAX(sequence)", (1,)),
            Step("INSERT INTO public.backend_invocation_journal"),
            # dispatch permit
            Step("sleepagent_heartbeat_operation", (True,)),
            Step("SELECT cas_version", (4,)),
            Step(
                "FROM public.backend_invocations WHERE invocation_id",
                ("reserved", 1, digest, 2, token),
            ),
            Step("current_state = 'send_started'", (2,)),
            Step("MAX(sequence)", (2,)),
            Step("INSERT INTO public.backend_invocation_journal"),
            # ambiguous outcome
            Step("sleepagent_heartbeat_operation", (True,)),
            Step("SELECT cas_version", (4,)),
            Step(
                "FROM public.backend_invocations WHERE invocation_id",
                ("send_started", 2, digest, 2, token),
            ),
            Step("SET current_state = %s", (3,)),
            Step("MAX(sequence)", (3,)),
            Step("INSERT INTO public.backend_invocation_journal"),
        ]
    )
    identifiers = iter((f"id-{index}" for index in range(1, 20)))
    store = PostgresDurableWorkStore(
        _settings(),
        factory,
        id_generator=lambda: next(identifiers),
    )
    dispatcher = InvocationDispatcher(
        store=store,
        claim=claim,
        lease_lost=threading.Event(),
    )

    def ambiguous_sender():
        raise ConnectionError("lost after write")

    with pytest.raises(OutcomeUnknownError):
        dispatcher.dispatch(
            invocation_key="provider:semantic-effect-1",
            request=request,
            sender=ambiguous_sender,
        )

    outcome_update = next(
        call for call in factory.calls if "SET current_state = %s" in call[1]
    )
    assert outcome_update[2][0] == InvocationState.OUTCOME_UNKNOWN.value
    final_journal = [
        call
        for call in factory.calls
        if "INSERT INTO public.backend_invocation_journal" in call[1]
    ][-1]
    assert "connector_outcome_unknown" in final_journal[2][-1]
    authority_locks = [
        query
        for _, query, _ in factory.calls
        if "sleep_domain_operations" in query and "FOR UPDATE" in query
    ]
    assert authority_locks == []
    invocation_locks = [
        query
        for _, query, _ in factory.calls
        if "backend_invocations" in query and "FOR UPDATE" in query
    ]
    assert invocation_locks
    assert factory.commits == 3
    factory.assert_consumed()


def test_delivery_outcome_unknown_atomically_creates_reconciliation_operation() -> None:
    claim = _delivery_claim().model_copy(
        update={
            "metadata": {
                "work_kind": "delivery",
                "destination": "replay_care_notification",
                "handler_name": "deterministic_replay_sink",
                "semantic_effect_key": "effect-key-1",
                "lease_seconds": 30,
            }
        }
    )
    factory = ScriptedUowFactory(
        [
            Step("sleepagent_heartbeat_delivery", (True,)),
            Step("SELECT status FROM public.backend_delivery_intents", ("dispatching",)),
            Step("MAX(sequence)", (3,)),
            Step("INSERT INTO public.backend_delivery_journal"),
            Step("sleepagent_finalize_delivery", (True,)),
            Step(
                "source.policy_sha256",
                (
                    "replay_care_notification",
                    "deterministic_replay_sink",
                    "effect-key-1",
                    "a" * 64,
                    "b" * 64,
                ),
            ),
            Step("INSERT INTO public.sleep_domain_operations"),
        ]
    )
    store = PostgresDurableWorkStore(
        _settings(), factory, id_generator=lambda: "01987654-3210-7abc-8def-0123456789ab"
    )

    finalized = store.finalize(
        claim,
        WorkResult(
            disposition=WorkDisposition.OUTCOME_UNKNOWN,
            error_code="prior_send_requires_reconciliation",
        ),
    )

    assert finalized is True
    operation_insert = factory.calls[-1]
    assert operation_insert[2][5] == "delivery-1"
    assert "delivery_reconciliation" in operation_insert[1]
    assert '"allowed_handler":"reconciliation"' in operation_insert[2][-2]
    assert factory.commits == 1
    factory.assert_consumed()


def test_known_not_delivered_reconciliation_is_the_only_reopen_path() -> None:
    claim = _delivery_claim().model_copy(
        update={
            "lease_generation": 3,
            "fencing_token": "33333333-3333-4333-8333-333333333333",
        }
    )
    request_sha256 = "a" * 64
    factory = ScriptedUowFactory(
        [
            Step("sleepagent_heartbeat_delivery", (True,)),
            Step("SELECT status FROM public.backend_delivery_intents", ("running",)),
            Step(
                "FROM public.backend_invocations AS invocation",
                (
                    "invocation-1",
                    "external_sink",
                    request_sha256,
                    "reconciled",
                    2,
                    "22222222-2222-4222-8222-222222222222",
                    None,
                    4,
                    {
                        "event": "reconciled",
                        "resolution": "known_not_delivered",
                    },
                ),
            ),
            Step("current_state = 'reserved'", (5,)),
            Step("MAX(sequence)", (5,)),
            Step("INSERT INTO public.backend_invocation_journal"),
        ]
    )
    store = PostgresDurableWorkStore(_settings(), factory)

    record = store.reserve_invocation(
        claim,
        invocation_kind=InvocationKind.EXTERNAL_SINK,
        invocation_key="replay-delivery:effect-key-1:v1",
        request_sha256=request_sha256,
    )

    assert record.state == InvocationState.RESERVED
    assert record.lease_generation == 3
    rebound_event = factory.calls[-1][2][-1]
    assert '"event":"lease_rebound"' in rebound_event
    assert factory.commits == 1
    factory.assert_consumed()
