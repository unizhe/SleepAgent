from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from sleepagent.radar_agent.provider import ReplayRadarProvider
from sleepagent.radar_agent.quality import (
    DataQualityGate,
    DeviceBindingMismatch,
    run_replay_night_slice,
)
from sleepagent.radar_agent.schemas import (
    RadarBedPresence,
    RadarDataQualityStatus,
    RadarDevice,
    RadarDeviceStatus,
    RadarNightSummary,
    RadarVitalSnapshot,
)


NIGHT = date(2026, 7, 9)


def test_normal_replay_slice_runs_provider_to_quality_gate_to_night_summary() -> None:
    summary = run_replay_night_slice(ReplayRadarProvider(scenario="normal_night"))

    assert isinstance(summary, RadarNightSummary)
    assert summary.subject_id == "elder-demo-001"
    assert summary.radar_device_id == "radar-device-demo-001"
    assert summary.device_status == RadarDeviceStatus.ONLINE
    assert summary.night_of == NIGHT
    assert summary.night_boundary_start_at is not None
    assert summary.night_boundary_start_at.tzinfo == timezone.utc
    assert summary.night_boundary_end_at is not None
    assert summary.night_boundary_end_at.tzinfo == timezone.utc
    assert summary.data_quality_status == RadarDataQualityStatus.GOOD
    assert summary.confidence_label == "normal"
    assert summary.health_conclusion_allowed is True
    assert summary.data_coverage_ratio == 0.96
    assert summary.invalid_reading_count == 0
    assert summary.missing_intervals == []
    assert summary.explainable_metrics["snapshot_count"] == 5
    assert summary.explainable_metrics["mean_breath_rate_bpm"] == 15.0


def test_partial_missing_data_is_low_confidence_but_still_summarized() -> None:
    device = _device()
    snapshots = [
        _snapshot("s-000", "2026-07-09T22:30:00+00:00"),
        _snapshot(
            "s-001",
            "2026-07-10T00:30:00+00:00",
            flags=["missing_interval"],
        ),
        _snapshot("s-002", "2026-07-10T05:30:00+00:00"),
    ]
    report = _report(
        coverage=0.72,
        missing=["2026-07-10T00:30:00Z/2026-07-10T05:30:00Z"],
    )

    summary = DataQualityGate().run(
        device=device,
        snapshots=snapshots,
        provider_report=report,
    )

    assert summary.data_quality_status == RadarDataQualityStatus.PARTIAL
    assert summary.confidence_label == "low_confidence"
    assert summary.health_conclusion_allowed is True
    assert "missing_intervals" in summary.quality_reasons
    assert summary.missing_intervals
    assert summary.total_sleep_minutes == 420
    assert "low confidence" in " ".join(summary.caveats)


def test_offline_replay_slice_is_not_interpretable_and_has_no_health_conclusion() -> None:
    summary = run_replay_night_slice(
        ReplayRadarProvider(scenario="device_or_data_quality_issue")
    )

    assert summary.device_status == RadarDeviceStatus.OFFLINE
    assert summary.data_quality_status == RadarDataQualityStatus.UNUSABLE
    assert summary.confidence_label == "not_interpretable"
    assert summary.health_conclusion_allowed is False
    assert summary.sleep_score is None
    assert summary.total_sleep_minutes is None
    assert "device_offline" in summary.blocked_reasons
    assert "low_coverage" in summary.blocked_reasons
    assert "Data is not interpretable" in " ".join(summary.caveats)


def test_severe_low_coverage_is_not_interpretable_even_when_device_is_online() -> None:
    device = _device()
    snapshots = [
        _snapshot("s-000", "2026-07-09T22:30:00+00:00"),
        _snapshot("s-001", "2026-07-10T05:30:00+00:00"),
    ]
    report = _report(coverage=0.31)

    summary = DataQualityGate().run(
        device=device,
        snapshots=snapshots,
        provider_report=report,
    )

    assert summary.device_status == RadarDeviceStatus.ONLINE
    assert summary.data_quality_status == RadarDataQualityStatus.UNUSABLE
    assert summary.confidence_label == "not_interpretable"
    assert summary.health_conclusion_allowed is False
    assert "low_coverage" in summary.blocked_reasons
    assert summary.total_sleep_minutes is None


def test_long_out_of_bed_period_is_not_interpreted_as_sleep_health() -> None:
    device = _device()
    snapshots = [
        _snapshot(
            "s-000",
            "2026-07-09T22:30:00+00:00",
            presence=RadarBedPresence.OUT_OF_BED,
        ),
        _snapshot(
            "s-001",
            "2026-07-10T05:30:00+00:00",
            presence=RadarBedPresence.OUT_OF_BED,
        ),
    ]
    report = _report(coverage=0.92)

    summary = DataQualityGate().run(
        device=device,
        snapshots=snapshots,
        provider_report=report,
    )

    assert summary.data_quality_status == RadarDataQualityStatus.UNUSABLE
    assert summary.confidence_label == "not_interpretable"
    assert summary.health_conclusion_allowed is False
    assert "long_not_in_bed" in summary.blocked_reasons
    assert summary.not_in_bed_intervals
    assert summary.explainable_metrics["not_in_bed_minutes"] >= 360
    assert summary.sleep_score is None


def test_abnormal_readings_are_flagged_without_creating_a_diagnosis() -> None:
    device = _device()
    snapshots = [
        _snapshot(
            "s-000",
            "2026-07-09T22:30:00+00:00",
            heart=168,
            breath=42,
            flags=["vital_outlier"],
        ),
        _snapshot("s-001", "2026-07-10T00:30:00+00:00", heart=64, breath=15),
        _snapshot("s-002", "2026-07-10T02:30:00+00:00", heart=62, breath=14),
    ]
    report = _report(coverage=0.95)

    summary = DataQualityGate().run(
        device=device,
        snapshots=snapshots,
        provider_report=report,
    )

    assert summary.data_quality_status == RadarDataQualityStatus.PARTIAL
    assert summary.confidence_label == "low_confidence"
    assert summary.abnormal_reading_count >= 3
    assert "abnormal_readings" in summary.quality_reasons
    assert "not a diagnosis" in " ".join(summary.caveats)
    assert summary.explainable_metrics["mean_heart_rate_bpm"] == 98.0


def test_quality_gate_rejects_cross_subject_device_binding() -> None:
    device = _device(subject_id="elder-001")
    snapshots = [
        _snapshot("s-000", "2026-07-09T22:30:00+00:00", subject_id="elder-002"),
    ]

    with pytest.raises(DeviceBindingMismatch):
        DataQualityGate().run(
            device=device,
            snapshots=snapshots,
            provider_report=_report(),
        )


def _device(subject_id: str = "elder-001") -> RadarDevice:
    return RadarDevice(
        radar_device_id="radar-001",
        display_name="Bedroom radar",
        provider="replay",
        status=RadarDeviceStatus.ONLINE,
        bound_subject_id=subject_id,
        timezone_name="Asia/Shanghai",
    )


def _snapshot(
    snapshot_id: str,
    measured_at: str,
    *,
    subject_id: str = "elder-001",
    heart: int | None = 64,
    breath: int | None = 15,
    presence: RadarBedPresence = RadarBedPresence.IN_BED,
    flags: list[str] | None = None,
    invalid_flags: list[str] | None = None,
) -> RadarVitalSnapshot:
    measured = datetime.fromisoformat(measured_at)
    return RadarVitalSnapshot(
        snapshot_id=snapshot_id,
        radar_device_id="radar-001",
        subject_id=subject_id,
        measured_at=measured,
        received_at=measured,
        heart_rate_bpm=heart,
        breath_rate_bpm=breath,
        body_movement=0.4,
        bed_presence=presence,
        data_quality_flags=list(flags or []),
        invalid_reading_flags=list(invalid_flags or []),
        source_raw_event_ids=[f"raw:{snapshot_id}"],
    )


def _report(
    *,
    coverage: float = 0.95,
    missing: list[str] | None = None,
) -> RadarNightSummary:
    return RadarNightSummary(
        radar_device_id="radar-001",
        subject_id="elder-001",
        night_of=NIGHT,
        timezone_name="Asia/Shanghai",
        sleep_start_at=datetime(2026, 7, 9, 22, 30, tzinfo=timezone.utc),
        sleep_end_at=datetime(2026, 7, 10, 6, 10, tzinfo=timezone.utc),
        total_sleep_minutes=420,
        sleep_score=82,
        data_coverage_ratio=coverage,
        missing_intervals=list(missing or []),
        source_report_ref="test-report",
    )
