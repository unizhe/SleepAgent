import subprocess
import sys

from scripts.run_agent_orchestration_smoke import (
    build_agent_orchestration_payload,
    run_agent_orchestration_smoke,
)
from sleepagent.agents import run_sleep_agent_orchestration
from sleepagent.schemas import SleepAgentEndpointRequest


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self.payload


class FakeHTTPClient:
    def __init__(self, response_payload: dict) -> None:
        self.response_payload = response_payload
        self.post_calls = []
        self.closed = False

    def post(self, url: str, *, json: dict) -> FakeResponse:
        self.post_calls.append({"url": url, "json": json})
        return FakeResponse(self.response_payload)

    def close(self) -> None:
        self.closed = True


def test_build_agent_orchestration_payload_includes_endpoint_controls() -> None:
    payload = build_agent_orchestration_payload(
        record_id="smoke-record",
        subject_id="smoke-subject",
        duration_hours=0.5,
        seed=52,
        abnormal_event_rate_per_hour=7.0,
        user_question="AHI 是什么？",
        history_summary="最近一周睡眠效率下降",
        use_deepseek_report=False,
        use_langgraph=True,
    )

    assert payload == {
        "record_id": "smoke-record",
        "subject_id": "smoke-subject",
        "duration_hours": 0.5,
        "seed": 52,
        "abnormal_event_rate_per_hour": 7.0,
        "user_question": "AHI 是什么？",
        "dialogue_context": {"history_summary": "最近一周睡眠效率下降"},
        "use_deepseek_report": False,
        "use_langgraph": True,
    }


def test_agent_orchestration_smoke_posts_and_summarizes_contract() -> None:
    request = SleepAgentEndpointRequest(
        record_id="smoke-record",
        subject_id="smoke-subject",
        duration_hours=0.5,
        seed=53,
        user_question="AHI 是什么？",
    )
    result = run_sleep_agent_orchestration(
        **request.model_dump(exclude={"use_langgraph"})
    )
    client = FakeHTTPClient(result.model_dump(mode="json"))

    summary = run_agent_orchestration_smoke(
        api_base_url="http://127.0.0.1:18000/",
        payload=request.model_dump(mode="json"),
        timeout_seconds=3.0,
        http_client=client,
    )

    assert client.post_calls == [
        {
            "url": "http://127.0.0.1:18000/agent/orchestrate",
            "json": request.model_dump(mode="json"),
        }
    ]
    assert client.closed is False
    assert summary["method"] == "POST"
    assert summary["record_id"] == "smoke-record"
    assert summary["subject_id"] == "smoke-subject"
    assert summary["orchestration_mode"] == "linear"
    assert summary["step_names"] == ["sleep_analysis", "report", "dialogue"]
    assert summary["dialogue_present"] is True
    assert "summary" in summary["report_contract_fields"]


def test_agent_orchestration_smoke_help_is_import_safe() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/run_agent_orchestration_smoke.py", "--help"],
        cwd=".",
        check=True,
        capture_output=True,
        text=True,
    )

    assert "/agent/orchestrate" in completed.stdout
