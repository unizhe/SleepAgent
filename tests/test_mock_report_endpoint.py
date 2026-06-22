from fastapi.testclient import TestClient

from backend.main import app
from sleepagent.services import generate_mock_sleep_report


def test_mock_report_endpoint_returns_template_report() -> None:
    client = TestClient(app)

    response = client.get(
        "/mock-report",
        params={
            "record_id": "report-record",
            "subject_id": "report-subject",
            "duration_hours": 0.5,
            "seed": 12,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["record_id"] == "report-record"
    assert payload["summary"]["subject_id"] == "report-subject"
    assert payload["summary"]["total_recording_minutes"] == 30.0
    assert payload["summary"]["ahi"] >= 0
    assert payload["elder_report"]
    assert payload["professional_report"]
    assert payload["care_suggestions"]
    assert "不能替代医生诊断" in payload["medical_disclaimer"]


def test_mock_report_endpoint_rejects_invalid_duration() -> None:
    client = TestClient(app)

    response = client.get("/mock-report", params={"duration_hours": 0.1})

    assert response.status_code == 422


def test_mock_report_llm_endpoint_defaults_to_template_without_live_call(monkeypatch) -> None:
    client = TestClient(app)

    def fail_live_call(*args, **kwargs):
        raise AssertionError("DeepSeek fallback should be opt-in")

    monkeypatch.setattr(
        "backend.main.generate_sleep_report_with_deepseek_fallback",
        fail_live_call,
    )
    response = client.get(
        "/mock-report/llm",
        params={
            "record_id": "llm-record",
            "subject_id": "llm-subject",
            "duration_hours": 0.5,
            "seed": 12,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["record_id"] == "llm-record"
    assert payload["summary"]["subject_id"] == "llm-subject"
    assert "不能替代医生诊断" in payload["medical_disclaimer"]


def test_mock_report_llm_endpoint_can_opt_into_deepseek_fallback(monkeypatch) -> None:
    client = TestClient(app)
    calls = []

    def fake_deepseek_fallback(analysis):
        calls.append(analysis.metadata.record_id)
        return generate_mock_sleep_report(analysis)

    monkeypatch.setattr(
        "backend.main.generate_sleep_report_with_deepseek_fallback",
        fake_deepseek_fallback,
    )
    response = client.get(
        "/mock-report/llm",
        params={
            "record_id": "live-record",
            "duration_hours": 0.5,
            "seed": 12,
            "use_deepseek": "true",
        },
    )

    assert response.status_code == 200
    assert calls == ["live-record"]
    assert response.json()["summary"]["record_id"] == "live-record"
