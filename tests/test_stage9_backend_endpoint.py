from fastapi.testclient import TestClient

from backend.main import app
from sleepagent.services import (
    ALERT_EVENTS_FILENAME,
    ANALYSIS_RECORDS_FILENAME,
    REPORT_RECORDS_FILENAME,
    SLEEPAGENT_DATA_STORE_DIR_ENV,
)


def test_stage9_mock_context_endpoint_runs_local_integration(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SLEEPAGENT_DATA_STORE_DIR_ENV, str(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/stage9/mock-context",
        json={
            "record_id": "stage9-api-record",
            "subject_id": "stage9-api-subject",
            "duration_hours": 0.5,
            "seed": 701,
            "abnormal_event_rate_per_hour": 20.0,
            "location": "Shanghai",
            "context_date": "2026-05-21",
            "external_context_seed": 702,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["local_store_dir"] == str(tmp_path)
    assert payload["analysis_record"]["record_id"] == "stage9-api-record"
    assert payload["report_record"]["analysis_id"] == (
        payload["analysis_record"]["analysis_id"]
    )
    assert payload["memory_summary"]["subject_id"] == "stage9-api-subject"
    assert payload["memory_summary"]["record_count"] == 1
    assert payload["alert_event"] is not None
    assert payload["alert_event"]["risk_level"] == "high"
    assert payload["alert_event"]["push_channels_attempted"] == []
    assert payload["external_context"]["source"] == "mock"
    assert payload["external_context"]["location"] == "Shanghai"
    assert payload["external_context"]["context_date"] == "2026-05-21"
    assert "不代表真实外部数据" in payload["external_context"]["summary"]
    assert tmp_path.joinpath(ANALYSIS_RECORDS_FILENAME).exists()
    assert tmp_path.joinpath(REPORT_RECORDS_FILENAME).exists()
    assert tmp_path.joinpath(ALERT_EVENTS_FILENAME).exists()


def test_stage9_mock_context_endpoint_accumulates_memory_without_low_risk_alert(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(SLEEPAGENT_DATA_STORE_DIR_ENV, str(tmp_path))
    client = TestClient(app)

    first = client.post(
        "/stage9/mock-context",
        json={
            "record_id": "stage9-api-record-1",
            "subject_id": "stage9-api-subject",
            "duration_hours": 0.5,
            "seed": 703,
            "abnormal_event_rate_per_hour": 1.0,
        },
    )
    second = client.post(
        "/stage9/mock-context",
        json={
            "record_id": "stage9-api-record-2",
            "subject_id": "stage9-api-subject",
            "duration_hours": 0.5,
            "seed": 704,
            "abnormal_event_rate_per_hour": 1.0,
        },
    )

    assert first.status_code == 200
    assert second.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload["alert_event"] is None
    assert second_payload["alert_event"] is None
    assert second_payload["memory_summary"]["record_count"] == 2
    assert second_payload["memory_summary"]["record_ids"] == [
        "stage9-api-record-2",
        "stage9-api-record-1",
    ]
    assert not tmp_path.joinpath(ALERT_EVENTS_FILENAME).exists()


def test_stage9_mock_context_endpoint_rejects_invalid_duration(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(SLEEPAGENT_DATA_STORE_DIR_ENV, str(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/stage9/mock-context",
        json={
            "duration_hours": 0.1,
        },
    )

    assert response.status_code == 422
