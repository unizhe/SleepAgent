from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sleepagent.radar_agent.api.http import (
    RADAR_AGENT_API_KEY_ENV,
    RADAR_AGENT_DEV_MODE_ENV,
    reset_radar_api_runtime_for_tests,
    router,
)
from sleepagent.radar_agent.persistence import RadarSubject


API_KEY = "history-reader-api-key"
ACTOR_ID = "history-reader-family"
TASK_ID = "historical-api-task"
APP = FastAPI()
APP.include_router(router)


@pytest.fixture()
def historical_runtime(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(RADAR_AGENT_API_KEY_ENV, API_KEY)
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "true")
    runtime = reset_radar_api_runtime_for_tests()
    runtime.store.save_subject(
        RadarSubject(
            subject_id="historical-api-subject",
            display_name="Historical API Subject",
        )
    )
    task = {
        "task_id": TASK_ID,
        "trace_id": "trace:historical-api-task",
        "subject_id": "historical-api-subject",
        "radar_device_id": "historical-api-device",
        "role": "family",
        "requested_by_user_id": ACTOR_ID,
        "scenario": "historical",
        "runtime_kind": "retired-test-runtime",
        "runtime_contract_version": "retired-test.v1",
        "status": "completed",
    }
    runtime.store.connection.execute(
        """
        INSERT INTO radar_tasks (
          task_id, trace_id, subject_id, radar_device_id, role, scenario,
          status, task_json, created_at, updated_at, runtime_kind,
          runtime_contract_version, task_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            TASK_ID,
            task["trace_id"],
            task["subject_id"],
            task["radar_device_id"],
            task["role"],
            task["scenario"],
            task["status"],
            json.dumps(task),
            "2026-08-09T00:00:00+00:00",
            "2026-08-09T00:00:00+00:00",
            task["runtime_kind"],
            task["runtime_contract_version"],
            1,
        ),
    )
    event = {
        "event_id": "historical-api-event",
        "task_id": TASK_ID,
        "trace_id": task["trace_id"],
        "sequence": 1,
        "event_type": "historical.recorded",
        "message": "Persisted before the canonical cutover.",
        "payload": {"future_field": {"preserved": True}},
        "created_at": "2026-08-09T00:00:01+00:00",
    }
    runtime.store.connection.execute(
        """
        INSERT INTO radar_task_events (
          event_id, task_id, trace_id, sequence, event_type,
          event_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["event_id"],
            TASK_ID,
            event["trace_id"],
            event["sequence"],
            event["event_type"],
            json.dumps(event),
            event["created_at"],
        ),
    )
    runtime.store.connection.execute(
        """
        INSERT INTO radar_human_confirmations (
          confirmation_id, task_id, action_type, requested_role, status,
          confirmation_json, created_at, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "historical-api-confirmation",
            TASK_ID,
            "share_report",
            "family",
            "approved",
            json.dumps(
                {
                    "confirmation_id": "historical-api-confirmation",
                    "future_marker": "confirmation",
                }
            ),
            "2026-08-09T00:00:02+00:00",
            "2026-08-09T00:00:03+00:00",
        ),
    )
    runtime.store.connection.execute(
        """
        INSERT INTO radar_task_artifact_versions (
          artifact_version_id, artifact_id, task_id, artifact_type,
          version_number, artifact_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "historical-api-artifact-version",
            "historical-api-artifact",
            TASK_ID,
            "workflow_run",
            1,
            json.dumps(
                {
                    "artifact_version_id": "historical-api-artifact-version",
                    "future_marker": "artifact",
                }
            ),
            "2026-08-09T00:00:02+00:00",
        ),
    )
    runtime.store.connection.execute(
        """
        INSERT INTO radar_a2a_messages (
          message_id, task_id, target_task_id, sender, receiver, intent,
          risk_level, collaboration_round, routed_by, shared_artifact_type,
          message_status, message_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "historical-api-message",
            TASK_ID,
            None,
            "evidence_analysis",
            "orchestrator",
            "review",
            "low",
            1,
            "orchestrator",
            None,
            "delivered",
            json.dumps(
                {
                    "message_id": "historical-api-message",
                    "future_marker": "collaboration",
                }
            ),
            "2026-08-09T00:00:02+00:00",
        ),
    )
    runtime.store.connection.execute(
        """
        INSERT INTO radar_conflict_records (
          conflict_id, task_id, final_status, requires_human_confirmation,
          conflict_json, decided_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "historical-api-conflict",
            TASK_ID,
            "resolved",
            0,
            json.dumps(
                {
                    "conflict_id": "historical-api-conflict",
                    "future_marker": "conflict",
                }
            ),
            "2026-08-09T00:00:02+00:00",
        ),
    )
    runtime.store.connection.commit()
    return runtime


def _headers() -> dict[str, str]:
    return {
        "x-api-key": API_KEY,
        "x-actor-id": ACTOR_ID,
        "x-actor-role": "family",
    }


def test_historical_api_reads_task_events_and_trace_without_writes(
    historical_runtime,
) -> None:
    changes_before = historical_runtime.store.connection.total_changes
    with TestClient(APP) as client:
        detail = client.get(f"/radar-agent/tasks/{TASK_ID}", headers=_headers())
        events = client.get(
            f"/radar-agent/tasks/{TASK_ID}/events",
            headers=_headers(),
        )
        trace = client.get(
            f"/radar-agent/tasks/{TASK_ID}/developer-trace",
            headers=_headers(),
        )

    assert detail.status_code == 200
    assert detail.json()["task"]["runtime_kind"] == "retired-test-runtime"
    assert events.status_code == 200
    assert events.json()[0]["payload"]["future_field"] == {"preserved": True}
    assert trace.status_code == 200
    assert trace.json()["history"]["plans"] == []
    assert trace.json()["events"][0]["event_type"] == "historical.recorded"
    assert trace.json()["confirmations"][0]["future_marker"] == "confirmation"
    assert trace.json()["artifacts"][0]["future_marker"] == "artifact"
    assert trace.json()["a2a"][0]["future_marker"] == "collaboration"
    assert trace.json()["conflicts"][0]["future_marker"] == "conflict"
    assert historical_runtime.store.connection.total_changes == changes_before


def test_historical_api_rejects_every_agent_mutation(historical_runtime) -> None:
    with TestClient(APP) as client:
        responses = (
            client.post(f"/radar-agent/tasks/{TASK_ID}/run", headers=_headers()),
            client.post(
                "/radar-agent/chat",
                headers=_headers(),
                json={
                    "task_id": TASK_ID,
                    "message": "Can this retired task continue?",
                    "actor_id": ACTOR_ID,
                    "role": "family",
                },
            ),
            client.post(
                f"/radar-agent/tasks/{TASK_ID}/user-input",
                headers=_headers(),
                json={"request_id": "historical-request", "answer": "no"},
            ),
            client.post(
                f"/radar-agent/tasks/{TASK_ID}/confirm",
                headers=_headers(),
                json={
                    "confirmation_id": "historical-confirmation",
                    "approved": True,
                    "actor_id": ACTOR_ID,
                    "actor_role": "family",
                },
            ),
        )

    assert {response.status_code for response in responses} == {409}
    assert historical_runtime.store.history.read_task(TASK_ID).status == "completed"
