from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.legacy_main import app
from sleepagent.radar_agent.api.http import (
    RADAR_AGENT_API_KEY_ENV,
    RADAR_AGENT_DEV_MODE_ENV,
    RadarApiRuntime,
    RadarTaskCreateRequest,
    reset_radar_api_runtime_for_tests,
)
from sleepagent.radar_agent.product_agent.contracts import (
    CommunicationDraft,
    EpisodeReceipt,
    EpisodeStatus,
    ExecutionMode,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.governance import (
    DeterministicCommitController,
)
from sleepagent.radar_agent.product_agent.runtime_contracts import (
    CommitFrozenConfirmedAction,
    PendingConfirmationTarget,
    PendingUserInputTarget,
    ProductEpisodeRunResult,
)
from sleepagent.radar_agent.product_agent.hitl import HumanDecisionStatus
from sleepagent.radar_agent.product_agent.runtime_factory import (
    ProductRuntimeBundle,
    build_product_runtime_bundle_from_env,
)
from sleepagent.radar_agent.replay import get_replay_scenario, replay_scenario_ids
from sleepagent.radar_agent.persistence import (
    RadarDataAuthorization,
    RadarPersistenceStore,
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
        headers={**_headers(), "Idempotency-Key": key},
        json={
            "scenario": scenario,
            "actor_id": ACTOR_ID,
            "role": "family",
        },
    )


class _RecordingRuntimeBundleAdapter:
    """Keep the canonical graph while replacing its API-facing runner only."""

    def __init__(
        self,
        bundle: ProductRuntimeBundle,
        runner: RecordingProductRunner,
    ) -> None:
        self._bundle = bundle
        self.runner = runner
        runner.commit_controller = bundle.commit_controller
        runner.human_decisions = bundle.human_decisions

    def __getattr__(self, name: str):
        return getattr(self._bundle, name)


def _product_runtime_with_runner(
    runner: RecordingProductRunner,
    *,
    connection: sqlite3.Connection | None = None,
) -> _RecordingRuntimeBundleAdapter:
    persistence = RadarPersistenceStore.connect_sqlite(
        connection or sqlite3.connect(":memory:", check_same_thread=False)
    )
    bundle = build_product_runtime_bundle_from_env(
        persistence_store=persistence,
    )
    return _RecordingRuntimeBundleAdapter(bundle, runner)


def test_task_api_requires_auth_and_creation_is_idempotent() -> None:
    with TestClient(app) as client:
        assert client.post("/radar-agent/tasks", json={}).status_code == 401
        first = _create(client)
        second = _create(client)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["task"]["task_id"] == first.json()["task"]["task_id"]


def test_product_routes_task_and_chat_only_to_product_runner(
    monkeypatch,
) -> None:
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "true")
    runner = RecordingProductRunner()
    reset_radar_api_runtime_for_tests(
        product_runtime=_product_runtime_with_runner(runner),
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


def test_radar_agent_routes_are_hidden_outside_explicit_dev_transport(
    monkeypatch,
) -> None:
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "false")
    runner = RecordingProductRunner()
    runtime = reset_radar_api_runtime_for_tests(
        product_runtime=_product_runtime_with_runner(runner)
    )
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
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "true")
    runner = ConfirmationProductRunner()
    runtime = reset_radar_api_runtime_for_tests(
        product_runtime=_product_runtime_with_runner(runner)
    )
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
        checkpoint = runtime._latest_product_checkpoint(task_id)
        [checkpoint_target] = checkpoint.payload["pending_confirmations"]
        [frozen_target] = checkpoint.payload["result"]["pending_confirmations"]
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
    assert "active_grant" not in first.json()["decisions"][0]
    assert checkpoint_target["decision_id"]
    assert checkpoint_target["proposal_id"]
    assert checkpoint_target == frozen_target
    assert pending["decision_id"] == checkpoint_target["decision_id"]
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
    assert "active_grant" not in detail.json()["decisions"][0]
    assert len(runner.requests) == 1
    assert runner.confirmation_commits == 1


def test_product_user_fact_resumes_frozen_episode_with_reviewed_answer(
    monkeypatch,
) -> None:
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "true")
    runner = UserInputProductRunner()
    reset_radar_api_runtime_for_tests(
        product_runtime=_product_runtime_with_runner(runner)
    )
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


def test_create_contract_rejects_legacy_and_dynamic_agent_paths(
    monkeypatch,
) -> None:
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "false")
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    runtime = RadarApiRuntime(
        product_runtime=_product_runtime_with_runner(
            RecordingProductRunner(),
            connection=connection,
        ),
    )
    assert not hasattr(runtime, "worker")

    for runtime_kind in ("legacy_fixed", "dynamic_goal"):
        with pytest.raises(ValueError, match="product_episode"):
            RadarTaskCreateRequest(runtime_kind=runtime_kind)


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
        receipt = EpisodeReceipt(
            episode_id=request.episode_id,
            episode_type=request.episode_type,
            receipt_revision=1,
            terminal=False,
            execution_mode=ExecutionMode.INTELLIGENT,
            status=EpisodeStatus.WAITING_CONFIRMATION,
            goal_achieved=False,
            fact_snapshot_id=request.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=request.fact_snapshot.fact_snapshot_hash,
            source_scope=request.fact_snapshot.source_scope,
            final_episode_state_revision=1,
            trace_ref=f"trace:{request.episode_id}",
        )
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
            pending_confirmations=pending,
        )

    def commit_frozen_confirmations(
        self,
        command: CommitFrozenConfirmedAction,
    ) -> ProductEpisodeRunResult:
        request = command.request
        frozen_result = command.frozen_result
        self.confirmation_commits = getattr(self, "confirmation_commits", 0) + 1
        assert request.fact_snapshot.fact_snapshot_hash == (
            frozen_result.receipt.fact_snapshot_hash
        )
        [target] = frozen_result.pending_confirmations
        assert target.candidate_hash == self.target_hash
        assert target.decision_id is not None
        assert target.proposal_id is not None
        decision = self.human_decisions.get(target.decision_id)
        proposal = decision.proposal
        assert proposal.proposal_id == target.proposal_id
        capability = self.human_decisions.acquire_verified_capability(
            decision.decision_id,
            expected_proposal_id=proposal.proposal_id,
            expected_subject_id=proposal.subject_id,
            expected_target_id=proposal.target_id,
            expected_target_hash=proposal.target_hash,
            expected_action_scope=proposal.action_scope,
            expected_fact_snapshot_id=proposal.fact_snapshot_id,
            expected_fact_snapshot_hash=proposal.fact_snapshot_hash,
            expected_policy_version=proposal.policy_version,
            idempotency_key=f"api-test:{decision.decision_id}",
        )
        receipt = frozen_result.receipt.model_copy(
            update={
                "receipt_revision": frozen_result.receipt.receipt_revision + 1,
                "terminal": True,
                "status": EpisodeStatus.COMPLETE,
                "goal_achieved": True,
            }
        )
        committed = frozen_result.model_copy(
            update={
                "receipt": receipt,
                "committed_care_candidate_id": self.candidate_id,
                "pending_confirmations": [],
            }
        )
        self.human_decisions.record_execution_result(
            capability,
            status=HumanDecisionStatus.COMMITTED,
            receipt_ref=committed.receipt.trace_ref,
        )
        return committed


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
