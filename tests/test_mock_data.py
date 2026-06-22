import pytest

from sleepagent.preprocessing import generate_mock_sleep_analysis
from sleepagent.schemas import RespiratoryEventType, RiskLevel, SleepStage


def test_mock_sleep_analysis_is_deterministic_with_seed() -> None:
    first = generate_mock_sleep_analysis(seed=7)
    second = generate_mock_sleep_analysis(seed=7)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_mock_sleep_analysis_contains_core_contract_fields() -> None:
    result = generate_mock_sleep_analysis(
        record_id="mock-record",
        subject_id="mock-subject",
        duration_hours=2.0,
        seed=11,
    )

    assert result.metadata.record_id == "mock-record"
    assert result.metadata.patient.subject_id == "mock-subject"
    assert result.metadata.source_dataset == "mock"
    assert result.epochs
    assert result.respiratory_events
    assert result.respiratory_trend
    assert result.risk_level in set(RiskLevel)


def test_mock_sleep_stage_summary_matches_epoch_minutes() -> None:
    result = generate_mock_sleep_analysis(duration_hours=2.0, seed=13)

    wake_minutes = sum(
        epoch.duration_seconds for epoch in result.epochs if epoch.stage == SleepStage.WAKE
    ) / 60
    rem_minutes = sum(
        epoch.duration_seconds for epoch in result.epochs if epoch.stage == SleepStage.REM
    ) / 60
    nrem_minutes = sum(
        epoch.duration_seconds for epoch in result.epochs if epoch.stage == SleepStage.NREM
    ) / 60

    assert result.sleep_summary.wake_minutes == pytest.approx(wake_minutes)
    assert result.sleep_summary.rem_minutes == pytest.approx(rem_minutes)
    assert result.sleep_summary.nrem_minutes == pytest.approx(nrem_minutes)


def test_mock_respiratory_summary_matches_events() -> None:
    result = generate_mock_sleep_analysis(duration_hours=2.0, seed=17)

    normal_count = sum(
        event.event_type == RespiratoryEventType.NORMAL_BREATHING
        for event in result.respiratory_events
    )
    hypopnea_count = sum(
        event.event_type == RespiratoryEventType.HYPOPNEA
        for event in result.respiratory_events
    )
    suspected_apnea_count = sum(
        event.event_type == RespiratoryEventType.SUSPECTED_APNEA
        for event in result.respiratory_events
    )

    assert result.respiratory_summary.normal_breathing_count == normal_count
    assert result.respiratory_summary.hypopnea_count == hypopnea_count
    assert result.respiratory_summary.suspected_apnea_count == suspected_apnea_count
    assert result.respiratory_summary.ahi >= 0

