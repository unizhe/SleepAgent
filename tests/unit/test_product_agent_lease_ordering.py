from __future__ import annotations

import threading
from contextlib import AbstractContextManager
from typing import Any, cast

import pytest

from sleepagent.persistence.uow import UowScope
from sleepagent.workers.product import (
    LoadedProductAgentSource,
    ProductAgentLease,
    ProductAgentProcessor,
)


pytestmark = pytest.mark.unit


class _Connection:
    def __init__(self, phase: int) -> None:
        self.phase = phase


class _UnitOfWork(AbstractContextManager["_UnitOfWork"]):
    def __init__(
        self,
        phase: int,
        operation_lock: threading.Lock,
        events: list[str],
    ) -> None:
        self.connection = _Connection(phase)
        self._phase = phase
        self._operation_lock = operation_lock
        self._events = events

    def __enter__(self) -> "_UnitOfWork":
        self._events.append(f"begin:{self._phase}")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._phase == 0 and self._operation_lock.locked():
            self._operation_lock.release()
        return None

    def commit(self) -> None:
        self._events.append(f"commit:{self._phase}")
        if self._phase == 0:
            self._operation_lock.release()


class _UnitOfWorkFactory:
    def __init__(self, operation_lock: threading.Lock, events: list[str]) -> None:
        self._operation_lock = operation_lock
        self._events = events
        self._phase = 0

    def begin(self, scope: UowScope) -> _UnitOfWork:
        del scope
        phase = self._phase
        self._phase += 1
        return _UnitOfWork(phase, self._operation_lock, self._events)


class _Repository:
    def __init__(
        self,
        connection: _Connection,
        operation_lock: threading.Lock,
        load_started: threading.Event,
        heartbeat_renewed: threading.Event,
        events: list[str],
        source: LoadedProductAgentSource,
    ) -> None:
        self._connection = connection
        self._operation_lock = operation_lock
        self._load_started = load_started
        self._heartbeat_renewed = heartbeat_renewed
        self._events = events
        self._source = source

    def lock_source_lease(self, lease: ProductAgentLease) -> None:
        del lease
        assert self._connection.phase == 0
        assert self._operation_lock.acquire(timeout=0.01)
        self._events.append("operation-fence-locked")

    def load_source(self, lease: ProductAgentLease) -> LoadedProductAgentSource:
        del lease
        assert self._connection.phase == 1
        assert self._operation_lock.locked() is False
        self._events.append("large-source-load-started")
        self._load_started.set()
        # Use a cardinality larger than the failed real source without making
        # that incident's exact membership count a production constant.
        assert sum(range(12_000)) > 0
        assert self._heartbeat_renewed.wait(timeout=1)
        self._events.append("large-source-load-finished")
        return self._source


def test_large_source_load_releases_operation_lock_before_heartbeat_window() -> None:
    operation_lock = threading.Lock()
    load_started = threading.Event()
    heartbeat_renewed = threading.Event()
    events: list[str] = []
    source = cast(LoadedProductAgentSource, object())
    factory = _UnitOfWorkFactory(operation_lock, events)

    def repository_factory(connection: Any, scope: UowScope) -> Any:
        del scope
        return _Repository(
            cast(_Connection, connection),
            operation_lock,
            load_started,
            heartbeat_renewed,
            events,
            source,
        )

    processor = ProductAgentProcessor(
        cast(Any, factory),
        runtime_bundle=cast(Any, object()),
        repository_factory=repository_factory,
    )
    scope = UowScope(
        namespace_id="live:test",
        data_mode="live",
        process_role="worker",
        purpose="worker",
        service_principal_id="worker-test",
        namespace_generation=1,
        subject_id="subject-test",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-test",
    )
    lease = ProductAgentLease(
        operation_id="operation-test",
        attempt_sequence=1,
        lease_generation=1,
        fencing_token="f" * 64,
        worker_instance="worker-test",
    )

    def heartbeat() -> None:
        assert load_started.wait(timeout=1)
        if operation_lock.acquire(timeout=0.02):
            events.append("heartbeat-renewed")
            operation_lock.release()
            heartbeat_renewed.set()

    heartbeat_thread = threading.Thread(target=heartbeat)
    heartbeat_thread.start()
    try:
        assert processor.load_source(scope, lease) is source
    finally:
        heartbeat_thread.join(timeout=1)

    assert heartbeat_thread.is_alive() is False
    assert heartbeat_renewed.is_set()
    assert events == [
        "begin:0",
        "operation-fence-locked",
        "commit:0",
        "begin:1",
        "large-source-load-started",
        "heartbeat-renewed",
        "large-source-load-finished",
        "commit:1",
    ]
