from __future__ import annotations

from datetime import datetime, time, timezone

from sleepagent.product_device.schemas import (
    RadarBedPresence,
    RadarDataQuality,
    RadarDevice,
    RadarDeviceStatus,
    RadarSleepReport,
    RadarVitalSnapshot,
)
from sleepagent.observability import log_event, record_data_freshness


DEFAULT_STALE_SNAPSHOT_SECONDS = 300.0
DEFAULT_EXPECTED_SNAPSHOT_INTERVAL_SECONDS = 60.0
DEFAULT_REPORT_STALE_HOURS = 36.0


def evaluate_radar_data_quality(
    *,
    device: RadarDevice,
    recent_snapshots: list[RadarVitalSnapshot],
    latest_sleep_report: RadarSleepReport | None = None,
    now: datetime | None = None,
    stale_snapshot_after_seconds: float = DEFAULT_STALE_SNAPSHOT_SECONDS,
    expected_snapshot_interval_seconds: float = (
        DEFAULT_EXPECTED_SNAPSHOT_INTERVAL_SECONDS
    ),
    report_stale_after_hours: float = DEFAULT_REPORT_STALE_HOURS,
) -> RadarDataQuality:
    resolved_now = _as_utc(now or datetime.now(timezone.utc))
    ordered_snapshots = sorted(recent_snapshots, key=lambda item: item.measured_at)
    current_snapshot = ordered_snapshots[-1] if ordered_snapshots else None

    caveats: list[str] = []
    blocked_reasons: list[str] = []
    freshness_seconds: float | None = None
    stale = False
    current_snapshot_available = current_snapshot is not None

    if current_snapshot is None:
        caveats.append("No recent radar vital snapshot is available.")
        blocked_reasons.append("no_recent_vital_snapshot")
    else:
        freshness_seconds = max(
            0.0,
            (resolved_now - _as_utc(current_snapshot.measured_at)).total_seconds(),
        )
        stale = freshness_seconds > stale_snapshot_after_seconds
        if stale:
            caveats.append("The latest radar vital snapshot is stale.")
            blocked_reasons.append("stale_current_snapshot")

    device_offline = device.status == RadarDeviceStatus.OFFLINE
    if device_offline:
        caveats.append("The radar device is offline, so live readings may be unavailable.")
        blocked_reasons.append("device_offline")

    user_out_of_bed = (
        current_snapshot is not None
        and current_snapshot.bed_presence == RadarBedPresence.OUT_OF_BED
    )
    if user_out_of_bed:
        caveats.append("The user appears out of bed, so live in-bed readings are limited.")
        blocked_reasons.append("user_out_of_bed")

    missing_intervals = _count_missing_intervals(
        ordered_snapshots,
        expected_snapshot_interval_seconds=expected_snapshot_interval_seconds,
    )
    if missing_intervals:
        caveats.append("Recent radar vital snapshots have missing time intervals.")

    missing_reading_count = sum(
        _count_missing_vital_readings(snapshot) for snapshot in ordered_snapshots
    )
    if missing_reading_count:
        caveats.append("Some recent heart-rate or breath-rate readings are missing.")

    invalid_reading_count = sum(
        _count_invalid_readings(snapshot) for snapshot in ordered_snapshots
    )
    if invalid_reading_count:
        caveats.append(
            "The vendor reported invalid -1 readings, so affected vital values are unavailable."
        )

    if current_snapshot is not None:
        current_missing = _count_missing_vital_readings(current_snapshot)
        current_invalid = _count_invalid_readings(current_snapshot)
        if current_missing >= 2:
            blocked_reasons.append("current_vital_readings_missing")
        if current_invalid >= 2:
            blocked_reasons.append("current_vital_readings_invalid")

    (
        report_freshness_hours,
        report_stale,
        partial_sleep_report,
        report_caveats,
    ) = _evaluate_report_freshness(
        latest_sleep_report,
        now=resolved_now,
        report_stale_after_hours=report_stale_after_hours,
    )
    caveats.extend(report_caveats)

    deduped_blocked_reasons = _dedupe(blocked_reasons)
    quality = RadarDataQuality(
        freshness_seconds=freshness_seconds,
        max_snapshot_age_seconds=stale_snapshot_after_seconds,
        stale=stale,
        device_offline=device_offline,
        user_out_of_bed=user_out_of_bed,
        current_snapshot_available=current_snapshot_available,
        missing_intervals=missing_intervals,
        missing_reading_count=missing_reading_count,
        invalid_reading_count=invalid_reading_count,
        report_freshness_hours=report_freshness_hours,
        max_report_age_hours=report_stale_after_hours,
        report_stale=report_stale,
        partial_sleep_report=partial_sleep_report,
        blocks_current_values=bool(deduped_blocked_reasons),
        caveats=_dedupe(caveats),
        blocked_reasons=deduped_blocked_reasons,
    )
    _record_data_quality_decision(
        device=device,
        quality=quality,
        evaluated_at=resolved_now,
    )
    return quality


def latest_vital_snapshot(
    snapshots: list[RadarVitalSnapshot],
) -> RadarVitalSnapshot | None:
    if not snapshots:
        return None
    return max(snapshots, key=lambda item: item.measured_at)


def _count_missing_intervals(
    snapshots: list[RadarVitalSnapshot],
    *,
    expected_snapshot_interval_seconds: float,
) -> int:
    if len(snapshots) < 2:
        return 0
    max_gap_seconds = expected_snapshot_interval_seconds * 2
    missing_intervals = 0
    for previous, current in zip(snapshots, snapshots[1:]):
        gap_seconds = (
            _as_utc(current.measured_at) - _as_utc(previous.measured_at)
        ).total_seconds()
        if gap_seconds > max_gap_seconds:
            missing_intervals += 1
    return missing_intervals


def _count_missing_vital_readings(snapshot: RadarVitalSnapshot) -> int:
    count = 0
    if snapshot.heart_rate_bpm is None:
        count += 1
    if snapshot.breath_rate_bpm is None:
        count += 1
    return count


def _count_invalid_readings(snapshot: RadarVitalSnapshot) -> int:
    return sum(1 for flag in snapshot.invalid_reading_flags if "invalid" in flag)


def _evaluate_report_freshness(
    latest_sleep_report: RadarSleepReport | None,
    *,
    now: datetime,
    report_stale_after_hours: float,
) -> tuple[float | None, bool, bool, list[str]]:
    if latest_sleep_report is None:
        return (
            None,
            False,
            True,
            ["No radar sleep report is available."],
        )

    report_reference_time = _report_reference_time(latest_sleep_report)
    report_freshness_hours = max(
        0.0,
        (_as_utc(now) - report_reference_time).total_seconds() / 3600.0,
    )
    report_stale = report_freshness_hours > report_stale_after_hours
    partial_sleep_report = _is_partial_sleep_report(latest_sleep_report)

    caveats: list[str] = []
    if report_stale:
        caveats.append("The latest radar sleep report is stale.")
    if partial_sleep_report:
        caveats.append("The latest radar sleep report is incomplete.")
    return report_freshness_hours, report_stale, partial_sleep_report, caveats


def _report_reference_time(report: RadarSleepReport) -> datetime:
    if report.sleep_end_at is not None:
        return _as_utc(report.sleep_end_at)
    if report.source_metadata.vendor_payload_timestamp is not None:
        return _as_utc(report.source_metadata.vendor_payload_timestamp)
    if report.source_metadata.received_at is not None:
        return _as_utc(report.source_metadata.received_at)
    return datetime.combine(report.report_date, time.min).replace(tzinfo=timezone.utc)


def _is_partial_sleep_report(report: RadarSleepReport) -> bool:
    return (
        report.sleep_start_at is None
        or report.sleep_end_at is None
        or report.total_sleep_minutes is None
        or report.sleep_score is None
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dedupe(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        deduped.append(item)
        seen.add(item)
    return deduped


def _record_data_quality_decision(
    *,
    device: RadarDevice,
    quality: RadarDataQuality,
    evaluated_at: datetime,
) -> None:
    freshness = {
        "source": "radar_product_data_quality",
        "radar_device_id": device.radar_device_id,
        "provider": device.provider,
        "device_status": device.status.value,
        "evaluated_at": evaluated_at.isoformat(),
        "freshness_seconds": quality.freshness_seconds,
        "stale": quality.stale,
        "report_freshness_hours": quality.report_freshness_hours,
        "report_stale": quality.report_stale,
        "blocks_current_values": quality.blocks_current_values,
        "blocked_reasons": quality.blocked_reasons,
    }
    record_data_freshness(freshness)
    log_event("data_quality_decision", **freshness)
