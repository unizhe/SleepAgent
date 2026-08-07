from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal, Protocol

from pydantic import Field

from sleepagent.product_device.api import (
    DEFAULT_FAKE_RADAR_DEVICE_ID,
    FakeRadarProductDataProvider,
)
from sleepagent.radar_agent.schemas import (
    RadarAgentSchema,
    RadarBedPresence,
    RadarDevice,
    RadarDeviceStatus,
    RadarNightSummary,
    RadarVitalSnapshot,
    radar_device_from_product_device,
    radar_night_summary_from_product_report,
    radar_vital_snapshot_from_product_snapshot,
)
from sleepagent.radar_agent.replay import (
    ReplayScenario,
    get_replay_scenario,
    replay_scenario_ids,
    scenario_now,
)


DEFAULT_REPLAY_SUBJECT_ID = "elder-demo-001"
DEFAULT_REPLAY_TIMEZONE = "Asia/Shanghai"
REPLAY_BASE_WAKE_TIME = datetime(2026, 7, 10, 6, 30, tzinfo=timezone.utc)
SUPPORTED_REPLAY_SCENARIOS = replay_scenario_ids()


class ProviderFaultState(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    OUTAGE = "outage"


class ProviderAuthenticationError(RuntimeError):
    """Raised when a provider call requires auth that was not supplied."""


class ProviderAuthReservation(RadarAgentSchema):
    required: bool = False
    scheme: Literal["none", "api_key", "bearer", "oauth2", "vendor_signed"] = "none"
    key_id: str | None = None
    token_subject: str | None = None
    scopes: list[str] = Field(default_factory=list)


class ProviderSignatureReservation(RadarAgentSchema):
    required: bool = False
    verified: bool = False
    algorithm: str | None = None
    key_id: str | None = None
    header_name: str = "x-radar-provider-signature"
    failure_reason: str | None = None


class ProviderTimestampAlignment(RadarAgentSchema):
    provider_timezone_name: str = DEFAULT_REPLAY_TIMEZONE
    canonical_timezone_name: str = "UTC"
    align_to_utc: bool = True
    clock_skew_tolerance_seconds: int = Field(default=300, ge=0)
    max_observed_delay_seconds: float = Field(default=0, ge=0)


class ProviderDeviceBinding(RadarAgentSchema):
    radar_device_id: str = Field(..., min_length=1)
    binding_status: Literal["bound", "unbound", "unknown"] = "unknown"
    subject_id: str | None = None
    vendor_device_id: str | None = None
    vendor_device_name: str | None = None
    vendor_home_id: str | None = None
    bound_at: datetime | None = None


class ProviderPullCheckpoint(RadarAgentSchema):
    radar_device_id: str = Field(..., min_length=1)
    cursor: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    last_snapshot_id: str | None = None
    last_event_at: datetime | None = None
    next_index: int = Field(default=0, ge=0)
    has_more: bool = False
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ProviderFaultStatus(RadarAgentSchema):
    state: ProviderFaultState = ProviderFaultState.AVAILABLE
    code: str | None = None
    message: str = ""
    retry_after_seconds: int | None = Field(default=None, ge=0)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReplayDiagnostics(RadarAgentSchema):
    scenario: str = Field(..., min_length=1)
    raw_event_count: int = Field(default=0, ge=0)
    canonical_snapshot_count: int = Field(default=0, ge=0)
    delayed_event_count: int = Field(default=0, ge=0)
    duplicate_event_count: int = Field(default=0, ge=0)
    missing_event_count: int = Field(default=0, ge=0)
    out_of_order_event_count: int = Field(default=0, ge=0)
    offline_event_count: int = Field(default=0, ge=0)
    empty_bed_snapshot_count: int = Field(default=0, ge=0)
    last_checkpoint: ProviderPullCheckpoint | None = None


class ProviderHealth(RadarAgentSchema):
    provider_name: str = Field(..., min_length=1)
    healthy: bool = True
    mode: str = Field(..., min_length=1)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    detail: str = ""
    supported_methods: list[str] = Field(
        default_factory=lambda: [
            "list_devices",
            "get_device",
            "pull_snapshots",
            "pull_night_report",
            "receive_webhook",
            "health_check",
        ]
    )
    auth: ProviderAuthReservation = Field(default_factory=ProviderAuthReservation)
    signature: ProviderSignatureReservation = Field(
        default_factory=ProviderSignatureReservation
    )
    timestamp_alignment: ProviderTimestampAlignment = Field(
        default_factory=ProviderTimestampAlignment
    )
    device_bindings: list[ProviderDeviceBinding] = Field(default_factory=list)
    checkpoint: ProviderPullCheckpoint | None = None
    fault_status: ProviderFaultStatus = Field(default_factory=ProviderFaultStatus)


class WebhookReceipt(RadarAgentSchema):
    provider_name: str = Field(..., min_length=1)
    accepted: bool
    duplicate: bool = False
    idempotency_key: str | None = None
    canonical_event_refs: list[str] = Field(default_factory=list)
    message: str = ""
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    signature: ProviderSignatureReservation = Field(
        default_factory=ProviderSignatureReservation
    )
    fault_status: ProviderFaultStatus = Field(default_factory=ProviderFaultStatus)


class RadarProvider(Protocol):
    """Provider contract all fake/replay/live radar adapters must satisfy."""

    provider_name: str

    def list_devices(
        self,
        *,
        auth: ProviderAuthReservation | None = None,
    ) -> list[RadarDevice]:
        ...

    def get_device(
        self,
        radar_device_id: str,
        *,
        auth: ProviderAuthReservation | None = None,
    ) -> RadarDevice:
        ...

    def pull_snapshots(
        self,
        radar_device_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: ProviderPullCheckpoint | str | None = None,
        limit: int | None = None,
        auth: ProviderAuthReservation | None = None,
    ) -> list[RadarVitalSnapshot]:
        ...

    def pull_night_report(
        self,
        radar_device_id: str,
        *,
        night_of: datetime | date | None = None,
        auth: ProviderAuthReservation | None = None,
    ) -> RadarNightSummary | None:
        ...

    def receive_webhook(
        self,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        auth: ProviderAuthReservation | None = None,
    ) -> WebhookReceipt:
        ...

    def health_check(self) -> ProviderHealth:
        ...


class ReplayRadarProvider:
    """Deterministic fake/replay provider for the v1 radar-provider contract."""

    provider_name = "replay"

    def __init__(
        self,
        delegate: FakeRadarProductDataProvider | None = None,
        *,
        scenario: str = "normal_night",
        now: datetime | None = None,
        require_auth: bool = False,
        require_signed_webhooks: bool = False,
    ) -> None:
        if scenario not in SUPPORTED_REPLAY_SCENARIOS:
            raise ValueError(f"Unsupported replay scenario: {scenario}.")
        self._delegate = delegate
        self._scenario = scenario
        self._catalog_scenario: ReplayScenario = get_replay_scenario(scenario)
        self._now = _as_utc(now or scenario_now(self._catalog_scenario))
        self._auth = ProviderAuthReservation(
            required=require_auth,
            scheme="api_key" if require_auth else "none",
            scopes=["radar:read", "radar:webhook"] if require_auth else [],
        )
        self._signature = ProviderSignatureReservation(
            required=require_signed_webhooks,
            algorithm="hmac-sha256" if require_signed_webhooks else None,
        )
        self._seen_webhook_keys: set[str] = set()
        self.last_pull_checkpoint: ProviderPullCheckpoint | None = None
        self.last_replay_diagnostics = ReplayDiagnostics(scenario=scenario)

        if delegate is None:
            self._devices = self._build_devices()
            self._snapshots = self._build_scenario_snapshots()
            self._night_summary = self._build_scenario_night_summary()
        else:
            self._devices = {}
            self._snapshots = []
            self._night_summary = None

    @property
    def scenario(self) -> str:
        return self._scenario

    def list_devices(
        self,
        *,
        auth: ProviderAuthReservation | None = None,
    ) -> list[RadarDevice]:
        self._assert_authorized(auth)
        if self._delegate is not None:
            return [
                radar_device_from_product_device(device)
                for device in self._delegate.list_devices()
            ]
        return sorted(self._devices.values(), key=lambda item: item.radar_device_id)

    def get_device(
        self,
        radar_device_id: str = DEFAULT_FAKE_RADAR_DEVICE_ID,
        *,
        auth: ProviderAuthReservation | None = None,
    ) -> RadarDevice:
        self._assert_authorized(auth)
        if self._delegate is not None:
            return radar_device_from_product_device(self._delegate.get_device(radar_device_id))
        return self._devices[radar_device_id]

    def pull_snapshots(
        self,
        radar_device_id: str = DEFAULT_FAKE_RADAR_DEVICE_ID,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        cursor: ProviderPullCheckpoint | str | None = None,
        limit: int | None = None,
        auth: ProviderAuthReservation | None = None,
    ) -> list[RadarVitalSnapshot]:
        self._assert_authorized(auth)
        aligned_since = _as_utc(since) if since is not None else None
        aligned_until = _as_utc(until) if until is not None else None
        if self._delegate is not None:
            snapshots = self._delegate.get_recent_snapshots(radar_device_id)
            if aligned_since is not None:
                snapshots = [
                    item for item in snapshots if _as_utc(item.measured_at) >= aligned_since
                ]
            if aligned_until is not None:
                snapshots = [
                    item for item in snapshots if _as_utc(item.measured_at) <= aligned_until
                ]
            canonical = [
                radar_vital_snapshot_from_product_snapshot(snapshot)
                for snapshot in snapshots
            ]
            self.last_pull_checkpoint = _checkpoint_for_batch(
                radar_device_id=radar_device_id,
                snapshots=canonical,
                since=aligned_since,
                until=aligned_until,
                next_index=len(canonical),
                has_more=False,
            )
            self.last_replay_diagnostics = self.last_replay_diagnostics.model_copy(
                update={
                    "canonical_snapshot_count": len(canonical),
                    "last_checkpoint": self.last_pull_checkpoint,
                }
            )
            return canonical

        self.get_device(radar_device_id, auth=auth)
        filtered = list(self._snapshots)
        if aligned_since is not None:
            filtered = [
                item for item in filtered if _as_utc(item.measured_at) >= aligned_since
            ]
        if aligned_until is not None:
            filtered = [
                item for item in filtered if _as_utc(item.measured_at) <= aligned_until
            ]
        filtered = sorted(filtered, key=lambda item: (item.measured_at, item.snapshot_id))

        start_index = _cursor_start_index(filtered, cursor)
        page = filtered[start_index:]
        has_more = False
        if limit is not None and limit >= 0 and len(page) > limit:
            page = page[:limit]
            has_more = True
        next_index = start_index + len(page)
        self.last_pull_checkpoint = _checkpoint_for_batch(
            radar_device_id=radar_device_id,
            snapshots=page,
            since=aligned_since,
            until=aligned_until,
            next_index=next_index,
            has_more=has_more,
        )
        self.last_replay_diagnostics = self._build_diagnostics().model_copy(
            update={"last_checkpoint": self.last_pull_checkpoint}
        )
        return page

    def pull_night_report(
        self,
        radar_device_id: str = DEFAULT_FAKE_RADAR_DEVICE_ID,
        *,
        night_of: datetime | date | None = None,
        auth: ProviderAuthReservation | None = None,
    ) -> RadarNightSummary | None:
        self._assert_authorized(auth)
        if self._delegate is not None:
            report = self._delegate.get_latest_sleep_report(radar_device_id)
            if report is None or night_of is None:
                return (
                    radar_night_summary_from_product_report(report)
                    if report is not None
                    else None
                )
            requested_night = _night_date(night_of)
            if report.report_date != requested_night:
                return None
            return radar_night_summary_from_product_report(report)

        self.get_device(radar_device_id, auth=auth)
        if night_of is not None and self._night_summary is not None:
            if self._night_summary.night_of != _night_date(night_of):
                return None
        return self._night_summary

    def receive_webhook(
        self,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        auth: ProviderAuthReservation | None = None,
    ) -> WebhookReceipt:
        self._assert_authorized(auth)
        signature = self._verify_signature(headers or {})
        idempotency_key = _idempotency_key(payload)
        duplicate = bool(idempotency_key and idempotency_key in self._seen_webhook_keys)
        if idempotency_key and not duplicate:
            self._seen_webhook_keys.add(idempotency_key)

        accepted = signature.failure_reason is None
        canonical_refs = (
            []
            if duplicate or not accepted or idempotency_key is None
            else [f"webhook:{idempotency_key}"]
        )
        message = "Replay provider accepted webhook."
        if duplicate:
            message = "Replay provider ignored duplicate idempotent webhook."
        if not accepted:
            message = signature.failure_reason or "Webhook signature rejected."

        return WebhookReceipt(
            provider_name=self.provider_name,
            accepted=accepted,
            duplicate=duplicate,
            idempotency_key=idempotency_key,
            canonical_event_refs=canonical_refs,
            message=message,
            signature=signature,
            fault_status=self._fault_status(),
        )

    def health_check(self) -> ProviderHealth:
        fault_status = self._fault_status()
        if self._delegate is not None:
            devices = [
                radar_device_from_product_device(device)
                for device in self._delegate.list_devices()
            ]
        else:
            devices = list(self._devices.values())
        device_bindings = [
            _binding_for_device(device) for device in devices
        ]
        return ProviderHealth(
            provider_name=self.provider_name,
            healthy=fault_status.state != ProviderFaultState.OUTAGE,
            mode="fake_replay",
            detail=f"Deterministic replay scenario: {self._scenario}.",
            auth=self._auth,
            signature=self._signature,
            timestamp_alignment=ProviderTimestampAlignment(
                max_observed_delay_seconds=_max_delay_seconds(self._snapshots)
            ),
            device_bindings=device_bindings,
            checkpoint=self.last_pull_checkpoint,
            fault_status=fault_status,
        )

    def _assert_authorized(self, auth: ProviderAuthReservation | None) -> None:
        if not self._auth.required:
            return
        if auth is None or not auth.key_id:
            raise ProviderAuthenticationError("Radar provider auth is required.")

    def _verify_signature(
        self,
        headers: dict[str, str],
    ) -> ProviderSignatureReservation:
        if not self._signature.required:
            return self._signature.model_copy(update={"verified": False})
        header_name = self._signature.header_name
        signature = headers.get(header_name) or headers.get(header_name.title())
        if not signature:
            return self._signature.model_copy(
                update={
                    "verified": False,
                    "failure_reason": f"Missing required {header_name} header.",
                }
            )
        return self._signature.model_copy(
            update={
                "verified": True,
                "key_id": "replay-webhook-key",
                "failure_reason": None,
            }
        )

    def _build_devices(self) -> dict[str, RadarDevice]:
        scenario_input = self._catalog_scenario.deterministic_input
        now = self._now
        device = RadarDevice(
            radar_device_id=scenario_input.radar_device_id,
            display_name=f"Replay bedroom radar - {self._catalog_scenario.title}",
            provider=self.provider_name,
            status=RadarDeviceStatus(scenario_input.device_status),
            vendor_device_id="vendor-device-demo-001",
            vendor_device_name="imei-demo-001",
            vendor_home_id="home-demo-001",
            bound_subject_id=scenario_input.subject_id,
            timezone_name=scenario_input.timezone_name,
            source_raw_event_ids=[f"{self._scenario}:device"],
            registered_at=now - timedelta(days=30),
            updated_at=now,
        )
        return {device.radar_device_id: device}

    def _build_scenario_snapshots(self) -> list[RadarVitalSnapshot]:
        scenario_input = self._catalog_scenario.deterministic_input
        snapshots: list[RadarVitalSnapshot] = []
        for snapshot_input in scenario_input.snapshots:
            snapshot = RadarVitalSnapshot(
                snapshot_id=snapshot_input.snapshot_id,
                radar_device_id=scenario_input.radar_device_id,
                subject_id=scenario_input.subject_id,
                measured_at=_as_utc(snapshot_input.measured_at),
                received_at=_as_utc(snapshot_input.received_at),
                heart_rate_bpm=snapshot_input.heart_rate_bpm,
                breath_rate_bpm=snapshot_input.breath_rate_bpm,
                body_movement=snapshot_input.body_movement,
                bed_presence=snapshot_input.bed_presence,
                invalid_reading_flags=snapshot_input.invalid_reading_flags,
                data_quality_flags=snapshot_input.data_quality_flags,
                source_raw_event_ids=[f"{self._scenario}:raw:{snapshot_input.snapshot_id}"],
            )
            snapshots.append(snapshot)
        return _dedupe_snapshots(snapshots)

    def _build_scenario_night_summary(self) -> RadarNightSummary:
        scenario_input = self._catalog_scenario.deterministic_input
        report = scenario_input.night_report
        return RadarNightSummary(
            radar_device_id=scenario_input.radar_device_id,
            subject_id=scenario_input.subject_id,
            night_of=report.night_of,
            timezone_name=scenario_input.timezone_name,
            sleep_start_at=report.sleep_start_at,
            sleep_end_at=report.sleep_end_at,
            total_sleep_minutes=report.total_sleep_minutes,
            sleep_score=report.sleep_score,
            out_of_bed_count=report.out_of_bed_count,
            movement_count=report.movement_count,
            data_coverage_ratio=report.data_coverage_ratio,
            invalid_reading_count=report.invalid_reading_count,
            missing_intervals=report.missing_intervals,
            source_snapshot_ids=[snapshot.snapshot_id for snapshot in self._snapshots],
            source_raw_event_ids=[
                raw_event_id
                for snapshot in self._snapshots
                for raw_event_id in snapshot.source_raw_event_ids
            ],
            source_report_ref=f"replay-night-report:{self._scenario}",
            generated_at=self._now,
        )

    def _build_diagnostics(self) -> ReplayDiagnostics:
        anomalies = set(self._catalog_scenario.deterministic_input.anomalies)
        delayed_count = sum(
            1 for item in self._snapshots if "provider_delay" in item.data_quality_flags
        )
        out_of_bed_count = sum(
            1
            for item in self._snapshots
            if item.bed_presence == RadarBedPresence.OUT_OF_BED
        )
        missing_count = (
            len(self._night_summary.missing_intervals) if self._night_summary else 0
        )
        return ReplayDiagnostics(
            scenario=self._scenario,
            raw_event_count=len(self._snapshots),
            canonical_snapshot_count=len(self._snapshots),
            delayed_event_count=delayed_count,
            duplicate_event_count=1 if "duplicate" in anomalies else 0,
            missing_event_count=missing_count if "missing_events" in anomalies else 0,
            out_of_order_event_count=1 if "out_of_order" in anomalies else 0,
            offline_event_count=1 if "offline" in anomalies else 0,
            empty_bed_snapshot_count=out_of_bed_count,
        )

    def _fault_status(self) -> ProviderFaultStatus:
        expected = self._catalog_scenario.expected
        if expected.data_quality.device_offline:
            return ProviderFaultStatus(
                state=ProviderFaultState.OUTAGE,
                code="device_offline",
                message="Replay radar device is offline for the requested night.",
                retry_after_seconds=900,
                observed_at=self._now,
            )
        if expected.data_quality.status in {"partial", "unusable"}:
            return ProviderFaultStatus(
                state=ProviderFaultState.DEGRADED,
                code=f"data_quality_{expected.data_quality.status}",
                message=f"Replay scenario data quality is {expected.data_quality.status}.",
                retry_after_seconds=300,
                observed_at=self._now,
            )
        return ProviderFaultStatus(observed_at=self._now)


FakeRadarProvider = ReplayRadarProvider


def _checkpoint_for_batch(
    *,
    radar_device_id: str,
    snapshots: list[RadarVitalSnapshot],
    since: datetime | None,
    until: datetime | None,
    next_index: int,
    has_more: bool,
) -> ProviderPullCheckpoint:
    last_snapshot = snapshots[-1] if snapshots else None
    return ProviderPullCheckpoint(
        radar_device_id=radar_device_id,
        cursor=f"{radar_device_id}:{next_index}",
        since=since,
        until=until,
        last_snapshot_id=last_snapshot.snapshot_id if last_snapshot else None,
        last_event_at=last_snapshot.measured_at if last_snapshot else None,
        next_index=next_index,
        has_more=has_more,
    )


def _cursor_start_index(
    snapshots: list[RadarVitalSnapshot],
    cursor: ProviderPullCheckpoint | str | None,
) -> int:
    if cursor is None:
        return 0
    if isinstance(cursor, ProviderPullCheckpoint):
        return min(cursor.next_index, len(snapshots))
    try:
        return min(max(int(cursor.rsplit(":", 1)[-1]), 0), len(snapshots))
    except ValueError:
        return 0


def _dedupe_snapshots(snapshots: list[RadarVitalSnapshot]) -> list[RadarVitalSnapshot]:
    deduped: dict[str, RadarVitalSnapshot] = {}
    for snapshot in snapshots:
        deduped.setdefault(snapshot.snapshot_id, snapshot)
    return list(deduped.values())


def _binding_for_device(device: RadarDevice) -> ProviderDeviceBinding:
    return ProviderDeviceBinding(
        radar_device_id=device.radar_device_id,
        binding_status="bound" if device.bound_subject_id else "unbound",
        subject_id=device.bound_subject_id,
        vendor_device_id=device.vendor_device_id,
        vendor_device_name=device.vendor_device_name,
        vendor_home_id=device.vendor_home_id,
        bound_at=device.registered_at if device.bound_subject_id else None,
    )


def _idempotency_key(payload: dict[str, Any]) -> str | None:
    key = payload.get("message_id") or payload.get("messageId") or payload.get("event_id")
    return str(key) if key else None


def _night_date(value: datetime | date) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def _max_delay_seconds(snapshots: list[RadarVitalSnapshot]) -> float:
    max_delay = 0.0
    for snapshot in snapshots:
        delay = (_as_utc(snapshot.received_at) - _as_utc(snapshot.measured_at)).total_seconds()
        max_delay = max(max_delay, delay)
    return max_delay


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


__all__ = [
    "FakeRadarProvider",
    "ProviderAuthReservation",
    "ProviderAuthenticationError",
    "ProviderDeviceBinding",
    "ProviderFaultState",
    "ProviderFaultStatus",
    "ProviderHealth",
    "ProviderPullCheckpoint",
    "ProviderSignatureReservation",
    "ProviderTimestampAlignment",
    "RadarProvider",
    "ReplayDiagnostics",
    "ReplayRadarProvider",
    "SUPPORTED_REPLAY_SCENARIOS",
    "WebhookReceipt",
]
