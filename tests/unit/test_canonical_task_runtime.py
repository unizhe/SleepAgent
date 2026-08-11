from __future__ import annotations

import inspect
import json
import sqlite3
from typing import Any

import pytest
from pydantic import ValidationError

from sleepagent.persistence import RadarPersistenceStore, RadarSubject
from sleepagent.product_runtime.task_runtime import (
    RadarAgentTask,
    RadarTaskStatus,
    TaskService,
)
from sleepagent.product_runtime.schemas import RadarDevice, RadarDeviceStatus


def _canonical_task_payload() -> dict[str, Any]:
    return {
        "task_id": "task-canonical",
        "trace_id": "trace-canonical",
        "subject_id": "subject-canonical",
        "radar_device_id": "device-canonical",
    }


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
