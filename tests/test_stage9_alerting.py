from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from sleepagent.preprocessing import generate_mock_sleep_analysis
from sleepagent.schemas import AlertEvent, RiskLevel
from sleepagent.services import (
    ALERT_EVENTS_FILENAME,
    LocalJsonlAlertEventRepository,
    LocalJsonlSleepDataRepository,
    build_high_risk_alert_event,
    record_high_risk_alert_for_analysis_if_needed,
    record_high_risk_alert_if_needed,
)


def test_high_risk_analysis_record_builds_local_alert_event(tmp_path) -> None:
    data_repository = LocalJsonlSleepDataRepository(tmp_path)
    analysis = generate_mock_sleep_analysis(
        record_id="alert-record-high",
        subject_id="alert-subject",
        duration_hours=1.0,
        seed=501,
        abnormal_event_rate_per_hour=20.0,
    )
    analysis_record = data_repository.save_analysis(analysis)
    created_at = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)

    event = build_high_risk_alert_event(
        analysis_record,
        created_at=created_at,
    )

    assert event is not None
    assert event.schema_version == "stage9.alert_event.v1"
    assert (
        event.alert_id
        == "alert-alert-subject-alert-record-high-20260301T080000Z"
    )
    assert event.subject_id == "alert-subject"
    assert event.record_id == "alert-record-high"
    assert event.source_analysis_id == analysis_record.analysis_id
    assert event.risk_level == RiskLevel.HIGH
    assert event.ahi == analysis.respiratory_summary.ahi
    assert "risk_level=high" in event.trigger_reasons
    assert event.push_channels_attempted == []
    assert "未发送短信、邮件或应用推送" in event.message


def test_local_alert_repository_records_and_filters_events(tmp_path) -> None:
    data_repository = LocalJsonlSleepDataRepository(tmp_path)
    alert_repository = LocalJsonlAlertEventRepository(tmp_path)
    high_risk_analysis = generate_mock_sleep_analysis(
        record_id="alert-record-high",
        subject_id="alert-subject",
        duration_hours=1.0,
        seed=502,
        abnormal_event_rate_per_hour=20.0,
    )
    low_risk_analysis = generate_mock_sleep_analysis(
        record_id="alert-record-low",
        subject_id="alert-subject",
        duration_hours=1.0,
        seed=503,
        abnormal_event_rate_per_hour=1.0,
    )
    high_record = data_repository.save_analysis(high_risk_analysis)
    low_record = data_repository.save_analysis(low_risk_analysis)

    event = record_high_risk_alert_if_needed(alert_repository, high_record)
    skipped_event = record_high_risk_alert_if_needed(alert_repository, low_record)

    assert event is not None
    assert skipped_event is None
    assert tmp_path.joinpath(ALERT_EVENTS_FILENAME).exists()
    assert alert_repository.list_alert_events(subject_id="alert-subject") == [event]
    assert alert_repository.list_alert_events(record_id="alert-record-high") == [event]
    assert alert_repository.list_alert_events(record_id="alert-record-low") == []


def test_record_alert_for_analysis_does_not_require_saved_analysis(tmp_path) -> None:
    alert_repository = LocalJsonlAlertEventRepository(tmp_path)
    analysis = generate_mock_sleep_analysis(
        record_id="alert-analysis-record",
        subject_id="alert-analysis-subject",
        duration_hours=1.0,
        seed=504,
        abnormal_event_rate_per_hour=25.0,
    )

    event = record_high_risk_alert_for_analysis_if_needed(
        alert_repository,
        analysis,
        analysis_id="analysis-alert-manual",
    )

    assert event is not None
    assert event.source_analysis_id == "analysis-alert-manual"
    assert alert_repository.list_alert_events(
        subject_id="alert-analysis-subject"
    ) == [event]


def test_alert_event_schema_rejects_non_local_or_non_high_alerts() -> None:
    base_payload = {
        "alert_id": "alert-invalid",
        "subject_id": "subject-invalid",
        "record_id": "record-invalid",
        "risk_level": RiskLevel.HIGH,
        "ahi": 16.0,
        "hypopnea_count": 10,
        "suspected_apnea_count": 4,
        "trigger_reasons": ["risk_level=high"],
        "message": "本地高风险告警",
    }

    with pytest.raises(ValidationError):
        AlertEvent(**base_payload, push_channels_attempted=["email"])

    with pytest.raises(ValidationError):
        AlertEvent(**{**base_payload, "risk_level": RiskLevel.MODERATE})

    with pytest.raises(ValidationError):
        AlertEvent(**{**base_payload, "trigger_reasons": []})
