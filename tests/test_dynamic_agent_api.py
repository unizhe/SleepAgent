from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from sleepagent.radar_agent.api.http import (
    RADAR_AGENT_API_KEY_ENV,
    RADAR_AGENT_DEV_MODE_ENV,
    reset_radar_api_runtime_for_tests,
)


API_KEY = "dynamic-api-key"
ACTOR_ID = "dynamic-family-user"


@pytest.fixture(autouse=True)
def _runtime(monkeypatch):
    monkeypatch.setenv(RADAR_AGENT_API_KEY_ENV, API_KEY)
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "true")
    reset_radar_api_runtime_for_tests()


def _headers() -> dict[str, str]:
    return {
        "x-api-key": API_KEY,
        "x-actor-id": ACTOR_ID,
        "x-actor-role": "family",
    }


def _payload() -> dict[str, str]:
    return {
        "runtime_kind": "dynamic_goal",
        "goal_type": "night_review",
        "target_date": "2026-07-09",
        "scenario": "normal_night",
    }


def test_dynamic_create_uses_authenticated_role_and_run_is_database_queued() -> None:
    with TestClient(app) as client:
        denied = client.post(
            "/radar-agent/tasks",
            headers={"x-api-key": API_KEY},
            json=_payload(),
        )
        created = client.post(
            "/radar-agent/tasks",
            headers={**_headers(), "Idempotency-Key": "dynamic-night-1"},
            json=_payload(),
        )
        task_id = created.json()["task"]["task_id"]
        queued = client.post(
            f"/radar-agent/tasks/{task_id}/run", headers=_headers()
        )
        deadline = time.monotonic() + 3
        detail = None
        while time.monotonic() < deadline:
            detail = client.get(
                f"/radar-agent/tasks/{task_id}", headers=_headers()
            )
            if detail.json()["task"]["status"] in {
                "completed",
                "failed",
                "waiting_for_confirmation",
            }:
                break
            time.sleep(0.03)
        trace = client.get(
            f"/radar-agent/tasks/{task_id}/decision-trace", headers=_headers()
        )
        history = client.get(
            "/radar-agent/tasks?view=history", headers=_headers()
        )

    assert denied.status_code == 403
    assert created.status_code == 200
    assert created.json()["task"]["role"] == "family"
    assert queued.status_code == 202
    assert detail is not None
    assert detail.json()["task"]["status"] == "completed"
    assert detail.json()["task"]["execution_mode"] == "safe_degraded"
    assert detail.json()["completion_receipt"]["completion_status"] == "complete"
    assert trace.status_code == 200
    assert any(
        item["event_type"] == "execution.degraded"
        for item in trace.json()["entries"]
    )
    assert [item["task"]["task_id"] for item in history.json()] == [task_id]


def test_creating_or_refreshing_a_task_never_starts_analysis() -> None:
    with TestClient(app) as client:
        created = client.post(
            "/radar-agent/tasks",
            headers={**_headers(), "Idempotency-Key": "created-not-run"},
            json=_payload(),
        )
        task_id = created.json()["task"]["task_id"]
        time.sleep(0.15)
        refreshed = client.get(
            f"/radar-agent/tasks/{task_id}", headers=_headers()
        )

    assert created.status_code == 200
    assert refreshed.json()["task"]["status"] == "created"
    assert refreshed.json()["task"]["current_plan_id"] is None
    assert refreshed.json()["artifacts"] == []


def test_dynamic_compatibility_routes_are_hidden_outside_dev_mode(monkeypatch) -> None:
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "false")
    reset_radar_api_runtime_for_tests()
    payload = {
        **_payload(),
        "scenario": "worsening_trend",
    }
    with TestClient(app) as client:
        rejected = client.post(
            "/radar-agent/tasks", headers=_headers(), json=payload
        )
        missing_bindings = client.post(
            "/radar-agent/tasks", headers=_headers(), json=_payload()
        )

    assert rejected.status_code == 404
    assert missing_bindings.status_code == 404


def test_dynamic_doctor_material_confirmation_uses_authenticated_identity() -> None:
    payload = {
        **_payload(),
        "goal_type": "doctor_material",
    }
    with TestClient(app) as client:
        created = client.post(
            "/radar-agent/tasks", headers=_headers(), json=payload
        )
        task_id = created.json()["task"]["task_id"]
        assert client.post(
            f"/radar-agent/tasks/{task_id}/run", headers=_headers()
        ).status_code == 202
        deadline = time.monotonic() + 3
        detail = None
        while time.monotonic() < deadline:
            detail = client.get(
                f"/radar-agent/tasks/{task_id}", headers=_headers()
            ).json()
            if detail["task"]["status"] == "waiting_for_confirmation":
                break
            time.sleep(0.03)
        pending = next(
            item for item in detail["confirmations"] if item["status"] == "pending"
        )
        resolved = client.post(
            f"/radar-agent/tasks/{task_id}/confirm",
            headers=_headers(),
            json={
                "confirmation_id": pending["confirmation_id"],
                "approved": True,
                "actor_id": "body-identity-is-ignored",
                "actor_role": "doctor",
            },
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            detail = client.get(
                f"/radar-agent/tasks/{task_id}", headers=_headers()
            ).json()
            if detail["task"]["status"] == "completed":
                break
            time.sleep(0.03)

    assert resolved.status_code == 200
    assert resolved.json()["resolved_by"] == ACTOR_ID
    assert detail["task"]["status"] == "completed"
    doctor_reports = [
        item for item in detail["artifacts"] if item["artifact_type"] == "role_report:doctor"
    ]
    assert len(doctor_reports) == 1
    exports = [
        item
        for item in detail["artifacts"]
        if item["artifact_type"] == "doctor_material_export"
    ]
    assert len(exports) == 1
    confirmation = next(
        item
        for item in detail["confirmations"]
        if item["confirmation_id"] == pending["confirmation_id"]
    )
    assert confirmation["execution_status"] == "completed"
    assert detail["completion_receipt"]["actions_executed"] == [
        "export_doctor_material"
    ]
    source_hash = doctor_reports[0]["metadata"]["fact_snapshot_sha256"]
    assert exports[0]["payload"]["fact_snapshot_sha256"] == source_hash
    assert exports[0]["metadata"]["fact_snapshot_sha256"] == source_hash
