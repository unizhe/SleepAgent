from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

from pydantic import Field

from sleepagent.observability import log_event, record_pull
from sleepagent.product_device.dashboard_tool import RadarDashboardProjectionTool
from sleepagent.product_device.schemas import (
    ProductDeviceSchema,
    RadarAlertEvent,
    RadarAlertSeverity,
    RadarBedPresence,
    RadarDashboardSummary,
    RadarDataQuality,
    RadarDevice,
    RadarDeviceStatus,
    RadarDialogueStatus,
    RadarSleepReport,
    RadarSleepStage,
    RadarSleepStageSegment,
    RadarSourceMetadata,
    RadarVitalSnapshot,
)
from sleepagent.simulation.replay import (
    ReplayScenario,
    default_replay_scenario_id,
    get_replay_scenario,
    scenario_now,
)


PRODUCT_RADAR_API_KEY_ENV = "SLEEPAGENT_PRODUCT_RADAR_API_KEY"
DEFAULT_FAKE_RADAR_DEVICE_ID = "radar-device-demo-001"


class RadarPublicDevice(ProductDeviceSchema):
    radar_device_id: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    status: RadarDeviceStatus = RadarDeviceStatus.UNKNOWN
    timezone_name: str = "UTC"
    registered_at: datetime
    updated_at: datetime


class RadarPublicVitalSnapshot(ProductDeviceSchema):
    radar_device_id: str = Field(..., min_length=1)
    measured_at: datetime
    received_at: datetime
    heart_rate_bpm: int | None = Field(default=None, ge=0, le=240)
    breath_rate_bpm: int | None = Field(default=None, ge=0, le=80)
    body_movement: float | None = Field(default=None, ge=0)
    bed_presence: RadarBedPresence = RadarBedPresence.UNKNOWN
    invalid_reading_flags: list[str] = Field(default_factory=list)


class RadarPublicSleepStageSegment(ProductDeviceSchema):
    radar_device_id: str = Field(..., min_length=1)
    start_at: datetime
    end_at: datetime
    stage: RadarSleepStage
    confidence: float | None = Field(default=None, ge=0, le=1)


class RadarPublicSleepReport(ProductDeviceSchema):
    radar_device_id: str = Field(..., min_length=1)
    report_date: date
    sleep_start_at: datetime | None = None
    sleep_end_at: datetime | None = None
    total_sleep_minutes: float | None = Field(default=None, ge=0)
    sleep_score: float | None = Field(default=None, ge=0, le=100)
    deep_sleep_minutes: float | None = Field(default=None, ge=0)
    light_sleep_minutes: float | None = Field(default=None, ge=0)
    rem_sleep_minutes: float | None = Field(default=None, ge=0)
    awake_minutes: float | None = Field(default=None, ge=0)
    movement_count: int | None = Field(default=None, ge=0)
    getup_count: int | None = Field(default=None, ge=0)
    stage_segments: list[RadarPublicSleepStageSegment] = Field(default_factory=list)


class RadarPublicAlertEvent(ProductDeviceSchema):
    radar_alert_event_id: str = Field(..., min_length=1)
    radar_device_id: str = Field(..., min_length=1)
    alert_type: str = Field(..., min_length=1)
    severity: RadarAlertSeverity = RadarAlertSeverity.UNKNOWN
    occurred_at: datetime
    resolved_at: datetime | None = None
    title: str | None = None
    message: str | None = None


class RadarPublicDashboardSummary(ProductDeviceSchema):
    radar_device_id: str = Field(..., min_length=1)
    device: RadarPublicDevice
    current_snapshot: RadarPublicVitalSnapshot | None = None
    latest_sleep_report: RadarPublicSleepReport | None = None
    recent_alerts: list[RadarPublicAlertEvent] = Field(default_factory=list)
    data_quality: RadarDataQuality = Field(default_factory=RadarDataQuality)
    status_line: str = ""
    summary_text: str = ""
    highlights: list[str] = Field(default_factory=list)
    trend_observations: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    generated_at: datetime


class RadarProductChatRequest(ProductDeviceSchema):
    radar_device_id: str = Field(..., min_length=1)
    user_message: str = Field(..., min_length=1)
    locale: str = "zh-CN"


class RadarPublicDialogueResult(ProductDeviceSchema):
    radar_device_id: str = Field(..., min_length=1)
    status: RadarDialogueStatus
    assistant_message: str = Field(..., min_length=1)
    safety_flags: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    generated_at: datetime


class RadarRefreshResult(ProductDeviceSchema):
    radar_device_id: str = Field(..., min_length=1)
    refreshed_at: datetime
    dashboard: RadarPublicDashboardSummary
    message: str = Field(..., min_length=1)


class RadarRealtimeState(ProductDeviceSchema):
    radar_device_id: str = Field(..., min_length=1)
    active: bool = False
    started_at: datetime | None = None
    latest_snapshot: RadarPublicVitalSnapshot | None = None
    data_quality: RadarDataQuality = Field(default_factory=RadarDataQuality)
    generated_at: datetime


class FakeRadarProductDataProvider:
    """In-memory fake radar provider for local product API development/tests."""

    def __init__(self, scenario_id: str | None = None) -> None:
        deployment = os.getenv("SLEEPAGENT_DEPLOYMENT_MODE", "").strip().lower()
        provider_mode = (
            os.getenv("SLEEPAGENT_PRODUCT_RADAR_PROVIDER_MODE", "")
            .strip()
            .lower()
        )
        namespace = (
            os.getenv("SLEEPAGENT_PRODUCT_RADAR_NAMESPACE", "").strip()
        )
        if (
            deployment not in {"development", "test"}
            or provider_mode != "fake"
            or not namespace.startswith("replay:")
        ):
            raise RuntimeError(
                "FakeRadarProductDataProvider requires explicit "
                "development/test mode, provider_mode=fake and a replay "
                "product namespace"
            )
        self.scenario = get_replay_scenario(scenario_id or default_replay_scenario_id())
        self.scenario_id = self.scenario.scenario_id
        now = scenario_now(self.scenario)
        scenario_input = self.scenario.deterministic_input
        self._devices: dict[str, RadarDevice] = {
            scenario_input.radar_device_id: RadarDevice(
                radar_device_id=scenario_input.radar_device_id,
                display_name=f"Bedroom radar - {self.scenario.title}",
                provider="replay",
                status=RadarDeviceStatus(scenario_input.device_status),
                vendor_device_id="vendor-device-demo-001",
                vendor_device_name="imei-demo-001",
                vendor_home_id="home-demo-001",
                timezone_name=scenario_input.timezone_name,
                source_metadata=_scenario_source_metadata(
                    scenario=self.scenario,
                    raw_event_id=f"{self.scenario_id}:device",
                    received_at=now,
                ),
                registered_at=now - timedelta(days=30),
                updated_at=now,
            )
        }
        self._snapshots: dict[str, list[RadarVitalSnapshot]] = {
            scenario_input.radar_device_id: [
                _scenario_snapshot(
                    scenario=self.scenario,
                    snapshot_input=snapshot_input,
                )
                for snapshot_input in scenario_input.snapshots
            ]
        }
        self._sleep_reports: dict[str, RadarSleepReport] = {
            scenario_input.radar_device_id: _scenario_sleep_report(
                scenario=self.scenario,
            )
        }
        self._alerts: dict[str, list[RadarAlertEvent]] = {
            scenario_input.radar_device_id: [
                _scenario_alert(
                    scenario=self.scenario,
                    alert_input=alert_input,
                )
                for alert_input in scenario_input.alerts
            ]
        }
        self._realtime_started_at: dict[str, datetime] = {}

    def list_devices(self) -> list[RadarDevice]:
        return sorted(self._devices.values(), key=lambda item: item.radar_device_id)

    def get_device(self, radar_device_id: str) -> RadarDevice:
        return self._devices[radar_device_id]

    def build_dashboard(self, radar_device_id: str) -> RadarDashboardSummary:
        dashboard = RadarDashboardProjectionTool().run(
            device=self.get_device(radar_device_id),
            recent_snapshots=self.get_recent_snapshots(radar_device_id),
            latest_sleep_report=self.get_latest_sleep_report(radar_device_id),
            recent_alerts=self.get_recent_alerts(radar_device_id),
            now=scenario_now(self.scenario),
        )
        return _apply_scenario_expected_quality(
            dashboard=dashboard,
            scenario=self.scenario,
        )

    def get_recent_snapshots(self, radar_device_id: str) -> list[RadarVitalSnapshot]:
        self.get_device(radar_device_id)
        return list(self._snapshots.get(radar_device_id, []))[-12:]

    def get_latest_sleep_report(self, radar_device_id: str) -> RadarSleepReport | None:
        self.get_device(radar_device_id)
        return self._sleep_reports.get(radar_device_id)

    def get_recent_alerts(self, radar_device_id: str) -> list[RadarAlertEvent]:
        self.get_device(radar_device_id)
        return list(self._alerts.get(radar_device_id, []))[-20:]

    def refresh_device(self, radar_device_id: str) -> RadarDashboardSummary:
        log_event(
            "vendor_call_start",
            source="fake_radar_product_provider",
            provider_mode="fake",
            endpoint="refresh_device",
            radar_device_id=radar_device_id,
        )
        device = self.get_device(radar_device_id)
        now = scenario_now(self.scenario) + timedelta(
            minutes=len(self._snapshots.get(radar_device_id, [])) + 1
        )
        snapshots = self._snapshots.setdefault(radar_device_id, [])
        snapshots.append(
            _fake_snapshot(
                radar_device_id=radar_device_id,
                measured_at=now,
                heart_rate_bpm=65 + len(snapshots) % 4,
                breath_rate_bpm=15,
                body_movement=1 + len(snapshots) % 2,
                bed_presence=RadarBedPresence.IN_BED,
                raw_event_id=f"fake-refresh-snapshot-{len(snapshots) + 1}",
            )
        )
        self._devices[radar_device_id] = device.model_copy(
            update={
                "status": RadarDeviceStatus.ONLINE,
                "updated_at": now,
            }
        )
        dashboard = self.build_dashboard(radar_device_id)
        record_pull(source="fake_radar_product_provider", endpoint="refresh_device")
        log_event(
            "vendor_call_success",
            source="fake_radar_product_provider",
            provider_mode="fake",
            endpoint="refresh_device",
            radar_device_id=radar_device_id,
        )
        return dashboard

    def start_realtime(self, radar_device_id: str) -> RadarRealtimeState:
        self.get_device(radar_device_id)
        started_at = datetime.now(timezone.utc)
        self._realtime_started_at[radar_device_id] = started_at
        dashboard = self.refresh_device(radar_device_id)
        return build_public_realtime_state(
            radar_device_id=radar_device_id,
            active=True,
            started_at=started_at,
            dashboard=dashboard,
        )

    def get_realtime(self, radar_device_id: str) -> RadarRealtimeState:
        self.get_device(radar_device_id)
        started_at = self._realtime_started_at.get(radar_device_id)
        dashboard = (
            self.refresh_device(radar_device_id)
            if started_at is not None
            else self.build_dashboard(radar_device_id)
        )
        return build_public_realtime_state(
            radar_device_id=radar_device_id,
            active=started_at is not None,
            started_at=started_at,
            dashboard=dashboard,
        )


def build_public_device(device: RadarDevice) -> RadarPublicDevice:
    return RadarPublicDevice(
        radar_device_id=device.radar_device_id,
        display_name=device.display_name,
        status=device.status,
        timezone_name=device.timezone_name,
        registered_at=device.registered_at,
        updated_at=device.updated_at,
    )


def build_public_dashboard(
    dashboard: RadarDashboardSummary,
) -> RadarPublicDashboardSummary:
    return RadarPublicDashboardSummary(
        radar_device_id=dashboard.radar_device_id,
        device=build_public_device(dashboard.device),
        current_snapshot=(
            build_public_snapshot(dashboard.current_snapshot)
            if dashboard.current_snapshot is not None
            else None
        ),
        latest_sleep_report=(
            build_public_sleep_report(dashboard.latest_sleep_report)
            if dashboard.latest_sleep_report is not None
            else None
        ),
        recent_alerts=[
            build_public_alert(alert) for alert in dashboard.recent_alerts
        ],
        data_quality=dashboard.data_quality,
        status_line=dashboard.status_line,
        summary_text=dashboard.summary_text,
        highlights=dashboard.highlights,
        trend_observations=dashboard.trend_observations,
        recommended_actions=dashboard.recommended_actions,
        caveats=dashboard.caveats,
        blocked_reasons=dashboard.blocked_reasons,
        generated_at=dashboard.generated_at,
    )


def build_public_snapshot(
    snapshot: RadarVitalSnapshot,
) -> RadarPublicVitalSnapshot:
    return RadarPublicVitalSnapshot(
        radar_device_id=snapshot.radar_device_id,
        measured_at=snapshot.measured_at,
        received_at=snapshot.received_at,
        heart_rate_bpm=snapshot.heart_rate_bpm,
        breath_rate_bpm=snapshot.breath_rate_bpm,
        body_movement=snapshot.body_movement,
        bed_presence=snapshot.bed_presence,
        invalid_reading_flags=snapshot.invalid_reading_flags,
    )


def build_public_sleep_report(
    report: RadarSleepReport,
) -> RadarPublicSleepReport:
    return RadarPublicSleepReport(
        radar_device_id=report.radar_device_id,
        report_date=report.report_date,
        sleep_start_at=report.sleep_start_at,
        sleep_end_at=report.sleep_end_at,
        total_sleep_minutes=report.total_sleep_minutes,
        sleep_score=report.sleep_score,
        deep_sleep_minutes=report.deep_sleep_minutes,
        light_sleep_minutes=report.light_sleep_minutes,
        rem_sleep_minutes=report.rem_sleep_minutes,
        awake_minutes=report.awake_minutes,
        movement_count=report.movement_count,
        getup_count=report.getup_count,
        stage_segments=[
            build_public_sleep_stage_segment(segment)
            for segment in report.stage_segments
        ],
    )


def build_public_sleep_stage_segment(
    segment: RadarSleepStageSegment,
) -> RadarPublicSleepStageSegment:
    return RadarPublicSleepStageSegment(
        radar_device_id=segment.radar_device_id,
        start_at=segment.start_at,
        end_at=segment.end_at,
        stage=segment.stage,
        confidence=segment.confidence,
    )


def build_public_alert(alert: RadarAlertEvent) -> RadarPublicAlertEvent:
    return RadarPublicAlertEvent(
        radar_alert_event_id=alert.radar_alert_event_id,
        radar_device_id=alert.radar_device_id,
        alert_type=alert.alert_type,
        severity=alert.severity,
        occurred_at=alert.occurred_at,
        resolved_at=alert.resolved_at,
        title=alert.title,
        message=alert.message,
    )


def build_public_realtime_state(
    *,
    radar_device_id: str,
    active: bool,
    started_at: datetime | None,
    dashboard: RadarDashboardSummary,
) -> RadarRealtimeState:
    return RadarRealtimeState(
        radar_device_id=radar_device_id,
        active=active,
        started_at=started_at,
        latest_snapshot=(
            build_public_snapshot(dashboard.current_snapshot)
            if dashboard.current_snapshot is not None
            else None
        ),
        data_quality=dashboard.data_quality,
        generated_at=dashboard.generated_at,
    )


def scenario_trend_summary(scenario: ReplayScenario) -> list[dict[str, object]]:
    return [
        point.model_dump(mode="json", exclude={"standard_mappings"})
        for point in scenario.deterministic_input.trend_summary
    ]


def _scenario_snapshot(
    *,
    scenario: ReplayScenario,
    snapshot_input,
) -> RadarVitalSnapshot:
    return RadarVitalSnapshot(
        radar_device_id=scenario.deterministic_input.radar_device_id,
        measured_at=snapshot_input.measured_at,
        received_at=snapshot_input.received_at,
        heart_rate_bpm=snapshot_input.heart_rate_bpm,
        breath_rate_bpm=snapshot_input.breath_rate_bpm,
        body_movement=snapshot_input.body_movement,
        bed_presence=RadarBedPresence(snapshot_input.bed_presence.value),
        invalid_reading_flags=snapshot_input.invalid_reading_flags,
        source_metadata=_scenario_source_metadata(
            scenario=scenario,
            raw_event_id=f"{scenario.scenario_id}:snapshot:{snapshot_input.snapshot_id}",
            received_at=snapshot_input.received_at,
            data_payload={
                "data_quality_flags": snapshot_input.data_quality_flags,
                "invalid_reading_flags": snapshot_input.invalid_reading_flags,
            },
        ),
    )


def _scenario_sleep_report(*, scenario: ReplayScenario) -> RadarSleepReport:
    report = scenario.deterministic_input.night_report
    source_metadata = _scenario_source_metadata(
        scenario=scenario,
        raw_event_id=f"{scenario.scenario_id}:night-report",
        received_at=scenario_now(scenario),
    )
    return RadarSleepReport(
        radar_device_id=scenario.deterministic_input.radar_device_id,
        report_date=report.night_of,
        sleep_start_at=report.sleep_start_at,
        sleep_end_at=report.sleep_end_at,
        total_sleep_minutes=report.total_sleep_minutes,
        sleep_score=report.sleep_score,
        deep_sleep_minutes=report.deep_sleep_minutes,
        light_sleep_minutes=report.light_sleep_minutes,
        rem_sleep_minutes=report.rem_sleep_minutes,
        awake_minutes=report.awake_minutes,
        movement_count=report.movement_count,
        getup_count=report.out_of_bed_count,
        stage_segments=_scenario_stage_segments(
            scenario=scenario,
            source_metadata=source_metadata,
        ),
        source_metadata=source_metadata,
    )


def _scenario_stage_segments(
    *,
    scenario: ReplayScenario,
    source_metadata: RadarSourceMetadata,
) -> list[RadarSleepStageSegment]:
    report = scenario.deterministic_input.night_report
    if report.sleep_start_at is None or report.sleep_end_at is None:
        return []
    cursor = report.sleep_start_at
    segments: list[RadarSleepStageSegment] = []
    for stage, minutes in (
        (RadarSleepStage.LIGHT, report.light_sleep_minutes or 0),
        (RadarSleepStage.DEEP, report.deep_sleep_minutes or 0),
        (RadarSleepStage.REM, report.rem_sleep_minutes or 0),
    ):
        if minutes <= 0:
            continue
        end_at = min(cursor + timedelta(minutes=minutes), report.sleep_end_at)
        if end_at <= cursor:
            break
        segments.append(
            RadarSleepStageSegment(
                radar_device_id=scenario.deterministic_input.radar_device_id,
                start_at=cursor,
                end_at=end_at,
                stage=stage,
                confidence=0.72,
                source_metadata=source_metadata,
            )
        )
        cursor = end_at
        if cursor >= report.sleep_end_at:
            break
    return segments


def _scenario_alert(
    *,
    scenario: ReplayScenario,
    alert_input,
) -> RadarAlertEvent:
    return RadarAlertEvent(
        radar_alert_event_id=alert_input.alert_id,
        radar_device_id=scenario.deterministic_input.radar_device_id,
        alert_type=alert_input.alert_type,
        severity=RadarAlertSeverity(alert_input.severity),
        occurred_at=alert_input.occurred_at,
        resolved_at=alert_input.resolved_at,
        title=alert_input.title,
        message=alert_input.message,
        source_metadata=_scenario_source_metadata(
            scenario=scenario,
            raw_event_id=f"{scenario.scenario_id}:alert:{alert_input.alert_id}",
            received_at=alert_input.occurred_at,
        ),
    )


def _apply_scenario_expected_quality(
    *,
    dashboard: RadarDashboardSummary,
    scenario: ReplayScenario,
) -> RadarDashboardSummary:
    expected = scenario.expected.data_quality
    caveats = _scenario_quality_caveats(scenario)
    quality = RadarDataQuality(
        freshness_seconds=60,
        max_snapshot_age_seconds=300,
        stale=False,
        device_offline=expected.device_offline,
        user_out_of_bed=expected.user_out_of_bed,
        current_snapshot_available=dashboard.current_snapshot is not None,
        missing_intervals=expected.missing_intervals,
        missing_reading_count=expected.invalid_reading_count,
        invalid_reading_count=expected.invalid_reading_count,
        report_freshness_hours=0,
        max_report_age_hours=36,
        report_stale=False,
        partial_sleep_report=expected.status in {"partial", "unusable"},
        blocks_current_values=bool(expected.blocked_reasons),
        caveats=caveats,
        blocked_reasons=expected.blocked_reasons,
    )
    return dashboard.model_copy(
        update={
            "data_quality": quality,
            "status_line": _scenario_status_line(scenario),
            "summary_text": _scenario_summary_text(scenario),
            "caveats": caveats,
            "blocked_reasons": expected.blocked_reasons,
        }
    )


def _scenario_status_line(scenario: ReplayScenario) -> str:
    risk = scenario.expected.risk_level.value
    quality = scenario.expected.data_quality.status
    return f"Replay scenario {scenario.scenario_id}: quality={quality}, risk={risk}."


def _scenario_summary_text(scenario: ReplayScenario) -> str:
    report = scenario.deterministic_input.night_report
    return (
        f"{scenario.title}: coverage {report.data_coverage_ratio:.2f}, "
        f"sleep score {report.sleep_score if report.sleep_score is not None else 'unavailable'}, "
        f"out-of-bed {report.out_of_bed_count}, risk {scenario.expected.risk_level.value}."
    )


def _scenario_quality_caveats(scenario: ReplayScenario) -> list[str]:
    caveats = [
        "Replay data is deterministic and is not a live vendor feed.",
        "Radar observations are not medical diagnoses.",
    ]
    caveats.extend(scenario.expected.report_expectations.must_include_caveats)
    return _dedupe_strings(caveats)


def _dedupe_strings(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        deduped.append(value)
        seen.add(value)
    return deduped


def _scenario_source_metadata(
    *,
    scenario: ReplayScenario,
    raw_event_id: str,
    received_at: datetime,
    data_payload: dict[str, object] | None = None,
) -> RadarSourceMetadata:
    return RadarSourceMetadata(
        vendor="replay",
        vendor_event_type="replay_scenario_event",
        vendor_message_id=f"{raw_event_id}:message",
        vendor_product_id="replay-product-id",
        vendor_device_id="vendor-device-demo-001",
        vendor_device_name="imei-demo-001",
        vendor_home_id="home-demo-001",
        vendor_payload_timestamp=received_at,
        received_at=received_at,
        raw_event_id=raw_event_id,
        raw_payload={
            "scenario_id": scenario.scenario_id,
            "fixture_version": "radar-replay-v1-2026-07-10",
        },
        data_payload=data_payload or {},
    )


def _fake_snapshot(
    *,
    radar_device_id: str,
    measured_at: datetime,
    heart_rate_bpm: int,
    breath_rate_bpm: int,
    body_movement: float,
    bed_presence: RadarBedPresence,
    raw_event_id: str,
) -> RadarVitalSnapshot:
    return RadarVitalSnapshot(
        radar_device_id=radar_device_id,
        measured_at=measured_at,
        received_at=measured_at,
        heart_rate_bpm=heart_rate_bpm,
        breath_rate_bpm=breath_rate_bpm,
        body_movement=body_movement,
        bed_presence=bed_presence,
        invalid_reading_flags=[],
        source_metadata=_fake_source_metadata(
            raw_event_id=raw_event_id,
            received_at=measured_at,
        ),
    )


def _fake_sleep_report(
    *,
    radar_device_id: str,
    sleep_end_at: datetime,
) -> RadarSleepReport:
    sleep_start_at = sleep_end_at - timedelta(hours=7, minutes=20)
    source_metadata = _fake_source_metadata(
        raw_event_id="fake-sleep-report-001",
        received_at=sleep_end_at,
    )
    return RadarSleepReport(
        radar_device_id=radar_device_id,
        report_date=sleep_end_at.date(),
        sleep_start_at=sleep_start_at,
        sleep_end_at=sleep_end_at,
        total_sleep_minutes=405,
        sleep_score=82,
        deep_sleep_minutes=92,
        light_sleep_minutes=238,
        rem_sleep_minutes=58,
        awake_minutes=27,
        movement_count=10,
        getup_count=1,
        stage_segments=[
            RadarSleepStageSegment(
                radar_device_id=radar_device_id,
                start_at=sleep_start_at,
                end_at=sleep_start_at + timedelta(hours=2),
                stage=RadarSleepStage.LIGHT,
                confidence=0.78,
                source_metadata=source_metadata,
            ),
            RadarSleepStageSegment(
                radar_device_id=radar_device_id,
                start_at=sleep_start_at + timedelta(hours=2),
                end_at=sleep_start_at + timedelta(hours=3, minutes=30),
                stage=RadarSleepStage.DEEP,
                confidence=0.72,
                source_metadata=source_metadata,
            ),
            RadarSleepStageSegment(
                radar_device_id=radar_device_id,
                start_at=sleep_start_at + timedelta(hours=3, minutes=30),
                end_at=sleep_end_at,
                stage=RadarSleepStage.REM,
                confidence=0.7,
                source_metadata=source_metadata,
            ),
        ],
        source_metadata=source_metadata,
    )


def _fake_source_metadata(
    *,
    raw_event_id: str,
    received_at: datetime,
) -> RadarSourceMetadata:
    return RadarSourceMetadata(
        vendor="fake-perceptor",
        vendor_event_type="fake_product_event",
        vendor_message_id=f"{raw_event_id}-message",
        vendor_product_id="fake-product-id",
        vendor_device_id="vendor-device-demo-001",
        vendor_device_name="imei-demo-001",
        vendor_home_id="home-demo-001",
        received_at=received_at,
        raw_event_id=raw_event_id,
        raw_payload={
            "device_id": "vendor-device-demo-001",
            "device_name": "imei-demo-001",
            "home_id": "home-demo-001",
        },
    )
