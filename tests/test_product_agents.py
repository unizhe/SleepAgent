from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sleepagent.product_device as product_device
import sleepagent.product_device.api as product_device_api
from sleepagent.product_device import (
    RadarAlertEvent,
    RadarAlertSeverity,
    RadarBedPresence,
    RadarDashboardProjectionTool,
    RadarDevice,
    RadarDeviceStatus,
    RadarSleepReport,
    RadarSourceMetadata,
    RadarVitalSnapshot,
)


NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)


def test_dashboard_projection_tool_marks_data_quality_caveats_and_blocks() -> None:
    device = _device(status=RadarDeviceStatus.OFFLINE)
    snapshots = [
        _snapshot(
            measured_at=NOW - timedelta(minutes=45),
            heart_rate_bpm=68,
            breath_rate_bpm=15,
            bed_presence=RadarBedPresence.IN_BED,
            raw_event_id="snapshot-old",
        ),
        _snapshot(
            measured_at=NOW - timedelta(minutes=20),
            heart_rate_bpm=None,
            breath_rate_bpm=None,
            bed_presence=RadarBedPresence.OUT_OF_BED,
            invalid_reading_flags=[
                "heart_rate_invalid_minus_one",
                "breath_rate_invalid_minus_one",
            ],
            raw_event_id="snapshot-current",
        ),
    ]
    report = _sleep_report(
        sleep_end_at=NOW - timedelta(days=3),
        raw_event_id="report-stale",
    )
    alert = _alert(raw_event_id="alert-001")

    dashboard = RadarDashboardProjectionTool().run(
        device=device,
        recent_snapshots=snapshots,
        latest_sleep_report=report,
        recent_alerts=[alert],
        now=NOW,
    )

    assert dashboard.current_snapshot == snapshots[-1]
    assert dashboard.data_quality.stale is True
    assert dashboard.data_quality.device_offline is True
    assert dashboard.data_quality.user_out_of_bed is True
    assert dashboard.data_quality.missing_intervals == 1
    assert dashboard.data_quality.missing_reading_count == 2
    assert dashboard.data_quality.invalid_reading_count == 2
    assert dashboard.data_quality.report_stale is True
    assert dashboard.data_quality.blocks_current_values is True
    assert "device_offline" in dashboard.blocked_reasons
    assert "user_out_of_bed" in dashboard.blocked_reasons
    assert any("invalid -1" in caveat for caveat in dashboard.caveats)
    assert "stale" in dashboard.summary_text.lower()


def test_product_device_package_and_api_do_not_expose_or_instantiate_old_agents() -> None:
    for retired_name in (
        "DeviceCareAgent",
        "ProductDialogueAgent",
        "RadarProductAgent",
        "RadarSleepAgentService",
    ):
        assert not hasattr(product_device, retired_name)

    package_root = Path(product_device.__file__).resolve().parent
    assert not (package_root / "agents.py").exists()
    assert not (package_root / "dialogue.py").exists()
    assert not (package_root / "radar_agent.py").exists()

    api_source = inspect.getsource(product_device_api)
    assert "product_device.agents" not in api_source
    assert "RadarProductAgent(" not in api_source


def test_product_radar_routes_are_registered() -> None:
    from backend.legacy_main import app

    route_paths = {route.path for route in app.routes}

    assert "/product/radar/devices" in route_paths
    assert "/product/radar/devices/{radar_device_id}/dashboard" in route_paths
    assert "/product/radar/devices/{radar_device_id}/refresh" in route_paths
    assert "/product/radar/devices/{radar_device_id}/realtime/start" in route_paths
    assert "/product/radar/devices/{radar_device_id}/realtime" in route_paths
    assert "/product/radar/devices/{radar_device_id}/sleep-report" in route_paths
    assert "/product/radar/devices/{radar_device_id}/alerts" in route_paths
    assert "/product/radar/chat" in route_paths


def _device(*, status: RadarDeviceStatus) -> RadarDevice:
    source = _source("device-source")
    return RadarDevice(
        radar_device_id="radar-device-001",
        display_name="Bedroom radar",
        status=status,
        vendor_device_name="imei-001",
        source_metadata=source,
        updated_at=NOW,
    )


def _snapshot(
    *,
    measured_at: datetime,
    heart_rate_bpm: int | None,
    breath_rate_bpm: int | None,
    bed_presence: RadarBedPresence,
    invalid_reading_flags: list[str] | None = None,
    raw_event_id: str = "snapshot-source",
) -> RadarVitalSnapshot:
    return RadarVitalSnapshot(
        radar_device_id="radar-device-001",
        measured_at=measured_at,
        received_at=measured_at,
        heart_rate_bpm=heart_rate_bpm,
        breath_rate_bpm=breath_rate_bpm,
        body_movement=1,
        bed_presence=bed_presence,
        invalid_reading_flags=invalid_reading_flags or [],
        source_metadata=_source(raw_event_id),
    )


def _sleep_report(
    *,
    sleep_end_at: datetime,
    raw_event_id: str = "report-source",
) -> RadarSleepReport:
    return RadarSleepReport(
        radar_device_id="radar-device-001",
        report_date=sleep_end_at.date(),
        sleep_start_at=sleep_end_at - timedelta(hours=7),
        sleep_end_at=sleep_end_at,
        total_sleep_minutes=390,
        sleep_score=78,
        deep_sleep_minutes=80,
        light_sleep_minutes=230,
        rem_sleep_minutes=60,
        awake_minutes=20,
        movement_count=12,
        getup_count=1,
        source_metadata=_source(raw_event_id),
    )


def _alert(raw_event_id: str) -> RadarAlertEvent:
    return RadarAlertEvent(
        radar_alert_event_id="alert-001",
        radar_device_id="radar-device-001",
        alert_type="heart_rate",
        severity=RadarAlertSeverity.WARNING,
        occurred_at=NOW - timedelta(minutes=10),
        title="Heart rate alert",
        message="Vendor alert",
        source_metadata=_source(raw_event_id),
    )


def _source(raw_event_id: str) -> RadarSourceMetadata:
    return RadarSourceMetadata(
        vendor="perceptor",
        vendor_event_type="test_event",
        vendor_message_id=raw_event_id,
        vendor_device_name="imei-001",
        received_at=NOW,
        raw_event_id=raw_event_id,
        raw_payload={"id": raw_event_id},
    )
