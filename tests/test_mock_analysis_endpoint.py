from fastapi.testclient import TestClient

from backend.main import app


def test_mock_analysis_returns_sleep_analysis_payload() -> None:
    client = TestClient(app)

    response = client.get(
        "/mock-analysis",
        params={
            "record_id": "record-a",
            "subject_id": "subject-a",
            "duration_hours": 0.5,
            "seed": 123,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"]["record_id"] == "record-a"
    assert payload["metadata"]["patient"]["subject_id"] == "subject-a"
    assert payload["metadata"]["source_dataset"] == "mock"
    assert payload["metadata"]["duration_seconds"] == 1800.0
    assert payload["epochs"]
    assert payload["respiratory_events"]
    assert payload["respiratory_trend"]
    assert payload["sleep_summary"]["total_recording_minutes"] == 30.0
    assert payload["respiratory_summary"]["ahi"] >= 0
    assert payload["risk_level"] in {"low", "moderate", "high"}


def test_mock_analysis_is_deterministic_for_same_seed() -> None:
    client = TestClient(app)
    params = {"duration_hours": 0.5, "seed": 99}

    first = client.get("/mock-analysis", params=params)
    second = client.get("/mock-analysis", params=params)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()


def test_mock_analysis_rejects_too_short_duration() -> None:
    client = TestClient(app)

    response = client.get("/mock-analysis", params={"duration_hours": 0.1})

    assert response.status_code == 422

