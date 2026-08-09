from __future__ import annotations

from datetime import datetime, timezone

from sleepagent.product_device.quality import (
    evaluate_radar_data_quality,
    latest_vital_snapshot,
)
from sleepagent.product_device.schemas import (
    RadarAlertEvent,
    RadarAlertSeverity,
    RadarBedPresence,
    RadarDashboardSummary,
    RadarDataQuality,
    RadarDevice,
    RadarDeviceStatus,
    RadarSleepReport,
    RadarSourceMetadata,
    RadarVitalSnapshot,
)


class RadarDashboardProjectionTool:
    """Deterministically project normalized radar data into dashboard state.

    This boundary performs no goal selection, collaboration, persistence, or
    external I/O. It is therefore a Tool used by product surfaces rather than
    an Agent identity.
    """

    def run(
        self,
        *,
        device: RadarDevice,
        recent_snapshots: list[RadarVitalSnapshot],
        latest_sleep_report: RadarSleepReport | None = None,
        recent_alerts: list[RadarAlertEvent] | None = None,
        now: datetime | None = None,
    ) -> RadarDashboardSummary:
        resolved_now = now or datetime.now(timezone.utc)
        alerts = sorted(
            recent_alerts or [],
            key=lambda item: item.occurred_at,
            reverse=True,
        )
        current_snapshot = latest_vital_snapshot(recent_snapshots)
        data_quality = evaluate_radar_data_quality(
            device=device,
            recent_snapshots=recent_snapshots,
            latest_sleep_report=latest_sleep_report,
            now=resolved_now,
        )
        status_line = _build_status_line(device, current_snapshot, data_quality)
        highlights = _build_highlights(
            device=device,
            current_snapshot=current_snapshot,
            latest_sleep_report=latest_sleep_report,
            alerts=alerts,
            blocks_current_values=data_quality.blocks_current_values,
        )
        trend_observations = _build_trend_observations(
            recent_snapshots=recent_snapshots,
            latest_sleep_report=latest_sleep_report,
        )
        recommended_actions = _build_recommended_actions(data_quality)
        summary_text = _join_summary_text(status_line, highlights, data_quality.caveats)

        return RadarDashboardSummary(
            radar_device_id=device.radar_device_id,
            device=device,
            current_snapshot=current_snapshot,
            latest_sleep_report=latest_sleep_report,
            recent_alerts=alerts[:10],
            data_quality=data_quality,
            status_line=status_line,
            summary_text=summary_text,
            highlights=highlights,
            trend_observations=trend_observations,
            recommended_actions=recommended_actions,
            caveats=data_quality.caveats,
            blocked_reasons=data_quality.blocked_reasons,
            source_metadata=_collect_source_metadata(
                device=device,
                recent_snapshots=recent_snapshots,
                latest_sleep_report=latest_sleep_report,
                recent_alerts=alerts,
            ),
            generated_at=resolved_now,
        )

    def build_dashboard_summary(
        self,
        *,
        device: RadarDevice,
        recent_snapshots: list[RadarVitalSnapshot],
        latest_sleep_report: RadarSleepReport | None = None,
        recent_alerts: list[RadarAlertEvent] | None = None,
        now: datetime | None = None,
    ) -> RadarDashboardSummary:
        """Compatibility spelling for callers migrating from the old Agent."""

        return self.run(
            device=device,
            recent_snapshots=recent_snapshots,
            latest_sleep_report=latest_sleep_report,
            recent_alerts=recent_alerts,
            now=now,
        )


def _build_status_line(
    device: RadarDevice,
    current_snapshot: RadarVitalSnapshot | None,
    data_quality: RadarDataQuality,
) -> str:
    if device.status == RadarDeviceStatus.OFFLINE:
        return "Device is offline; current radar readings should not be interpreted."
    if current_snapshot is None:
        return "No current radar vital snapshot is available."
    if current_snapshot.bed_presence == RadarBedPresence.OUT_OF_BED:
        return "User appears out of bed; current in-bed readings are limited."
    if data_quality.stale:
        return "Latest radar snapshot is stale; wait for a fresh reading."
    if device.status == RadarDeviceStatus.ONLINE:
        return "Device is online and the latest in-bed snapshot is available."
    return "Device status is unknown; interpret current readings with caution."


def _build_highlights(
    *,
    device: RadarDevice,
    current_snapshot: RadarVitalSnapshot | None,
    latest_sleep_report: RadarSleepReport | None,
    alerts: list[RadarAlertEvent],
    blocks_current_values: bool,
) -> list[str]:
    highlights: list[str] = [f"Device status: {device.status.value}."]
    if current_snapshot is not None:
        if blocks_current_values:
            highlights.append(
                "Current vital values are caveated because the latest snapshot is not fully usable."
            )
        else:
            highlights.append(_snapshot_highlight(current_snapshot))

    if latest_sleep_report is not None:
        highlights.append(_sleep_report_highlight(latest_sleep_report))

    if alerts:
        unresolved = [alert for alert in alerts if alert.resolved_at is None]
        warning_or_higher = [
            alert
            for alert in alerts
            if alert.severity
            in {RadarAlertSeverity.WARNING, RadarAlertSeverity.CRITICAL}
        ]
        highlights.append(
            f"Recent alerts: {len(alerts)} total, {len(unresolved)} unresolved, "
            f"{len(warning_or_higher)} warning or critical."
        )
    else:
        highlights.append("No recent radar alerts were provided.")
    return highlights


def _snapshot_highlight(snapshot: RadarVitalSnapshot) -> str:
    heart_rate = _format_optional_number(snapshot.heart_rate_bpm, "bpm")
    breath_rate = _format_optional_number(snapshot.breath_rate_bpm, "bpm")
    return (
        "Latest in-bed snapshot: "
        f"heart rate {heart_rate}, breath rate {breath_rate}, "
        f"movement {_format_optional_number(snapshot.body_movement, '')}."
    )


def _sleep_report_highlight(report: RadarSleepReport) -> str:
    total_sleep = _format_optional_number(report.total_sleep_minutes, "min")
    score = _format_optional_number(report.sleep_score, "")
    return (
        f"Latest sleep report for {report.report_date.isoformat()}: "
        f"total sleep {total_sleep}, sleep score {score}."
    )


def _build_trend_observations(
    *,
    recent_snapshots: list[RadarVitalSnapshot],
    latest_sleep_report: RadarSleepReport | None,
) -> list[str]:
    observations: list[str] = []
    in_bed_count = sum(
        1 for snapshot in recent_snapshots if snapshot.bed_presence == RadarBedPresence.IN_BED
    )
    out_of_bed_count = sum(
        1
        for snapshot in recent_snapshots
        if snapshot.bed_presence == RadarBedPresence.OUT_OF_BED
    )
    if recent_snapshots:
        observations.append(
            f"Recent snapshots include {in_bed_count} in-bed and {out_of_bed_count} out-of-bed readings."
        )
    if latest_sleep_report is not None:
        getups = (
            str(latest_sleep_report.getup_count)
            if latest_sleep_report.getup_count is not None
            else "unknown"
        )
        movement = (
            str(latest_sleep_report.movement_count)
            if latest_sleep_report.movement_count is not None
            else "unknown"
        )
        observations.append(
            f"Latest sleep report movement count: {movement}; get-up count: {getups}."
        )
    return observations


def _build_recommended_actions(data_quality: RadarDataQuality) -> list[str]:
    if data_quality.blocks_current_values:
        return [
            "Refresh device connectivity and wait for a fresh in-bed snapshot before interpreting current vital values.",
            "Use older sleep and alert records only as historical context until fresh radar data arrives.",
        ]
    if data_quality.caveats:
        return [
            "Review the listed data caveats before comparing this dashboard with prior days.",
        ]
    return [
        "Continue observing bedtime, wake time, in-bed duration, and alert patterns for lifestyle trend context.",
    ]


def _join_summary_text(
    status_line: str,
    highlights: list[str],
    caveats: list[str],
) -> str:
    parts = [status_line, *highlights[:2]]
    if caveats:
        parts.append("Caveats: " + " ".join(caveats))
    return " ".join(part for part in parts if part)


def _collect_source_metadata(
    *,
    device: RadarDevice,
    recent_snapshots: list[RadarVitalSnapshot],
    latest_sleep_report: RadarSleepReport | None,
    recent_alerts: list[RadarAlertEvent],
) -> list[RadarSourceMetadata]:
    source_metadata = [device.source_metadata]
    source_metadata.extend(snapshot.source_metadata for snapshot in recent_snapshots)
    if latest_sleep_report is not None:
        source_metadata.append(latest_sleep_report.source_metadata)
    source_metadata.extend(alert.source_metadata for alert in recent_alerts)
    return _dedupe_source_metadata(source_metadata)


def _dedupe_source_metadata(
    source_metadata: list[RadarSourceMetadata],
) -> list[RadarSourceMetadata]:
    deduped: list[RadarSourceMetadata] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for item in source_metadata:
        key = (item.vendor, item.raw_event_id, item.vendor_message_id)
        if key in seen:
            continue
        deduped.append(item)
        seen.add(key)
    return deduped


def _format_optional_number(value: float | int | None, suffix: str) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float) and not value.is_integer():
        text = f"{value:.1f}"
    else:
        text = str(int(value))
    return f"{text} {suffix}".strip()
