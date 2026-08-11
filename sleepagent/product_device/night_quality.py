from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sleepagent.product_runtime.schemas import (
    RadarBedPresence,
    RadarDataQualityStatus,
    RadarDevice,
    RadarDeviceStatus,
    RadarNightSummary,
    RadarVitalSnapshot,
)


DEFAULT_NIGHT_START = time(hour=18)
DEFAULT_NIGHT_END = time(hour=12)
DEFAULT_PROVIDER_DELAY_SECONDS = 30 * 60
DEFAULT_LONG_NOT_IN_BED_MINUTES = 6 * 60
DEFAULT_LOW_CONFIDENCE_COVERAGE = 0.85
DEFAULT_UNUSABLE_COVERAGE = 0.50


class DataQualityGateError(ValueError):
    pass


class DeviceBindingMismatch(DataQualityGateError):
    pass


class RadarProviderLike(Protocol):
    def list_devices(self) -> list[RadarDevice]:
        ...

    def get_device(self, radar_device_id: str) -> RadarDevice:
        ...

    def pull_snapshots(
        self,
        radar_device_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[RadarVitalSnapshot]:
        ...

    def pull_night_report(
        self,
        radar_device_id: str,
        *,
        night_of: datetime | date | None = None,
    ) -> RadarNightSummary | None:
        ...


@dataclass(frozen=True)
class NightWindow:
    night_of: date
    timezone_name: str
    start_at: datetime
    end_at: datetime


class DataQualityGate:
    """Build a canonical night summary from radar snapshots using fail-soft rules."""

    def __init__(
        self,
        *,
        low_confidence_coverage: float = DEFAULT_LOW_CONFIDENCE_COVERAGE,
        unusable_coverage: float = DEFAULT_UNUSABLE_COVERAGE,
        long_not_in_bed_minutes: float = DEFAULT_LONG_NOT_IN_BED_MINUTES,
        provider_delay_seconds: float = DEFAULT_PROVIDER_DELAY_SECONDS,
    ) -> None:
        self.low_confidence_coverage = low_confidence_coverage
        self.unusable_coverage = unusable_coverage
        self.long_not_in_bed_minutes = long_not_in_bed_minutes
        self.provider_delay_seconds = provider_delay_seconds

    def run(
        self,
        *,
        device: RadarDevice,
        snapshots: list[RadarVitalSnapshot],
        provider_report: RadarNightSummary | None = None,
        night_of: date | datetime | None = None,
        generated_at: datetime | None = None,
    ) -> RadarNightSummary:
        timezone_name = (
            provider_report.timezone_name if provider_report else device.timezone_name
        )
        window = resolve_night_window(
            night_of=night_of or (provider_report.night_of if provider_report else None),
            snapshots=snapshots,
            timezone_name=timezone_name,
        )
        processing_start, processing_end = _processing_bounds(
            window=window,
            snapshots=snapshots,
            provider_report=provider_report,
        )
        subject_id = _resolve_subject_id(device, snapshots, provider_report)
        ordered = [
            _normalize_snapshot(snapshot)
            for snapshot in snapshots
            if processing_start <= _as_utc(snapshot.measured_at) <= processing_end
        ]
        ordered.sort(key=lambda item: (_as_utc(item.measured_at), item.snapshot_id))

        missing_intervals = _dedupe(
            list(provider_report.missing_intervals if provider_report else [])
            + _missing_intervals(ordered)
            + _provider_delay_intervals(ordered, self.provider_delay_seconds)
        )
        not_in_bed_intervals = _bed_presence_intervals(
            ordered,
            presence={RadarBedPresence.OUT_OF_BED, RadarBedPresence.UNKNOWN},
        )
        out_of_bed_intervals = _bed_presence_intervals(
            ordered,
            presence={RadarBedPresence.OUT_OF_BED},
        )
        invalid_reading_count = max(
            provider_report.invalid_reading_count if provider_report else 0,
            sum(len(snapshot.invalid_reading_flags) for snapshot in ordered),
        )
        abnormal_reading_count = sum(
            _abnormal_reading_count(snapshot) for snapshot in ordered
        )
        in_bed_count = sum(
            1 for snapshot in ordered if snapshot.bed_presence == RadarBedPresence.IN_BED
        )
        not_in_bed_count = sum(
            1 for snapshot in ordered if snapshot.bed_presence != RadarBedPresence.IN_BED
        )
        coverage_ratio = _coverage_ratio(provider_report, ordered)
        not_in_bed_minutes = _interval_minutes(not_in_bed_intervals)

        reasons: list[str] = []
        blocked_reasons: list[str] = []
        caveats = [
            "Radar observation is not a medical diagnosis.",
            "Metrics are explainable only inside the canonical night window.",
        ]
        status = RadarDataQualityStatus.GOOD

        if device.status == RadarDeviceStatus.OFFLINE:
            status = RadarDataQualityStatus.UNUSABLE
            blocked_reasons.append("device_offline")
            reasons.append("device_offline")
        if coverage_ratio < self.unusable_coverage:
            status = RadarDataQualityStatus.UNUSABLE
            blocked_reasons.append("low_coverage")
            reasons.append("low_coverage")
        elif coverage_ratio < self.low_confidence_coverage:
            status = RadarDataQualityStatus.PARTIAL
            reasons.append("partial_coverage")
        if missing_intervals:
            if status == RadarDataQualityStatus.GOOD:
                status = RadarDataQualityStatus.PARTIAL
            if status == RadarDataQualityStatus.UNUSABLE:
                blocked_reasons.append("missing_intervals")
            reasons.append("missing_intervals")
        if invalid_reading_count:
            if status == RadarDataQualityStatus.GOOD:
                status = RadarDataQualityStatus.PARTIAL
            reasons.append("invalid_readings")
        if abnormal_reading_count:
            if status == RadarDataQualityStatus.GOOD:
                status = RadarDataQualityStatus.PARTIAL
            reasons.append("abnormal_readings")
            caveats.append(
                "Abnormal radar readings need downstream review and are not a diagnosis."
            )
        if not_in_bed_minutes >= self.long_not_in_bed_minutes:
            status = RadarDataQualityStatus.UNUSABLE
            blocked_reasons.append("long_not_in_bed")
            reasons.append("long_not_in_bed")

        if status == RadarDataQualityStatus.UNUSABLE:
            confidence_label = "not_interpretable"
            health_conclusion_allowed = False
            caveats.append("Data is not interpretable enough for a health conclusion.")
        elif status == RadarDataQualityStatus.PARTIAL:
            confidence_label = "low_confidence"
            health_conclusion_allowed = True
            caveats.append("Some metrics are low confidence because data quality is incomplete.")
        else:
            confidence_label = "normal"
            health_conclusion_allowed = True

        report = provider_report
        return RadarNightSummary(
            radar_device_id=device.radar_device_id,
            subject_id=subject_id,
            night_of=window.night_of,
            timezone_name=window.timezone_name,
            night_boundary_start_at=window.start_at,
            night_boundary_end_at=window.end_at,
            device_status=device.status,
            sleep_start_at=_usable_report_value(report, status, "sleep_start_at"),
            sleep_end_at=_usable_report_value(report, status, "sleep_end_at"),
            total_sleep_minutes=_usable_report_value(report, status, "total_sleep_minutes"),
            sleep_score=_usable_report_value(report, status, "sleep_score"),
            out_of_bed_count=(
                _report_value(report, "out_of_bed_count") or len(out_of_bed_intervals)
            ),
            movement_count=(
                _report_value(report, "movement_count") or _movement_count(ordered)
            ),
            data_coverage_ratio=coverage_ratio,
            data_quality_status=status,
            confidence_label=confidence_label,
            health_conclusion_allowed=health_conclusion_allowed,
            invalid_reading_count=invalid_reading_count,
            abnormal_reading_count=abnormal_reading_count,
            missing_intervals=missing_intervals,
            out_of_bed_intervals=out_of_bed_intervals,
            not_in_bed_intervals=not_in_bed_intervals,
            quality_reasons=_dedupe(reasons),
            blocked_reasons=_dedupe(blocked_reasons),
            caveats=_dedupe(caveats),
            explainable_metrics=_explainable_metrics(
                ordered=ordered,
                coverage_ratio=coverage_ratio,
                in_bed_count=in_bed_count,
                not_in_bed_count=not_in_bed_count,
                not_in_bed_minutes=not_in_bed_minutes,
                vital_fluctuation_count=sum(
                    "vital_fluctuation" in snapshot.data_quality_flags
                    for snapshot in ordered
                ),
            ),
            source_snapshot_ids=[snapshot.snapshot_id for snapshot in ordered],
            source_raw_event_ids=_dedupe(
                raw_id
                for snapshot in ordered
                for raw_id in snapshot.source_raw_event_ids
            ),
            source_report_ref=(
                report.source_report_ref
                if report is not None
                else f"quality-gate:{device.radar_device_id}:{window.night_of.isoformat()}"
            ),
            generated_at=_as_utc(generated_at or datetime.now(timezone.utc)),
        )


def run_replay_night_slice(
    provider: RadarProviderLike,
    *,
    radar_device_id: str | None = None,
    night_of: date | datetime | None = None,
    gate: DataQualityGate | None = None,
) -> RadarNightSummary:
    devices = provider.list_devices()
    if not devices:
        raise DataQualityGateError("replay provider returned no radar devices")
    device_id = radar_device_id or devices[0].radar_device_id
    device = provider.get_device(device_id)
    report = provider.pull_night_report(device.radar_device_id, night_of=night_of)
    window = resolve_night_window(
        night_of=night_of or (report.night_of if report is not None else None),
        snapshots=[],
        timezone_name=device.timezone_name,
    )
    snapshots = provider.pull_snapshots(device.radar_device_id)
    return (gate or DataQualityGate()).run(
        device=device,
        snapshots=snapshots,
        provider_report=report,
        night_of=window.night_of,
    )


def resolve_night_window(
    *,
    night_of: date | datetime | None,
    snapshots: list[RadarVitalSnapshot],
    timezone_name: str,
) -> NightWindow:
    tz = _zoneinfo(timezone_name)
    resolved_night = _night_date(night_of, snapshots, tz)
    local_start = datetime.combine(resolved_night, DEFAULT_NIGHT_START, tzinfo=tz)
    local_end = datetime.combine(
        resolved_night + timedelta(days=1),
        DEFAULT_NIGHT_END,
        tzinfo=tz,
    )
    return NightWindow(
        night_of=resolved_night,
        timezone_name=timezone_name,
        start_at=local_start.astimezone(timezone.utc),
        end_at=local_end.astimezone(timezone.utc),
    )


def _resolve_subject_id(
    device: RadarDevice,
    snapshots: list[RadarVitalSnapshot],
    provider_report: RadarNightSummary | None,
) -> str | None:
    subject_id = provider_report.subject_id if provider_report else None
    subject_id = subject_id or device.bound_subject_id
    candidates = {
        snapshot.subject_id
        for snapshot in snapshots
        if snapshot.subject_id is not None
    }
    if device.bound_subject_id is not None:
        candidates.add(device.bound_subject_id)
    if provider_report and provider_report.subject_id is not None:
        candidates.add(provider_report.subject_id)
    if len(candidates) > 1:
        raise DeviceBindingMismatch("canonical radar data contains multiple subject bindings")
    if subject_id is None and candidates:
        subject_id = next(iter(candidates))
    return subject_id


def _processing_bounds(
    *,
    window: NightWindow,
    snapshots: list[RadarVitalSnapshot],
    provider_report: RadarNightSummary | None,
) -> tuple[datetime, datetime]:
    start = window.start_at
    end = window.end_at
    if provider_report and provider_report.sleep_start_at and provider_report.sleep_end_at:
        start = min(start, _as_utc(provider_report.sleep_start_at) - timedelta(hours=1))
        end = max(end, _as_utc(provider_report.sleep_end_at) + timedelta(hours=1))
    elif provider_report and snapshots:
        start = min(start, min(_as_utc(snapshot.measured_at) for snapshot in snapshots))
        end = max(end, max(_as_utc(snapshot.measured_at) for snapshot in snapshots))
    return start, end


def _normalize_snapshot(snapshot: RadarVitalSnapshot) -> RadarVitalSnapshot:
    return snapshot.model_copy(
        update={
            "measured_at": _as_utc(snapshot.measured_at),
            "received_at": _as_utc(snapshot.received_at),
        }
    )


def _night_date(
    value: date | datetime | None,
    snapshots: list[RadarVitalSnapshot],
    tz: ZoneInfo,
) -> date:
    if isinstance(value, datetime):
        local = _as_utc(value).astimezone(tz)
        if local.time() >= DEFAULT_NIGHT_END:
            return local.date()
        return local.date() - timedelta(days=1)
    if isinstance(value, date):
        return value
    if snapshots:
        first_local = min(_as_utc(item.measured_at) for item in snapshots).astimezone(tz)
        if first_local.time() >= DEFAULT_NIGHT_END:
            return first_local.date()
        return first_local.date() - timedelta(days=1)
    return datetime.now(tz).date()


def _coverage_ratio(
    report: RadarNightSummary | None,
    ordered: list[RadarVitalSnapshot],
) -> float:
    if report is not None:
        return report.data_coverage_ratio
    if not ordered:
        return 0.0
    valid = sum(
        1
        for snapshot in ordered
        if not snapshot.invalid_reading_flags
        and snapshot.bed_presence != RadarBedPresence.UNKNOWN
    )
    return round(max(0.0, min(1.0, valid / len(ordered))), 2)


def _missing_intervals(snapshots: list[RadarVitalSnapshot]) -> list[str]:
    if len(snapshots) < 3:
        return []
    intervals: list[str] = []
    gaps = [
        (_as_utc(current.measured_at) - _as_utc(previous.measured_at)).total_seconds()
        for previous, current in zip(snapshots, snapshots[1:])
    ]
    typical_gap = (
        median(gap for gap in gaps if gap > 0)
        if any(gap > 0 for gap in gaps)
        else 0
    )
    max_expected_gap = max(typical_gap * 1.75, 90 * 60)
    for previous, current, gap in zip(snapshots, snapshots[1:], gaps):
        if gap > max_expected_gap or "missing_interval" in current.data_quality_flags:
            intervals.append(_format_interval(previous.measured_at, current.measured_at))
    return intervals


def _provider_delay_intervals(
    snapshots: list[RadarVitalSnapshot],
    max_delay_seconds: float,
) -> list[str]:
    intervals: list[str] = []
    for snapshot in snapshots:
        delay = (
            _as_utc(snapshot.received_at) - _as_utc(snapshot.measured_at)
        ).total_seconds()
        if delay > max_delay_seconds:
            intervals.append(f"provider_delay:{int(delay // 60)}m")
    return intervals


def _bed_presence_intervals(
    snapshots: list[RadarVitalSnapshot],
    *,
    presence: set[RadarBedPresence],
) -> list[str]:
    intervals: list[str] = []
    start: datetime | None = None
    previous: datetime | None = None
    for snapshot in snapshots:
        measured_at = _as_utc(snapshot.measured_at)
        if snapshot.bed_presence in presence:
            start = measured_at if start is None else start
            previous = measured_at
            continue
        if start is not None and previous is not None:
            intervals.append(_format_interval(start, previous))
        start = None
        previous = None
    if start is not None and previous is not None:
        intervals.append(_format_interval(start, previous))
    return intervals


def _interval_minutes(intervals: list[str]) -> float:
    total = 0.0
    for item in intervals:
        if "/" not in item:
            continue
        start_raw, end_raw = item.split("/", 1)
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        total += max(0.0, (end - start).total_seconds() / 60.0)
    return total


def _abnormal_reading_count(snapshot: RadarVitalSnapshot) -> int:
    count = 0
    if snapshot.heart_rate_bpm is not None and (
        snapshot.heart_rate_bpm < 35 or snapshot.heart_rate_bpm > 130
    ):
        count += 1
    if snapshot.breath_rate_bpm is not None and (
        snapshot.breath_rate_bpm < 6 or snapshot.breath_rate_bpm > 35
    ):
        count += 1
    if "vital_outlier" in snapshot.data_quality_flags:
        count += 1
    return count


def _movement_count(snapshots: list[RadarVitalSnapshot]) -> int:
    return sum(1 for snapshot in snapshots if (snapshot.body_movement or 0) >= 2.0)


def _explainable_metrics(
    *,
    ordered: list[RadarVitalSnapshot],
    coverage_ratio: float,
    in_bed_count: int,
    not_in_bed_count: int,
    not_in_bed_minutes: float,
    vital_fluctuation_count: int,
) -> dict[str, float | int | None]:
    heart_values = [
        snapshot.heart_rate_bpm
        for snapshot in ordered
        if snapshot.heart_rate_bpm is not None
        and snapshot.bed_presence == RadarBedPresence.IN_BED
    ]
    breath_values = [
        snapshot.breath_rate_bpm
        for snapshot in ordered
        if snapshot.breath_rate_bpm is not None
        and snapshot.bed_presence == RadarBedPresence.IN_BED
    ]
    movement_values = [
        snapshot.body_movement
        for snapshot in ordered
        if snapshot.body_movement is not None
    ]
    return {
        "coverage_ratio": coverage_ratio,
        "snapshot_count": len(ordered),
        "in_bed_snapshot_count": in_bed_count,
        "not_in_bed_snapshot_count": not_in_bed_count,
        "not_in_bed_minutes": round(not_in_bed_minutes, 2),
        "mean_heart_rate_bpm": _mean(heart_values),
        "mean_breath_rate_bpm": _mean(breath_values),
        "mean_body_movement": _mean(movement_values),
        "vital_fluctuation_count": vital_fluctuation_count,
    }


def _mean(values: list[int | float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _report_value(report: RadarNightSummary | None, key: str):
    return getattr(report, key) if report is not None else None


def _usable_report_value(
    report: RadarNightSummary | None,
    status: RadarDataQualityStatus,
    key: str,
):
    if status == RadarDataQualityStatus.UNUSABLE:
        return None
    return _report_value(report, key)


def _format_interval(start: datetime, end: datetime) -> str:
    return f"{_format_datetime(_as_utc(start))}/{_format_datetime(_as_utc(end))}"


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _zoneinfo(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _dedupe(items) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item in seen:
            continue
        deduped.append(item)
        seen.add(item)
    return deduped


__all__ = [
    "DataQualityGate",
    "DataQualityGateError",
    "DeviceBindingMismatch",
    "NightWindow",
    "resolve_night_window",
    "run_replay_night_slice",
]
