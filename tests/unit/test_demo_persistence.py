from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from sleepagent.demo_api import (
    DemoAdvanceRequest,
    DemoResetRequest,
    DemoSeedRequest,
    DemoTraceEntry,
)
from sleepagent.demo_persistence import (
    DemoAdvanceReservation,
    DemoReservation,
    DemoResetReservation,
    DurableDemoController,
    StoredDemoClock,
    StoredDemoOperation,
)
from sleepagent.simulation.generator import CanonicalReplayGenerator


pytestmark = pytest.mark.unit
UTC = timezone.utc
NOW = datetime(2026, 3, 2, 7, 0, tzinfo=UTC)


class Store:
    def __init__(self) -> None:
        self.root_id: str | None = None
        self.requests: dict[str, str] = {}
        self.reserve_calls = 0
        self.advance_id: str | None = None
        self.advance_requests: dict[str, str] = {}
        self.reset_id: str | None = None
        self.reset_requests: dict[str, str] = {}

    def reserve_seed(self, **values):
        self.reserve_calls += 1
        key = values["caller_idempotency_key"]
        request_sha256 = values["request_sha256"]
        if key in self.requests and self.requests[key] != request_sha256:
            raise RuntimeError("idempotency_conflict")
        self.requests[key] = request_sha256
        if self.root_id is None:
            self.root_id = values["root_operation_id"]
        return DemoReservation(
            operation_id=self.root_id,
            journey_id="journey-1",
            namespace_id="replay:normal-one-night",
            generation=1,
            run_id="run-1",
            arm_id="arm-1",
            subject_id="subject-1",
            phase="accepted",
            reused=self.reserve_calls > 1,
        )

    def get_operation(self, operation_id: str) -> StoredDemoOperation:
        if operation_id != self.root_id:
            raise LookupError("operation_not_found")
        return StoredDemoOperation(
            operation_id=operation_id,
            generation=1,
            phase="accepted",
            result=None,
            error_code=None,
            updated_at=NOW,
        )

    def reserve_advance(self, **values):
        key = values["caller_idempotency_key"]
        request_sha256 = values["request_sha256"]
        if (
            key in self.advance_requests
            and self.advance_requests[key] != request_sha256
        ):
            raise RuntimeError("idempotency_conflict")
        self.advance_requests[key] = request_sha256
        reused = self.advance_id is not None
        if self.advance_id is None:
            self.advance_id = values["operation_id"]
        return DemoAdvanceReservation(
            operation_id=self.advance_id,
            generation=1,
            reused=reused,
        )

    def get_clock(self) -> StoredDemoClock:
        return StoredDemoClock(
            scenario_time=datetime(2026, 3, 1, 20, 0, tzinfo=UTC),
            generation=1,
        )

    def reserve_reset(self, **values):
        key = values["caller_idempotency_key"]
        request_sha256 = values["request_sha256"]
        if key in self.reset_requests and self.reset_requests[key] != request_sha256:
            raise RuntimeError("idempotency_conflict")
        self.reset_requests[key] = request_sha256
        reused = self.reset_id is not None
        if self.reset_id is None:
            self.reset_id = values["operation_id"]
        return DemoResetReservation(
            operation_id=self.reset_id,
            generation=2,
            reused=reused,
        )

    def read_trace(self, *, operation_id, after_sequence, limit):
        del operation_id, limit
        return tuple(
            DemoTraceEntry(
                sequence=value,
                event_type="journey.accepted",
                root_operation_id=self.root_id or "root-1",
                operation_id=self.root_id,
                state="accepted",
                occurred_at=NOW,
            )
            for value in (1, 2)
            if value > after_sequence
        )


def _request(*, batch_size: int = 100) -> DemoSeedRequest:
    return DemoSeedRequest(
        artifact_family="canonical-replay-fixtures",
        scenario_id="normal-one-night",
        batch_size=batch_size,
    )


def test_seed_reserves_only_a_durable_root_and_does_not_run_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store()
    controller = DurableDemoController(store, now_factory=lambda: NOW)
    monkeypatch.setattr(
        CanonicalReplayGenerator,
        "generate",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("HTTP seed must not generate a night")
        ),
    )

    accepted = controller.seed(request=_request(), idempotency_key="seed-1")

    assert accepted.operation_id == store.root_id
    assert accepted.generation == 1
    assert store.reserve_calls == 1


def test_equivalent_caller_keys_reuse_root_and_conflicting_body_fails() -> None:
    store = Store()
    controller = DurableDemoController(store, now_factory=lambda: NOW)

    first = controller.seed(request=_request(), idempotency_key="caller-a")
    second = controller.seed(request=_request(), idempotency_key="caller-b")
    assert first.operation_id == second.operation_id

    with pytest.raises(HTTPException) as conflict:
        controller.seed(request=_request(batch_size=50), idempotency_key="caller-a")
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "idempotency_conflict"


def test_unknown_seed_and_oversized_batch_fail_before_reservation() -> None:
    store = Store()
    controller = DurableDemoController(store)
    with pytest.raises(HTTPException) as missing:
        controller.seed(
            request=DemoSeedRequest(
                artifact_family="canonical-replay-fixtures",
                scenario_id="does-not-exist",
                batch_size=100,
            ),
            idempotency_key="missing",
        )
    assert missing.value.status_code == 404
    assert store.reserve_calls == 0

    with pytest.raises(ValidationError):
        _request(batch_size=101)


def test_operation_clock_trace_and_advance_are_typed() -> None:
    store = Store()
    controller = DurableDemoController(store, now_factory=lambda: NOW)
    accepted = controller.seed(request=_request(), idempotency_key="seed-1")

    operation = controller.operation(operation_id=accepted.operation_id)
    clock = controller.clock()
    first_page = controller.trace(
        operation_id=accepted.operation_id,
        cursor=None,
        limit=1,
    )

    assert operation.state == "accepted"
    assert clock.data_mode == "replay"
    assert len(first_page.entries) == 1
    assert first_page.next_cursor == "1"

    advance = controller.advance(
        request=DemoAdvanceRequest(seconds=60),
        idempotency_key="advance-1",
    )
    replay = controller.advance(
        request=DemoAdvanceRequest(seconds=60),
        idempotency_key="advance-1",
    )
    assert advance.operation_id == replay.operation_id == store.advance_id
    assert advance.generation == 1

    with pytest.raises(HTTPException) as conflict:
        controller.advance(
            request=DemoAdvanceRequest(seconds=120),
            idempotency_key="advance-1",
        )
    assert conflict.value.status_code == 409

    reset = controller.reset(
        request=DemoResetRequest(confirmation="reset-replay-generation"),
        idempotency_key="reset-1",
    )
    reset_replay = controller.reset(
        request=DemoResetRequest(confirmation="reset-replay-generation"),
        idempotency_key="reset-1",
    )
    assert reset.operation_id == reset_replay.operation_id == store.reset_id
    assert reset.generation == 2
