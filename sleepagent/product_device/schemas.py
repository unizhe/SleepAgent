from __future__ import annotations

import json
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


DEFAULT_RADAR_VENDOR = "perceptor"
VITAL_SIGNS_DATA_EVENT = "VitalSignsDataEvent"
INVALID_VENDOR_READING = -1


class ProductDeviceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RadarDeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class RadarBedPresence(str, Enum):
    IN_BED = "in_bed"
    OUT_OF_BED = "out_of_bed"
    UNKNOWN = "unknown"


class RadarSleepStage(str, Enum):
    DEEP = "deep"
    LIGHT = "light"
    REM = "rem"
    AWAKE = "awake"
    UNKNOWN = "unknown"


class RadarAlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class RadarDialogueStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    LLM_NOT_CONFIGURED = "llm_not_configured"
    FAILED = "failed"


class RawVendorEventNormalizationStatus(str, Enum):
    RAW_ONLY = "raw_only"
    NORMALIZED = "normalized"
    UNNORMALIZED = "unnormalized"


class RadarSourceMetadata(ProductDeviceSchema):
    """Trace a normalized product-device record back to vendor payloads."""

    vendor: str = Field(..., min_length=1)
    vendor_event_type: str | None = None
    vendor_message_id: str | None = None
    vendor_product_id: str | None = None
    vendor_device_id: str | None = None
    vendor_device_name: str | None = None
    vendor_home_id: str | None = None
    vendor_payload_timestamp: datetime | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_event_id: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    data_payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def device_identifier(self) -> str | None:
        return self.vendor_device_name or self.vendor_device_id


class RawVendorEvent(ProductDeviceSchema):
    raw_event_id: str
    vendor: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    message_id: str | None = None
    product_id: str | None = None
    device_id: str | None = None
    device_name: str | None = None
    home_id: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event_timestamp: datetime | None = None
    raw_payload: dict[str, Any]
    data_payload: dict[str, Any] = Field(default_factory=dict)
    normalization_status: RawVendorEventNormalizationStatus = (
        RawVendorEventNormalizationStatus.RAW_ONLY
    )
    unnormalized_reason: str | None = None

    @property
    def device_identifier(self) -> str | None:
        return self.device_name or self.device_id

    def to_source_metadata(self) -> RadarSourceMetadata:
        return RadarSourceMetadata(
            vendor=self.vendor,
            vendor_event_type=self.event_type,
            vendor_message_id=self.message_id,
            vendor_product_id=self.product_id,
            vendor_device_id=self.device_id,
            vendor_device_name=self.device_name,
            vendor_home_id=self.home_id,
            vendor_payload_timestamp=self.event_timestamp,
            received_at=self.received_at,
            raw_event_id=self.raw_event_id,
            raw_payload=self.raw_payload,
            data_payload=self.data_payload,
        )


class RadarDevice(ProductDeviceSchema):
    radar_device_id: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    provider: str = Field(default=DEFAULT_RADAR_VENDOR, min_length=1)
    status: RadarDeviceStatus = RadarDeviceStatus.UNKNOWN
    vendor_device_id: str | None = None
    vendor_device_name: str | None = None
    vendor_home_id: str | None = None
    timezone_name: str = "UTC"
    source_metadata: RadarSourceMetadata
    registered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def device_identifier(self) -> str | None:
        return self.vendor_device_name or self.vendor_device_id


class RadarVitalSnapshot(ProductDeviceSchema):
    radar_device_id: str = Field(..., min_length=1)
    measured_at: datetime
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    heart_rate_bpm: int | None = Field(default=None, ge=0, le=240)
    breath_rate_bpm: int | None = Field(default=None, ge=0, le=80)
    body_movement: float | None = Field(default=None, ge=0)
    bed_presence: RadarBedPresence = RadarBedPresence.UNKNOWN
    invalid_reading_flags: list[str] = Field(default_factory=list)
    source_metadata: RadarSourceMetadata


class RadarSleepStageSegment(ProductDeviceSchema):
    radar_device_id: str = Field(..., min_length=1)
    start_at: datetime
    end_at: datetime
    stage: RadarSleepStage
    confidence: float | None = Field(default=None, ge=0, le=1)
    source_metadata: RadarSourceMetadata

    @model_validator(mode="after")
    def validate_time_order(self) -> "RadarSleepStageSegment":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at.")
        return self


class RadarSleepReport(ProductDeviceSchema):
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
    stage_segments: list[RadarSleepStageSegment] = Field(default_factory=list)
    source_metadata: RadarSourceMetadata

    @model_validator(mode="after")
    def validate_sleep_time_order(self) -> "RadarSleepReport":
        if (
            self.sleep_start_at is not None
            and self.sleep_end_at is not None
            and self.sleep_end_at <= self.sleep_start_at
        ):
            raise ValueError("sleep_end_at must be after sleep_start_at.")
        return self


class RadarAlertEvent(ProductDeviceSchema):
    radar_alert_event_id: str = Field(..., min_length=1)
    radar_device_id: str = Field(..., min_length=1)
    alert_type: str = Field(..., min_length=1)
    severity: RadarAlertSeverity = RadarAlertSeverity.UNKNOWN
    occurred_at: datetime
    resolved_at: datetime | None = None
    title: str | None = None
    message: str | None = None
    source_metadata: RadarSourceMetadata

    @model_validator(mode="after")
    def validate_resolved_at(self) -> "RadarAlertEvent":
        if self.resolved_at is not None and self.resolved_at < self.occurred_at:
            raise ValueError("resolved_at cannot be before occurred_at.")
        return self


class RadarDataQuality(ProductDeviceSchema):
    freshness_seconds: float | None = Field(default=None, ge=0)
    max_snapshot_age_seconds: float = Field(default=300.0, gt=0)
    stale: bool = False
    device_offline: bool = False
    user_out_of_bed: bool = False
    current_snapshot_available: bool = True
    missing_intervals: int = Field(default=0, ge=0)
    missing_reading_count: int = Field(default=0, ge=0)
    invalid_reading_count: int = Field(default=0, ge=0)
    report_freshness_hours: float | None = Field(default=None, ge=0)
    max_report_age_hours: float = Field(default=36.0, gt=0)
    report_stale: bool = False
    partial_sleep_report: bool = False
    blocks_current_values: bool = False
    caveats: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)


class RadarDashboardSummary(ProductDeviceSchema):
    radar_device_id: str = Field(..., min_length=1)
    device: RadarDevice
    current_snapshot: RadarVitalSnapshot | None = None
    latest_sleep_report: RadarSleepReport | None = None
    recent_alerts: list[RadarAlertEvent] = Field(default_factory=list)
    data_quality: RadarDataQuality = Field(default_factory=RadarDataQuality)
    status_line: str = ""
    summary_text: str = ""
    highlights: list[str] = Field(default_factory=list)
    trend_observations: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    source_metadata: list[RadarSourceMetadata] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def build_raw_vendor_event(
    payload: dict[str, Any],
    *,
    vendor: str = DEFAULT_RADAR_VENDOR,
    received_at: datetime | None = None,
    raw_event_id: str | None = None,
) -> RawVendorEvent:
    """Build a raw vendor event while preserving the original payload."""
    event_type = _require_non_empty_string(payload.get("type"), "type")
    data_payload = parse_vendor_data_payload(payload.get("data"))
    event_timestamp = _first_timestamp(
        payload,
        data_payload,
        ("timestamp", "time", "event_time", "DateTime", "dateTime", "datetime"),
    )
    device_id = _optional_string(payload.get("device_id") or payload.get("deviceId"))
    device_name = _optional_string(
        payload.get("device_name")
        or payload.get("deviceName")
        or data_payload.get("device_name")
        or data_payload.get("deviceName")
    )
    message_id = _optional_string(payload.get("message_id") or payload.get("messageId"))
    resolved_event_id = raw_event_id or _build_raw_event_id(
        vendor=vendor,
        event_type=event_type,
        message_id=message_id,
        device_identifier=device_name or device_id,
        event_timestamp=event_timestamp,
    )
    return RawVendorEvent(
        raw_event_id=resolved_event_id,
        vendor=vendor,
        event_type=event_type,
        message_id=message_id,
        product_id=_optional_string(payload.get("product_id") or payload.get("productId")),
        device_id=device_id,
        device_name=device_name,
        home_id=_optional_string(payload.get("home_id") or payload.get("homeId")),
        received_at=received_at or datetime.now(timezone.utc),
        event_timestamp=event_timestamp,
        raw_payload=dict(payload),
        data_payload=data_payload,
    )


def build_vital_snapshot_from_raw_event(
    event: RawVendorEvent,
    *,
    radar_device_id: str | None = None,
) -> RadarVitalSnapshot:
    if event.event_type != VITAL_SIGNS_DATA_EVENT:
        raise ValueError(
            f"Raw vendor event type must be {VITAL_SIGNS_DATA_EVENT} for vital snapshots."
        )

    data = event.data_payload
    measured_at = event.event_timestamp or _first_timestamp(
        event.raw_payload,
        data,
        ("DateTime", "dateTime", "datetime", "timestamp", "time"),
    )
    if measured_at is None:
        raise ValueError("Vital snapshot payload is missing a usable timestamp.")

    heart_rate, heart_flags = normalize_vendor_vital_reading(
        _first_value(data, ("HeartRate", "heartRate", "heart_rate")),
        field_name="heart_rate",
    )
    breath_rate, breath_flags = normalize_vendor_vital_reading(
        _first_value(data, ("BreathRate", "breathRate", "breath_rate")),
        field_name="breath_rate",
    )
    body_movement = normalize_optional_non_negative_number(
        _first_value(data, ("BodyShake", "bodyShake", "body_movement")),
        field_name="body_movement",
    )

    return RadarVitalSnapshot(
        radar_device_id=radar_device_id
        or event.device_identifier
        or event.raw_event_id,
        measured_at=measured_at,
        received_at=event.received_at,
        heart_rate_bpm=heart_rate,
        breath_rate_bpm=breath_rate,
        body_movement=body_movement,
        bed_presence=normalize_bed_presence(
            _first_value(data, ("OnBed", "Onbed", "onBed", "onbed"))
        ),
        invalid_reading_flags=[*heart_flags, *breath_flags],
        source_metadata=event.to_source_metadata(),
    )


def parse_vendor_data_payload(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Vendor data field is not valid JSON.") from exc
        if not isinstance(decoded, dict):
            raise ValueError("Vendor data JSON must decode to an object.")
        return decoded
    raise ValueError("Vendor data field must be an object or a JSON object string.")


def normalize_vendor_vital_reading(
    value: Any,
    *,
    field_name: str,
) -> tuple[int | None, list[str]]:
    if value is None or value == "":
        return None, [f"{field_name}_missing"]
    number = _coerce_int(value, field_name)
    if number == INVALID_VENDOR_READING:
        return None, [f"{field_name}_invalid_minus_one"]
    if number < 0:
        return None, [f"{field_name}_invalid_negative"]
    return number, []


def normalize_optional_non_negative_number(
    value: Any,
    *,
    field_name: str,
) -> float | None:
    if value is None or value == "":
        return None
    number = float(value)
    if number < 0:
        raise ValueError(f"{field_name} cannot be negative.")
    return number


def normalize_bed_presence(value: Any) -> RadarBedPresence:
    if value is None or value == "":
        return RadarBedPresence.UNKNOWN
    if isinstance(value, bool):
        return RadarBedPresence.IN_BED if value else RadarBedPresence.OUT_OF_BED
    if isinstance(value, (int, float)):
        return RadarBedPresence.IN_BED if int(value) == 1 else RadarBedPresence.OUT_OF_BED
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "onbed", "in_bed", "in-bed"}:
        return RadarBedPresence.IN_BED
    if normalized in {"0", "false", "no", "n", "out_of_bed", "out-of-bed"}:
        return RadarBedPresence.OUT_OF_BED
    return RadarBedPresence.UNKNOWN


def normalize_vendor_timestamp(
    value: Any,
    *,
    default_timezone: timezone = timezone.utc,
) -> datetime:
    if value is None or value == "":
        raise ValueError("timestamp value is required.")
    if isinstance(value, datetime):
        return _ensure_timezone(value, default_timezone)
    if isinstance(value, (int, float)):
        return _datetime_from_epoch(float(value))
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("timestamp value is required.")
        if _looks_numeric(stripped):
            return _datetime_from_epoch(float(stripped))
        normalized = stripped.replace("Z", "+00:00")
        try:
            return _ensure_timezone(
                datetime.fromisoformat(normalized),
                default_timezone,
            )
        except ValueError as exc:
            raise ValueError(f"Unsupported timestamp format: {value!r}") from exc
    raise ValueError(f"Unsupported timestamp type: {type(value).__name__}.")


def _first_timestamp(
    top_level: dict[str, Any],
    data_payload: dict[str, Any],
    keys: tuple[str, ...],
) -> datetime | None:
    for key in keys:
        if key in top_level:
            return normalize_vendor_timestamp(top_level[key])
        if key in data_payload:
            return normalize_vendor_timestamp(data_payload[key])
    return None


def _datetime_from_epoch(value: float) -> datetime:
    seconds = value / 1000.0 if abs(value) >= 10_000_000_000 else value
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _ensure_timezone(value: datetime, default_timezone: timezone) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=default_timezone)
    return value.astimezone(timezone.utc)


def _first_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_non_empty_string(value: Any, field_name: str) -> str:
    text = _optional_string(value)
    if text is None:
        raise ValueError(f"{field_name} is required.")
    return text


def _coerce_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc


def _looks_numeric(value: str) -> bool:
    return value.replace(".", "", 1).replace("-", "", 1).isdigit()


def _build_raw_event_id(
    *,
    vendor: str,
    event_type: str,
    message_id: str | None,
    device_identifier: str | None,
    event_timestamp: datetime | None,
) -> str:
    if message_id:
        return f"{vendor}:{event_type}:{message_id}"
    timestamp = (
        event_timestamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if event_timestamp is not None
        else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    return f"{vendor}:{event_type}:{device_identifier or 'unknown-device'}:{timestamp}"
