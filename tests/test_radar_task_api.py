from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from sleepagent.radar_agent.api.http import (
    RADAR_AGENT_API_KEY_ENV,
    RADAR_AGENT_LLM_RETRY_ENV,
    RADAR_AGENT_LLM_TIMEOUT_SECONDS_ENV,
    RADAR_AGENT_RUNTIME_MODE_ENV,
    RADAR_AGENT_DEV_MODE_ENV,
    RadarApiRuntime,
    RadarTaskCreateRequest,
    _model_router,
    reset_radar_api_runtime_for_tests,
)
from sleepagent.radar_agent.dynamic import GoalType
from sleepagent.radar_agent.product_agent import (
    CommunicationDraft,
    DeterministicCommitController,
    EpisodeReceipt,
    EpisodeStatus,
    ExecutionMode,
    PendingConfirmationTarget,
    PendingUserInputTarget,
    ProductEpisodeRunResult,
    stable_hash,
)
from sleepagent.radar_agent.replay import get_replay_scenario, replay_scenario_ids
from sleepagent.radar_agent.persistence import (
    RadarDataAuthorization,
    RadarSubject,
    RadarUserRoleBinding,
)


API_KEY = "radar-task-api-test-key"
ACTOR_ID = "family-api-user"


@pytest.fixture(autouse=True)
def _fresh_runtime(monkeypatch):
    monkeypatch.setenv(RADAR_AGENT_API_KEY_ENV, API_KEY)
    reset_radar_api_runtime_for_tests()


def _headers(*, actor_id: str = ACTOR_ID, role: str = "family") -> dict[str, str]:
    return {
        "x-api-key": API_KEY,
        "x-actor-id": actor_id,
        "x-actor-role": role,
    }


def _create(client: TestClient, scenario: str = "normal_night", key: str = "night-1"):
    return client.post(
        "/radar-agent/tasks",
        headers={"x-api-key": API_KEY, "Idempotency-Key": key},
        json={
            "runtime_kind": "legacy_fixed",
            "scenario": scenario,
            "actor_id": ACTOR_ID,
            "role": "family",
        },
    )


def test_task_api_requires_auth_and_creation_is_idempotent() -> None:
    with TestClient(app) as client:
        assert client.post("/radar-agent/tasks", json={}).status_code == 401
        first = _create(client)
        second = _create(client)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["task"]["task_id"] == first.json()["task"]["task_id"]


def test_all_seven_replay_scenarios_can_create_tasks() -> None:
    with TestClient(app) as client:
        responses = []
        for scenario in replay_scenario_ids():
            created = _create(client, scenario=scenario, key=f"scenario:{scenario}")
            task_id = created.json()["task"]["task_id"]
            responses.append(
                client.post(f"/radar-agent/tasks/{task_id}/run", headers=_headers())
            )

    assert len(responses) == 7
    assert all(response.status_code == 200 for response in responses)
    assert [response.json()["risk_level"] for response in responses] == [
        get_replay_scenario(scenario).expected.risk_level.value
        for scenario in replay_scenario_ids()
    ]
    assert [response.json()["replay_scenario"]["scenario_id"] for response in responses] == list(
        replay_scenario_ids()
    )


def test_run_history_artifacts_sse_reconnect_and_grounded_chat() -> None:
    with TestClient(app) as client:
        created = _create(client, scenario="frequent_out_of_bed")
        task_id = created.json()["task"]["task_id"]
        run = client.post(f"/radar-agent/tasks/{task_id}/run", headers=_headers())
        history = client.get(f"/radar-agent/tasks/{task_id}/events", headers=_headers())
        detail = client.get(f"/radar-agent/tasks/{task_id}", headers=_headers())
        ledger_before_chat = next(
            item["evidence_ledger"]
            for item in detail.json()["artifacts"]
            if item["evidence_ledger"] is not None
        )
        cursor = history.json()[-3]["sequence"]
        stream = client.get(
            f"/radar-agent/tasks/{task_id}/stream",
            headers={**_headers(), "Last-Event-ID": str(cursor)},
        )
        chat = client.post(
            "/radar-agent/chat",
            headers=_headers(),
            json={
                "task_id": task_id,
                "message": "昨晚为什么离床变多？",
                "actor_id": ACTOR_ID,
                "actor_role": "family",
                "role": "doctor",
            },
        )
        detail_after_chat = client.get(
            f"/radar-agent/tasks/{task_id}", headers=_headers()
        )

    assert run.status_code == 200
    assert run.json()["task"]["status"] == "completed"
    assert history.status_code == 200
    assert [event["sequence"] for event in history.json()] == list(
        range(1, len(history.json()) + 1)
    )
    assert detail.json()["artifacts"]
    assert any(item["artifact_type"] == "evidence_ledger" for item in detail.json()["artifacts"])
    assert stream.status_code == 200
    assert f"id: {cursor}" not in stream.text
    assert f"id: {cursor + 1}" in stream.text
    assert chat.status_code == 200
    assert chat.json()["role"] == "doctor"
    assert chat.json()["facts_mutated"] is False
    assert chat.json()["evidence_refs"]
    assert chat.json()["rag_citation_refs"]
    ledger_after_chat = next(
        item["evidence_ledger"]
        for item in detail_after_chat.json()["artifacts"]
        if item["evidence_ledger"] is not None
    )
    assert ledger_after_chat == ledger_before_chat


def test_task_permissions_and_confirmation_role_are_enforced() -> None:
    with TestClient(app) as client:
        created = _create(client, scenario="escalate_candidate")
        task_id = created.json()["task"]["task_id"]
        run = client.post(f"/radar-agent/tasks/{task_id}/run", headers=_headers())
        forbidden = client.get(
            f"/radar-agent/tasks/{task_id}",
            headers=_headers(actor_id="other-family-user"),
        )
        confirmation = run.json()["confirmations"][0]
        wrong_role = client.post(
            f"/radar-agent/tasks/{task_id}/confirm",
            headers={"x-api-key": API_KEY},
            json={
                "confirmation_id": confirmation["confirmation_id"],
                "approved": True,
                "actor_id": ACTOR_ID,
                "actor_role": "doctor",
            },
        )
        resolved = client.post(
            f"/radar-agent/tasks/{task_id}/confirm",
            headers={"x-api-key": API_KEY},
            json={
                "confirmation_id": confirmation["confirmation_id"],
                "approved": True,
                "actor_id": ACTOR_ID,
                "actor_role": "family",
            },
        )

    assert forbidden.status_code == 403
    assert wrong_role.status_code == 403
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "approved"


def test_radar_llm_timeout_and_retry_are_configurable(monkeypatch) -> None:
    monkeypatch.setenv(RADAR_AGENT_LLM_TIMEOUT_SECONDS_ENV, "12.5")
    monkeypatch.setenv(RADAR_AGENT_LLM_RETRY_ENV, "2")
    runtime = RadarApiRuntime()

    router = _model_router(runtime.service, "task-config-probe")

    assert router.client.config.timeout == 12.5
    assert router.client.config.retry == 2


def test_product_runtime_mode_routes_task_and_chat_only_to_product_runner(
    monkeypatch,
) -> None:
    monkeypatch.setenv(RADAR_AGENT_RUNTIME_MODE_ENV, "product")
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "true")
    runner = RecordingProductRunner()
    reset_radar_api_runtime_for_tests(
        product_runner=runner,
    )
    with TestClient(app) as client:
        created = client.post(
            "/radar-agent/tasks",
            headers={
                **_headers(),
                "Idempotency-Key": "product-task",
            },
            json={
                "goal_type": "night_review",
                "question": "昨晚睡得怎么样？",
            },
        )
        task_id = created.json()["task"]["task_id"]
        run = client.post(
            f"/radar-agent/tasks/{task_id}/run",
            headers=_headers(),
        )
        chat = client.post(
            "/radar-agent/chat",
            headers=_headers(),
            json={
                "task_id": task_id,
                "message": "今晚要注意什么？",
                "actor_id": ACTOR_ID,
                "actor_role": "family",
                "role": "family",
            },
        )

    detail = run.json()
    assert created.status_code == 200
    assert run.status_code == 200
    assert chat.status_code == 200
    assert detail["task"]["runtime_kind"] == "product_episode"
    assert detail["task"]["runtime_contract_version"] == "product-episode.v1"
    assert detail["task"]["status"] == "completed"
    assert detail["completion_receipt"] is not None
    assert any(
        item["artifact_type"] == "product_episode_result"
        for item in detail["artifacts"]
    )
    assert chat.json()["answer"].startswith("ProductEpisodeRunner")
    assert [item.episode_type.value for item in runner.requests] == [
        "morning_review",
        "grounded_dialogue",
    ]
    assert all(item.runtime_readiness_decisions for item in runner.requests)
    assert all(
        item.fact_snapshot.readiness_decision_refs
        for item in runner.requests
    )


def test_legacy_radar_agent_routes_are_hidden_outside_dev_mode(
    monkeypatch,
) -> None:
    monkeypatch.setenv(RADAR_AGENT_RUNTIME_MODE_ENV, "product")
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "false")
    runner = RecordingProductRunner()
    runtime = reset_radar_api_runtime_for_tests(product_runner=runner)
    runtime.store.save_subject(
        RadarSubject(
            subject_id="subject-product-api",
            display_name="Product Subject",
        )
    )
    runtime.store.save_role_binding(
        RadarUserRoleBinding(
            role_binding_id="binding-product-family",
            user_id=ACTOR_ID,
            subject_id="subject-product-api",
            role="family",
            display_name="Product Family",
            permissions=["process_health_data"],
        )
    )
    runtime.store.save_data_authorization(
        RadarDataAuthorization(
            authorization_id="authorization-product-api",
            subject_id="subject-product-api",
            granted_by_user_id="elder-product-api",
            granted_by_role="elder",
            scopes=["process_radar_summary"],
        )
    )
    with TestClient(app) as client:
        missing_binding = client.post(
            "/radar-agent/tasks",
            headers={
                **_headers(),
                "Idempotency-Key": "missing-product-binding",
            },
            json={"goal_type": "night_review"},
        )
        created = client.post(
            "/radar-agent/tasks",
            headers={
                **_headers(),
                "x-subject-id": "subject-product-api",
                "x-authorization-id": "authorization-product-api",
                "x-role-binding-ids": "binding-product-family",
                "Idempotency-Key": "bound-product-task",
            },
            json={"goal_type": "night_review"},
        )

    assert missing_binding.status_code == 404
    assert created.status_code == 404
    assert runner.requests == []


def test_product_confirmation_resumes_frozen_episode_and_records_execution(
    monkeypatch,
) -> None:
    monkeypatch.setenv(RADAR_AGENT_RUNTIME_MODE_ENV, "product")
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "true")
    runner = ConfirmationProductRunner()
    reset_radar_api_runtime_for_tests(product_runner=runner)
    with TestClient(app) as client:
        created = client.post(
            "/radar-agent/tasks",
            headers={
                **_headers(),
                "Idempotency-Key": "product-confirmation",
            },
            json={
                "goal_type": "night_review",
                "question": "请形成一个低负担照护行动。",
            },
        )
        task_id = created.json()["task"]["task_id"]
        first = client.post(
            f"/radar-agent/tasks/{task_id}/run",
            headers=_headers(),
        )
        pending = first.json()["confirmations"][0]
        family_denied = client.post(
            f"/radar-agent/tasks/{task_id}/confirm",
            headers=_headers(),
            json={
                "confirmation_id": pending["confirmation_id"],
                "approved": True,
                "actor_id": "ignored-body-family",
                "actor_role": "elder",
            },
        )
        resolved = client.post(
            f"/radar-agent/tasks/{task_id}/confirm",
            headers=_headers(actor_id="elder-user", role="elder"),
            json={
                "confirmation_id": pending["confirmation_id"],
                "approved": True,
                "actor_id": "untrusted-body-actor",
                "actor_role": "doctor",
            },
        )
        detail = client.get(
            f"/radar-agent/tasks/{task_id}",
            headers=_headers(),
        )

    assert first.status_code == 200
    assert first.json()["task"]["status"] == "waiting_for_confirmation"
    assert first.json()["decisions"][0]["risk_level"] == "R2"
    assert first.json()["decisions"][0]["requirements"][0]["role"] == "elder"
    assert family_denied.status_code == 409
    assert all(
        item["artifact_type"] != "_product_episode_checkpoint"
        for item in first.json()["artifacts"]
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "approved"
    assert resolved.json()["execution_status"] == "completed"
    assert detail.json()["task"]["status"] == "completed"
    assert detail.json()["decisions"][0]["status"] == "committed"
    assert len(runner.requests) == 1
    assert runner.confirmation_commits == 1


def test_product_user_fact_resumes_frozen_episode_with_reviewed_answer(
    monkeypatch,
) -> None:
    monkeypatch.setenv(RADAR_AGENT_RUNTIME_MODE_ENV, "product")
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "true")
    runner = UserInputProductRunner()
    reset_radar_api_runtime_for_tests(product_runner=runner)
    with TestClient(app) as client:
        created = client.post(
            "/radar-agent/tasks",
            headers={
                **_headers(),
                "Idempotency-Key": "product-user-input",
            },
            json={"goal_type": "night_review"},
        )
        task_id = created.json()["task"]["task_id"]
        first = client.post(
            f"/radar-agent/tasks/{task_id}/run",
            headers=_headers(),
        )
        pending = first.json()["user_input_requests"][0]
        answered = client.post(
            f"/radar-agent/tasks/{task_id}/user-input",
            headers=_headers(),
            json={
                "request_id": pending["request_id"],
                "answer": "是，昨晚比平时晚睡。",
            },
        )
        detail = client.get(
            f"/radar-agent/tasks/{task_id}",
            headers=_headers(),
        )

    assert first.json()["task"]["status"] == "waiting_for_user_input"
    assert answered.status_code == 202
    assert detail.json()["task"]["status"] == "completed"
    assert len(runner.requests) == 2
    response = runner.requests[1].user_fact_responses[0]
    assert response.request_id == runner.request_id
    assert response.answer == "是，昨晚比平时晚睡。"
    assert response.actor_id == ACTOR_ID
    assert response.actor_role == "family"
    assert response.source_ref.startswith("authorized_observer_report:")
    assert (
        runner.requests[1].fact_snapshot.fact_snapshot_hash
        == runner.requests[0].fact_snapshot.fact_snapshot_hash
    )


def test_product_runtime_mode_rejects_legacy_and_dynamic_agent_paths(
    monkeypatch,
) -> None:
    monkeypatch.setenv(RADAR_AGENT_RUNTIME_MODE_ENV, "product")
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "false")
    runtime = RadarApiRuntime(
        sqlite3.connect(":memory:", check_same_thread=False),
        product_runner=RecordingProductRunner(),
    )
    assert runtime.worker is None

    for runtime_kind in ("legacy_fixed", "dynamic_goal"):
        with pytest.raises(ValueError, match="disabled"):
            runtime.create_task(
                RadarTaskCreateRequest(
                    runtime_kind=runtime_kind,
                    goal_type=(
                        GoalType.NIGHT_REVIEW
                        if runtime_kind == "dynamic_goal"
                        else None
                    ),
                ),
                idempotency_key=f"rejected:{runtime_kind}",
            )


class RecordingProductRunner:
    def __init__(self) -> None:
        self.commit_controller = DeterministicCommitController()
        self.requests = []

    def run(self, request) -> ProductEpisodeRunResult:
        self.requests.append(request)
        receipt = EpisodeReceipt(
            episode_id=request.episode_id,
            episode_type=request.episode_type,
            receipt_revision=1,
            terminal=True,
            execution_mode=ExecutionMode.INTELLIGENT,
            status=EpisodeStatus.COMPLETE,
            goal_achieved=True,
            fact_snapshot_id=request.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=request.fact_snapshot.fact_snapshot_hash,
            source_scope=request.fact_snapshot.source_scope,
            final_episode_state_revision=1,
            trace_ref=f"trace:{request.episode_id}",
        )
        return ProductEpisodeRunResult(
            registry_hash=stable_hash("recording-product-runner"),
            receipt=receipt,
            publication=CommunicationDraft(
                draft_id=f"draft:{request.episode_id}",
                audience_role=request.audience_role,
                text=f"ProductEpisodeRunner 已处理 {request.episode_id}",
                context_notice="测试四角色产品路径。",
            ),
            publication_delivered=True,
        )


class ConfirmationProductRunner(RecordingProductRunner):
    candidate_id = "care-candidate-api"
    target_hash = stable_hash(
        {
            "candidate_id": candidate_id,
            "payload": {"duration_days": 3, "burden": "low"},
        }
    )

    def run(self, request) -> ProductEpisodeRunResult:
        self.requests.append(request)
        confirmed = request.care_confirmation is not None
        receipt = EpisodeReceipt(
            episode_id=request.episode_id,
            episode_type=request.episode_type,
            receipt_revision=2 if confirmed else 1,
            terminal=confirmed,
            execution_mode=ExecutionMode.INTELLIGENT,
            status=(
                EpisodeStatus.COMPLETE
                if confirmed
                else EpisodeStatus.WAITING_CONFIRMATION
            ),
            goal_achieved=confirmed,
            fact_snapshot_id=request.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=request.fact_snapshot.fact_snapshot_hash,
            source_scope=request.fact_snapshot.source_scope,
            final_episode_state_revision=1,
            trace_ref=f"trace:{request.episode_id}",
        )
        pending = []
        if not confirmed:
            pending = [
                PendingConfirmationTarget(
                    confirmation_id="runner-care-confirmation",
                    target_kind="care",
                    candidate_id=self.candidate_id,
                    candidate_hash=self.target_hash,
                    actor_id=request.fact_snapshot.binding.actor_id,
                    subject_id=request.fact_snapshot.binding.subject_id,
                    action_scope="activate_care",
                    reason="确认建立这个低负担照护行动。",
                    expires_at=datetime.now(timezone.utc)
                    + timedelta(minutes=20),
                )
            ]
        return ProductEpisodeRunResult(
            registry_hash=stable_hash("confirmation-product-runner"),
            receipt=receipt,
            publication=CommunicationDraft(
                draft_id=f"draft:{request.episode_id}",
                audience_role=request.audience_role,
                text="ProductEpisodeRunner 已形成目标绑定的照护候选。",
                context_notice="确认只对当前候选及其 hash 有效。",
            ),
            publication_delivered=True,
            committed_care_candidate_id=(
                self.candidate_id if confirmed else None
            ),
            pending_confirmations=pending,
        )

    def commit_frozen_confirmations(
        self,
        *,
        request,
        frozen_result,
        confirmations,
        declined_confirmation_ids=(),
    ) -> ProductEpisodeRunResult:
        self.confirmation_commits = getattr(self, "confirmation_commits", 0) + 1
        assert request.fact_snapshot.fact_snapshot_hash == (
            frozen_result.receipt.fact_snapshot_hash
        )
        assert set(confirmations) == {"runner-care-confirmation"}
        token = confirmations["runner-care-confirmation"]
        assert token.candidate_hash == self.target_hash
        receipt = frozen_result.receipt.model_copy(
            update={
                "receipt_revision": frozen_result.receipt.receipt_revision + 1,
                "terminal": True,
                "status": EpisodeStatus.COMPLETE,
                "goal_achieved": True,
            }
        )
        return frozen_result.model_copy(
            update={
                "receipt": receipt,
                "committed_care_candidate_id": self.candidate_id,
                "pending_confirmations": [],
                "declined_confirmation_ids": list(
                    declined_confirmation_ids
                ),
            }
        )


class UserInputProductRunner(RecordingProductRunner):
    request_id = "product-user-fact-bedtime"

    def run(self, request) -> ProductEpisodeRunResult:
        self.requests.append(request)
        answered = any(
            item.request_id == self.request_id
            for item in request.user_fact_responses
        )
        receipt = EpisodeReceipt(
            episode_id=request.episode_id,
            episode_type=request.episode_type,
            receipt_revision=2 if answered else 1,
            terminal=answered,
            execution_mode=ExecutionMode.INTELLIGENT,
            status=(
                EpisodeStatus.COMPLETE
                if answered
                else EpisodeStatus.WAITING_USER
            ),
            goal_achieved=answered,
            fact_snapshot_id=request.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=request.fact_snapshot.fact_snapshot_hash,
            source_scope=request.fact_snapshot.source_scope,
            final_episode_state_revision=1,
            trace_ref=f"trace:{request.episode_id}",
        )
        return ProductEpisodeRunResult(
            registry_hash=stable_hash("user-input-product-runner"),
            receipt=receipt,
            publication=(
                CommunicationDraft(
                    draft_id=f"draft:{request.episode_id}",
                    audience_role=request.audience_role,
                    text="ProductEpisodeRunner 已使用补充事实完成判断。",
                    context_notice="补充内容保持 user_reported 语义。",
                )
                if answered
                else None
            ),
            publication_delivered=answered,
            pending_user_input=(
                None
                if answered
                else PendingUserInputTarget(
                    request_id=self.request_id,
                    question_text="昨晚是否比平时更晚入睡？",
                    why_needed="用于区分作息变化与设备记录变化。",
                    decision_scope="morning_review",
                    target_role="family",
                    source_agent="evidence_reasoning",
                    expires_at=datetime.now(timezone.utc)
                    + timedelta(minutes=20),
                )
            ),
        )
