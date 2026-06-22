import pytest
from pydantic import ValidationError

from sleepagent.schemas import (
    PatientProfile,
    ReportSummary,
    RespiratoryEvent,
    RespiratoryEventType,
    RiskLevel,
    Sex,
    SleepEpoch,
    SleepStage,
    SleepStagingMetrics,
)


def test_patient_profile_schema_accepts_core_fields() -> None:
    profile = PatientProfile(
        subject_id="subject-001",
        age_years=72,
        sex=Sex.FEMALE,
        notes=["MVP synthetic profile."],
    )

    assert profile.subject_id == "subject-001"
    assert profile.age_years == 72
    assert profile.sex == Sex.FEMALE


def test_sleep_epoch_rejects_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        SleepEpoch(
            start_second=0,
            duration_seconds=30,
            stage=SleepStage.NREM,
            confidence=1.2,
        )


def test_respiratory_event_exposes_end_second() -> None:
    event = RespiratoryEvent(
        start_second=120,
        duration_seconds=15,
        event_type=RespiratoryEventType.HYPOPNEA,
        confidence=0.81,
        oxygen_desaturation_percent=4.2,
    )

    assert event.end_second == 135


def test_sleep_staging_metrics_rejects_invalid_per_class_f1() -> None:
    with pytest.raises(ValidationError):
        SleepStagingMetrics(per_class_f1={SleepStage.REM: 1.2})


def test_report_summary_reuses_respiratory_rate_bounds() -> None:
    with pytest.raises(ValidationError):
        ReportSummary(
            record_id="record-001",
            subject_id="subject-001",
            risk_level=RiskLevel.LOW,
            total_recording_minutes=30.0,
            total_sleep_minutes=25.0,
            sleep_efficiency_percent=83.3,
            ahi=1.0,
            hypopnea_count=1,
            suspected_apnea_count=0,
            mean_respiratory_rate_bpm=81.0,
        )
