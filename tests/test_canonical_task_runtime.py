from __future__ import annotations

import inspect
import json
import sqlite3
from typing import Any

import pytest
from pydantic import ValidationError

from sleepagent.radar_agent.persistence import RadarPersistenceStore, RadarSubject
from sleepagent.radar_agent.persistence.history import HistoricalTaskRecord
from sleepagent.radar_agent.runtime import (
    IdempotencyConflict,
    InvalidTaskTransition,
    RadarAgentTask,
    RadarTaskStatus,
    TaskService,
)
from sleepagent.radar_agent.schemas import RadarDevice, RadarDeviceStatus


def _canonical_task_payload() -> dict[str, Any]:
    return {
        "task_id": "task-canonical",
        "trace_id": "trace-canonical",
        "subject_id": "subject-canonical",
        "radar_device_id": "device-canonical",
    }


def _historical_task() -> HistoricalTaskRecord:
    return HistoricalTaskRecord(
        task_id="task-history",
        trace_id="trace-history",
        subject_id="subject-history",
        radar_device_id="device-history",
        role="family",
        scenario="replay",
        runtime_kind="retired-runtime",
        runtime_contract_version="retired.v1",
        status="running",
    )


class _HistoricalOnlyStore:
    """Fail if TaskService reaches any persistence method beyond read lookup."""

    def __init__(self) -> None:
        self.task = _historical_task()

    def get_task(self, task_id: str) -> HistoricalTaskRecord:
        assert task_id == self.task.task_id
        return self.task

    def get_task_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> HistoricalTaskRecord:
        assert idempotency_key == "occupied-history-key"
        return self.task

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"historical mutation reached store.{name}")


def test_radar_agent_task_schema_exposes_only_canonical_runtime_state() -> None:
    task = RadarAgentTask(**_canonical_task_payload())

    assert task.runtime_kind == "product_episode"
    assert task.runtime_contract_version == "product-episode.v1"
    assert "node_status" not in RadarAgentTask.model_fields
    assert "current_plan_id" not in RadarAgentTask.model_fields
    assert "pending_user_input_request_id" not in RadarAgentTask.model_fields

    with pytest.raises(ValidationError):
        RadarAgentTask(**_canonical_task_payload(), runtime_kind="retired-runtime")
    with pytest.raises(ValidationError):
        RadarAgentTask(**_canonical_task_payload(), node_status={"step": "pending"})


def test_persisted_canonical_task_decoder_accepts_only_inert_removed_state() -> None:
    persisted = {
        **_canonical_task_payload(),
        "runtime_kind": "product_episode",
        "runtime_contract_version": "product-episode.v1",
        "node_status": {},
        "current_plan_id": None,
        "pending_user_input_request_id": None,
    }

    decoded = RadarAgentTask.decode_persisted_json(json.dumps(persisted))

    assert decoded.task_id == "task-canonical"
    assert decoded.runtime_kind == "product_episode"
    assert "node_status" not in decoded.model_dump(mode="json")

    persisted["node_status"] = {"retired-step": "running"}
    with pytest.raises(ValueError, match="active retired state: node_status"):
        RadarAgentTask.decode_persisted_json(json.dumps(persisted))


def test_task_service_creates_and_advances_only_canonical_tasks() -> None:
    parameters = inspect.signature(TaskService.create_task).parameters
    assert "runtime_kind" not in parameters
    assert "runtime_contract_version" not in parameters

    store = RadarPersistenceStore.connect_sqlite(sqlite3.connect(":memory:"))
    store.save_subject(
        RadarSubject(
            subject_id="subject-canonical",
            display_name="Canonical subject",
        )
    )
    store.save_device(
        RadarDevice(
            radar_device_id="device-canonical",
            display_name="Canonical device",
            provider="test",
            status=RadarDeviceStatus.ONLINE,
            bound_subject_id="subject-canonical",
        )
    )
    service = TaskService(
        store,
        validate_bindings=False,
        require_authorization=False,
    )
    created = service.create_task(
        subject_id="subject-canonical",
        radar_device_id="device-canonical",
    )
    running = service.transition_task(created.task_id, RadarTaskStatus.RUNNING)
    failed = service.fail_task(
        created.task_id,
        error_code="expected-test-failure",
        message="exercise retry lifecycle",
    )
    retried = service.retry_failed_task(created.task_id)

    assert created.runtime_kind == "product_episode"
    assert running.status is RadarTaskStatus.RUNNING
    assert failed.status is RadarTaskStatus.FAILED
    assert retried.status is RadarTaskStatus.RUNNING
    assert retried.retry_count == 1
    assert [event.sequence for event in service.list_events(created.task_id)] == [
        1,
        2,
        3,
        4,
    ]


def test_historical_idempotency_key_cannot_create_or_alias_a_canonical_task() -> None:
    service = TaskService(
        _HistoricalOnlyStore(),  # type: ignore[arg-type]
        validate_bindings=False,
        require_authorization=False,
    )

    with pytest.raises(IdempotencyConflict, match="read-only historical task"):
        service.create_task(
            subject_id="subject-history",
            radar_device_id="device-history",
            idempotency_key="occupied-history-key",
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda service, task_id: service.emit_event(
            task_id,
            event_type="test.event",
            message="must not persist",
        ),
        lambda service, task_id: service.append_event(task_id, None),
        lambda service, task_id: service.transition_task(
            task_id,
            RadarTaskStatus.COMPLETED,
        ),
        lambda service, task_id: service.fail_task(
            task_id,
            error_code="test",
            message="must not persist",
        ),
        lambda service, task_id: service.retry_failed_task(task_id),
        lambda service, task_id: service.rerun_failed_task(task_id),
        lambda service, task_id: service.save_payload_artifact(
            task_id,
            artifact_id="artifact-history",
            artifact_type="test",
            payload={},
        ),
        lambda service, task_id: service.record_audit(
            task_id,
            actor="actor",
            action="test",
        ),
    ],
    ids=[
        "emit-event",
        "append-event",
        "transition-task",
        "fail-task",
        "retry-task",
        "rerun-task",
        "save-artifact",
        "record-audit",
    ],
)
def test_historical_task_mutations_fail_before_store_write(mutation: Any) -> None:
    store = _HistoricalOnlyStore()
    service = TaskService(
        store,  # type: ignore[arg-type]
        validate_bindings=False,
        require_authorization=False,
    )

    with pytest.raises(InvalidTaskTransition, match="historical task is read-only"):
        mutation(service, store.task.task_id)
