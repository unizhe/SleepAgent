from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from sleepagent.radar_agent.api.http import (
    RADAR_AGENT_API_KEY_ENV,
    RADAR_AGENT_DEV_MODE_ENV,
    RadarTaskCreateRequest,
    reset_radar_api_runtime_for_tests,
)
from sleepagent.radar_agent.runtime import (
    InvalidTaskTransition,
    RadarAgentTask,
)
from sleepagent.radar_agent.persistence import RadarSubject
from sleepagent.radar_agent.provider import ReplayRadarProvider


API_KEY = "runtime-cutover-api-key"
ACTOR_ID = "cutover-family-user"


@pytest.fixture(autouse=True)
def _runtime(monkeypatch):
    monkeypatch.setenv(RADAR_AGENT_API_KEY_ENV, API_KEY)
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "true")
    return reset_radar_api_runtime_for_tests()


def _headers() -> dict[str, str]:
    return {
        "x-api-key": API_KEY,
        "x-actor-id": ACTOR_ID,
        "x-actor-role": "family",
    }


@pytest.mark.parametrize("runtime_kind", ["legacy_fixed", "dynamic_goal"])
@pytest.mark.parametrize("development", ["true", "false"])
def test_historical_runtime_kinds_cannot_be_created(
    runtime_kind: str,
    development: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, development)
    runtime = reset_radar_api_runtime_for_tests()
    with TestClient(app) as client:
        response = client.post(
            "/radar-agent/tasks",
            headers=_headers(),
            json={"runtime_kind": runtime_kind},
        )

    assert response.status_code == (422 if development == "true" else 404)
    with pytest.raises(ValueError, match="product_episode"):
        RadarTaskCreateRequest(runtime_kind=runtime_kind)
    assert runtime.store.list_tasks() == []
    assert not hasattr(runtime, "worker")


@pytest.mark.parametrize(
    ("runtime_kind", "contract_version"),
    [
        ("legacy_fixed", "radar-legacy.v1"),
        ("dynamic_goal", "radar-dynamic.v1"),
    ],
)
def test_task_service_rejects_historical_runtime_creation(
    runtime_kind: str,
    contract_version: str,
) -> None:
    runtime = reset_radar_api_runtime_for_tests()
    device = ReplayRadarProvider().list_devices()[0]

    with pytest.raises(InvalidTaskTransition, match="read-only"):
        runtime.service.create_task(
            subject_id="historical-subject",
            radar_device_id=device.radar_device_id,
            runtime_kind=runtime_kind,
            runtime_contract_version=contract_version,
        )

    assert runtime.store.list_tasks() == []


@pytest.mark.parametrize(
    ("runtime_kind", "contract_version"),
    [
        ("legacy_fixed", "radar-legacy.v1"),
        ("dynamic_goal", "radar-dynamic.v1"),
    ],
)
def test_historical_tasks_are_readable_but_all_agent_mutations_fail_closed(
    runtime_kind: str,
    contract_version: str,
) -> None:
    runtime = reset_radar_api_runtime_for_tests()
    runtime.store.save_subject(
        RadarSubject(
            subject_id="historical-subject",
            display_name="Historical Subject",
        )
    )
    device = ReplayRadarProvider().list_devices()[0].model_copy(
        update={"bound_subject_id": "historical-subject"}
    )
    runtime.store.save_device(device)
    task = RadarAgentTask(
        task_id=f"historical:{runtime_kind}",
        trace_id=f"trace:{runtime_kind}",
        subject_id="historical-subject",
        radar_device_id=device.radar_device_id,
        role="family",
        requested_by_user_id=ACTOR_ID,
        scenario="normal_night",
        runtime_kind=runtime_kind,
        runtime_contract_version=contract_version,
        node_status=(
            {"legacy-node": "pending"}
            if runtime_kind == "legacy_fixed"
            else {}
        ),
        goal_payload=(
            {"goal_type": "night_review"}
            if runtime_kind == "dynamic_goal"
            else None
        ),
    )
    runtime.store.save_task(task)

    with TestClient(app) as client:
        detail = client.get(
            f"/radar-agent/tasks/{task.task_id}", headers=_headers()
        )
        run = client.post(
            f"/radar-agent/tasks/{task.task_id}/run", headers=_headers()
        )
        chat = client.post(
            "/radar-agent/chat",
            headers=_headers(),
            json={
                "task_id": task.task_id,
                "message": "历史任务可以继续吗？",
                "actor_id": ACTOR_ID,
                "actor_role": "family",
                "role": "family",
            },
        )
        user_input = client.post(
            f"/radar-agent/tasks/{task.task_id}/user-input",
            headers=_headers(),
            json={"request_id": "historical-request", "answer": "no"},
        )
        confirmation = client.post(
            f"/radar-agent/tasks/{task.task_id}/confirm",
            headers=_headers(),
            json={
                "confirmation_id": "historical-confirmation",
                "approved": True,
                "actor_id": ACTOR_ID,
                "actor_role": "family",
            },
        )

    assert detail.status_code == 200
    assert detail.json()["task"]["runtime_kind"] == runtime_kind
    assert {run.status_code, chat.status_code, user_input.status_code, confirmation.status_code} == {409}
    persisted = runtime.service.get_task(task.task_id)
    assert persisted.status.value == "created"
    assert runtime.service.list_events(task.task_id) == []
