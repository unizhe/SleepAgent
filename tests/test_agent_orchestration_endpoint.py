from fastapi.testclient import TestClient

from backend.main import app
from sleepagent.agents import LangGraphUnavailableError, run_sleep_agent_orchestration
from sleepagent.schemas import AgentOrchestrationMode


def test_agent_orchestration_endpoint_returns_linear_result() -> None:
    client = TestClient(app)

    response = client.get(
        "/agent/orchestrate",
        params={
            "record_id": "agent-record",
            "subject_id": "agent-subject",
            "duration_hours": 0.5,
            "seed": 41,
            "user_question": "AHI 是什么？",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["orchestration_mode"] == "linear"
    assert payload["analysis"]["metadata"]["record_id"] == "agent-record"
    assert payload["analysis"]["metadata"]["patient"]["subject_id"] == "agent-subject"
    assert payload["report"]["summary"]["record_id"] == "agent-record"
    assert payload["dialogue"]["referenced_record_id"] == "agent-record"
    assert "AHI" in payload["dialogue"]["assistant_response"]
    assert [step["step_name"] for step in payload["steps"]] == [
        "sleep_analysis",
        "report",
        "dialogue",
    ]


def test_agent_orchestration_endpoint_skips_dialogue_without_question() -> None:
    client = TestClient(app)

    response = client.get(
        "/agent/orchestrate",
        params={"duration_hours": 0.5, "seed": 42},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["dialogue"] is None
    assert payload["steps"][-1]["step_name"] == "skip_dialogue"
    assert payload["steps"][-1]["status"] == "skipped"


def test_agent_orchestration_post_endpoint_accepts_request_body() -> None:
    client = TestClient(app)

    response = client.post(
        "/agent/orchestrate",
        json={
            "record_id": "agent-post-record",
            "subject_id": "agent-post-subject",
            "duration_hours": 0.5,
            "seed": 45,
            "user_question": "请解释报告建议。",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["orchestration_mode"] == "linear"
    assert payload["analysis"]["metadata"]["record_id"] == "agent-post-record"
    assert payload["report"]["summary"]["subject_id"] == "agent-post-subject"
    assert payload["dialogue"]["referenced_record_id"] == "agent-post-record"


def test_agent_orchestration_post_endpoint_accepts_dialogue_context() -> None:
    client = TestClient(app)

    response = client.post(
        "/agent/orchestrate",
        json={
            "record_id": "agent-context-record",
            "duration_hours": 0.5,
            "seed": 47,
            "user_question": "有什么改善建议？",
            "dialogue_context": {
                "history_summary": "最近三晚睡眠效率下降",
                "user_preferences": ["给家属看的解释"],
                "recent_questions": ["为什么白天困"],
            },
        },
    )

    assert response.status_code == 200
    dialogue = response.json()["dialogue"]
    assert dialogue["context_used"] is True
    assert "最近三晚睡眠效率下降" in dialogue["assistant_response"]
    assert "给家属看的解释" in dialogue["assistant_response"]


def test_agent_orchestration_endpoint_handles_urgent_symptom_safely() -> None:
    client = TestClient(app)

    response = client.post(
        "/agent/orchestrate",
        json={
            "duration_hours": 0.5,
            "seed": 46,
            "user_question": "现在胸痛、严重呼吸困难，还有意识异常，是不是确诊了？",
        },
    )

    assert response.status_code == 200
    dialogue = response.json()["dialogue"]
    assert dialogue["safety_flags"] == ["urgent_symptom_safety_boundary"]
    assert "急诊" in dialogue["assistant_response"]
    assert "不能替代医生" in dialogue["assistant_response"]
    assert "确诊" not in dialogue["assistant_response"]


def test_agent_orchestration_endpoint_rejects_invalid_duration() -> None:
    client = TestClient(app)

    response = client.get("/agent/orchestrate", params={"duration_hours": 0.1})

    assert response.status_code == 422


def test_agent_orchestration_endpoint_keeps_deepseek_default_off(monkeypatch) -> None:
    client = TestClient(app)

    def fail_deepseek_call(*args, **kwargs):
        raise AssertionError("DeepSeek should remain opt-in for Agent endpoint")

    monkeypatch.setattr(
        "sleepagent.agents.report_agent.generate_sleep_report_with_deepseek_fallback",
        fail_deepseek_call,
    )

    response = client.get(
        "/agent/orchestrate",
        params={"duration_hours": 0.5, "seed": 43},
    )

    assert response.status_code == 200
    assert response.json()["report"]["elder_report"]


def test_agent_orchestration_endpoint_can_opt_into_langgraph(monkeypatch) -> None:
    client = TestClient(app)
    calls = []

    def fake_langgraph_orchestration(request):
        calls.append(request.record_id)
        result = run_sleep_agent_orchestration(request)
        return result.model_copy(
            update={"orchestration_mode": AgentOrchestrationMode.LANGGRAPH}
        )

    monkeypatch.setattr(
        "backend.main.run_sleep_agent_langgraph_orchestration",
        fake_langgraph_orchestration,
    )

    response = client.get(
        "/agent/orchestrate",
        params={
            "record_id": "langgraph-endpoint-record",
            "duration_hours": 0.5,
            "seed": 44,
            "use_langgraph": "true",
        },
    )

    assert response.status_code == 200
    assert calls == ["langgraph-endpoint-record"]
    assert response.json()["orchestration_mode"] == "langgraph"


def test_agent_orchestration_endpoint_reports_missing_langgraph(monkeypatch) -> None:
    client = TestClient(app)

    def raise_missing_langgraph(request):
        raise LangGraphUnavailableError("missing langgraph")

    monkeypatch.setattr(
        "backend.main.run_sleep_agent_langgraph_orchestration",
        raise_missing_langgraph,
    )

    response = client.get(
        "/agent/orchestrate",
        params={"duration_hours": 0.5, "use_langgraph": "true"},
    )

    assert response.status_code == 503
    assert "LangGraph is not installed" in response.json()["detail"]
