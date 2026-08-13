# 本模块定义与 provider 和运行时解耦的睡眠领域严格契约。
"""Provider-neutral, runtime-free sleep-domain contracts.

This module deliberately contains data contracts only.  It does not resolve
device bindings, aggregate nights, persist records, dispatch adapters, or call
the product-Agent runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Annotated, Literal, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator


NonEmptyStr = Annotated[str, Field(min_length=1)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
EventAttribute: TypeAlias = str | int | float | bool | None


class SleepDomainContract(BaseModel):
    """Immutable, fail-closed base for every provider-neutral contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def require_aware_datetimes(self) -> "SleepDomainContract":
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, datetime) and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError(f"{name} must be timezone-aware")
        return self


class DataMode(str, Enum):
    LIVE = "live"
    REPLAY = "replay"


class NamespaceMismatchError(RuntimeError):
    """A durable namespace was used with a different data authority."""


@dataclass(frozen=True)
class DomainNamespace:
    namespace_id: str
    data_mode: DataMode

    def __post_init__(self) -> None:
        expected_prefix = f"{self.data_mode.value}:"
        if not self.namespace_id.startswith(expected_prefix):
            raise NamespaceMismatchError(
                f"{self.data_mode.value} namespace must start with "
                f"{expected_prefix!r}"
            )
        if self.namespace_id == expected_prefix:
            raise NamespaceMismatchError("namespace requires an opaque suffix")


@dataclass(frozen=True)
class CurrentRevisionPointer:
    night_episode_id: str
    current_revision_id: str | None
    current_revision_number: int | None
    cas_version: int


@dataclass(frozen=True)
class SubjectLifecycleLease:
    subject_id: str
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime


class SourceKind(str, Enum):
    DEVICE_MEASURED = "device_measured"
    VENDOR_DERIVED = "vendor_derived"
    USER_REPORTED = "user_reported"
    EXTERNALLY_REPORTED = "externally_reported"
    FUTURE_MODEL_DERIVED = "future_model_derived"


class AvailabilityState(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    NOT_PROVIDED = "not_provided"


class MissingState(str, Enum):
    PRESENT = "present"
    MISSING = "missing"
    INVALID = "invalid"
    UNKNOWN = "unknown"
    NOT_PROVIDED = "not_provided"
    NOT_APPLICABLE = "not_applicable"


class TimezoneStatus(str, Enum):
    KNOWN = "known"
    NORMALIZED_FROM_BINDING = "normalized_from_binding"
    TIMEZONE_UNKNOWN = "timezone_unknown"
    NOT_PROVIDED = "not_provided"


class ConfidenceValue(SleepDomainContract):
    schema_version: Literal["confidence_value.v1"] = "confidence_value.v1"
    state: AvailabilityState
    value: float | None = Field(default=None, ge=0, le=1)
    reason: str | None = None

    @model_validator(mode="after")
    def value_matches_state(self) -> "ConfidenceValue":
        if self.state == AvailabilityState.KNOWN and self.value is None:
            raise ValueError("known confidence requires value")
        if self.state != AvailabilityState.KNOWN and self.value is not None:
            raise ValueError("unknown/not_provided confidence cannot carry value")
        return self


class AlgorithmVersionValue(SleepDomainContract):
    schema_version: Literal["algorithm_version_value.v1"] = (
        "algorithm_version_value.v1"
    )
    state: AvailabilityState
    value: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def value_matches_state(self) -> "AlgorithmVersionValue":
        if self.state == AvailabilityState.KNOWN and not self.value:
            raise ValueError("known algorithm_version requires value")
        if self.state != AvailabilityState.KNOWN and self.value is not None:
            raise ValueError(
                "unknown/not_provided algorithm_version cannot carry value"
            )
        return self


class CalibrationValue(SleepDomainContract):
    schema_version: Literal["calibration_value.v1"] = "calibration_value.v1"
    state: AvailabilityState
    calibration_id: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def value_matches_state(self) -> "CalibrationValue":
        if self.state == AvailabilityState.KNOWN and not self.calibration_id:
            raise ValueError("known calibration requires calibration_id")
        if self.state != AvailabilityState.KNOWN and self.calibration_id is not None:
            raise ValueError(
                "unknown/not_provided calibration cannot carry calibration_id"
            )
        return self


class ObservationQuality(SleepDomainContract):
    schema_version: Literal["observation_quality.v1"] = "observation_quality.v1"
    missing_state: MissingState
    confidence: ConfidenceValue
    algorithm_version: AlgorithmVersionValue
    calibration: CalibrationValue
    completeness: float | None = Field(default=None, ge=0, le=1)
    quality_flags: tuple[NonEmptyStr, ...] = ()
    processing_steps: tuple[NonEmptyStr, ...] = ()
    limitations: tuple[NonEmptyStr, ...] = ()


class ObservationProvenance(SleepDomainContract):
    """Reference-only canonical provenance; embedded vendor payloads are forbidden."""

    schema_version: Literal["observation_provenance.v1"] = (
        "observation_provenance.v1"
    )
    provider_id: NonEmptyStr
    provider_account_id: NonEmptyStr
    adapter_id: NonEmptyStr
    adapter_version: NonEmptyStr
    raw_ingress_record_id: NonEmptyStr
    raw_payload_sha256: Sha256Hex
    source_record_id: str | None = None
    source_idempotency_key: str | None = None
    acquisition_receipt_ids: tuple[NonEmptyStr, ...] = ()
    producer_name: str | None = None


class ProviderDeviceIdentity(SleepDomainContract):
    schema_version: Literal["provider_device_identity.v1"] = (
        "provider_device_identity.v1"
    )
    provider_device_id: str | None = None
    provider_device_name: str | None = None
    product_id: str | None = None
    home_id: str | None = None
    project_id: str | None = None
    native_keys: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_stable_identity(self) -> "ProviderDeviceIdentity":
        if not (
            self.provider_device_id
            or self.provider_device_name
            or self.native_keys
        ):
            raise ValueError(
                "provider device identity requires an id, name, or native key"
            )
        return self


class ObservationType(str, Enum):
    HEART_RATE = "heart_rate"
    RESPIRATORY_RATE = "respiratory_rate"
    BED_PRESENCE = "bed_presence"
    MOVEMENT = "movement"
    DEVICE_CONNECTIVITY = "device_connectivity"
    VENDOR_ALERT = "vendor_alert"
    SLEEP_STAGE_INTERVAL = "sleep_stage_interval"
    VENDOR_SLEEP_PROFILE_METRIC = "vendor_sleep_profile_metric"
    BED_EXIT = "bed_exit"
    MISSING_INTERVAL = "missing_interval"
    UNKNOWN = "unknown"


class BedPresenceState(str, Enum):
    IN_BED = "in_bed"
    OUT_OF_BED = "out_of_bed"
    UNKNOWN = "unknown"


class DeviceConnectivityState(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class SleepStageState(str, Enum):
    DEEP = "deep"
    LIGHT = "light"
    REM = "rem"
    AWAKE = "awake"
    UNKNOWN = "unknown"


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class AlertLifecycleState(str, Enum):
    ACTIVE = "active"
    STOPPED = "stopped"
    ORPHAN_STOP = "orphan_stop"
    UNKNOWN = "unknown"


class BedExitKind(str, Enum):
    BED_EXIT = "bed_exit"
    GET_UP = "get_up"
    RETURN_TO_BED = "return_to_bed"
    UNKNOWN = "unknown"


class HeartRatePayload(SleepDomainContract):
    schema_version: Literal["heart_rate_payload.v1"] = "heart_rate_payload.v1"
    observation_type: Literal[ObservationType.HEART_RATE] = ObservationType.HEART_RATE
    value: float = Field(..., gt=0, le=300)
    unit: Literal["beats_per_minute"] = "beats_per_minute"


class RespiratoryRatePayload(SleepDomainContract):
    schema_version: Literal["respiratory_rate_payload.v1"] = (
        "respiratory_rate_payload.v1"
    )
    observation_type: Literal[ObservationType.RESPIRATORY_RATE] = (
        ObservationType.RESPIRATORY_RATE
    )
    value: float = Field(..., gt=0, le=150)
    unit: Literal["breaths_per_minute"] = "breaths_per_minute"


class BedPresencePayload(SleepDomainContract):
    schema_version: Literal["bed_presence_payload.v1"] = "bed_presence_payload.v1"
    observation_type: Literal[ObservationType.BED_PRESENCE] = (
        ObservationType.BED_PRESENCE
    )
    state: BedPresenceState


class MovementPayload(SleepDomainContract):
    schema_version: Literal["movement_payload.v1"] = "movement_payload.v1"
    observation_type: Literal[ObservationType.MOVEMENT] = ObservationType.MOVEMENT
    value: float = Field(..., ge=0)
    unit: Literal["count", "index", "event"] = "index"


class DeviceConnectivityPayload(SleepDomainContract):
    schema_version: Literal["device_connectivity_payload.v1"] = (
        "device_connectivity_payload.v1"
    )
    observation_type: Literal[ObservationType.DEVICE_CONNECTIVITY] = (
        ObservationType.DEVICE_CONNECTIVITY
    )
    state: DeviceConnectivityState
    vendor_status_code: str | None = None


class VendorAlertPayload(SleepDomainContract):
    schema_version: Literal["vendor_alert_payload.v1"] = "vendor_alert_payload.v1"
    observation_type: Literal[ObservationType.VENDOR_ALERT] = (
        ObservationType.VENDOR_ALERT
    )
    alert_code: NonEmptyStr
    severity: AlertSeverity = AlertSeverity.UNKNOWN
    lifecycle_state: AlertLifecycleState = AlertLifecycleState.UNKNOWN
    vendor_alert_instance_id: str | None = None
    title: str | None = None
    message: str | None = None


class SleepStageIntervalPayload(SleepDomainContract):
    schema_version: Literal["sleep_stage_interval_payload.v1"] = (
        "sleep_stage_interval_payload.v1"
    )
    observation_type: Literal[ObservationType.SLEEP_STAGE_INTERVAL] = (
        ObservationType.SLEEP_STAGE_INTERVAL
    )
    stage: SleepStageState
    start_at: datetime | None = None
    end_at: datetime | None = None
    source_start_text: str | None = None
    source_end_text: str | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "SleepStageIntervalPayload":
        if (self.start_at is None) != (self.end_at is None):
            raise ValueError("stage interval requires both start_at and end_at")
        if (
            self.start_at is not None
            and self.end_at is not None
            and self.end_at <= self.start_at
        ):
            raise ValueError("end_at must be after start_at")
        if self.start_at is None and not (
            self.source_start_text and self.source_end_text
        ):
            raise ValueError(
                "stage interval without aware times requires source timestamp text"
            )
        return self


class VendorSleepProfileMetricPayload(SleepDomainContract):
    schema_version: Literal["vendor_sleep_profile_metric_payload.v1"] = (
        "vendor_sleep_profile_metric_payload.v1"
    )
    observation_type: Literal[ObservationType.VENDOR_SLEEP_PROFILE_METRIC] = (
        ObservationType.VENDOR_SLEEP_PROFILE_METRIC
    )
    metric_name: NonEmptyStr
    value_state: AvailabilityState
    value: str | int | float | bool | None = None
    source_text: str | None = None
    unit: str | None = None

    @model_validator(mode="after")
    def value_matches_state(self) -> "VendorSleepProfileMetricPayload":
        if self.value_state == AvailabilityState.KNOWN and self.value is None:
            raise ValueError("known vendor metric requires value")
        if self.value_state != AvailabilityState.KNOWN and self.value is not None:
            raise ValueError("unknown/not_provided vendor metric cannot carry value")
        return self


class BedExitPayload(SleepDomainContract):
    schema_version: Literal["bed_exit_payload.v1"] = "bed_exit_payload.v1"
    observation_type: Literal[ObservationType.BED_EXIT] = ObservationType.BED_EXIT
    kind: BedExitKind


class MissingIntervalPayload(SleepDomainContract):
    schema_version: Literal["missing_interval_payload.v1"] = (
        "missing_interval_payload.v1"
    )
    observation_type: Literal[ObservationType.MISSING_INTERVAL] = (
        ObservationType.MISSING_INTERVAL
    )
    target_observation_type: ObservationType
    missing_state: MissingState
    reason_code: NonEmptyStr
    interval_start_at: datetime | None = None
    interval_end_at: datetime | None = None

    @model_validator(mode="after")
    def validate_missing_interval(self) -> "MissingIntervalPayload":
        if self.target_observation_type == ObservationType.MISSING_INTERVAL:
            raise ValueError("missing interval cannot target itself")
        if self.missing_state not in {
            MissingState.MISSING,
            MissingState.INVALID,
            MissingState.UNKNOWN,
            MissingState.NOT_PROVIDED,
        }:
            raise ValueError("missing interval requires a non-present missing_state")
        if (
            self.interval_start_at is not None
            and self.interval_end_at is not None
            and self.interval_end_at <= self.interval_start_at
        ):
            raise ValueError("interval_end_at must be after interval_start_at")
        return self


class UnknownObservationPayload(SleepDomainContract):
    schema_version: Literal["unknown_observation_payload.v1"] = (
        "unknown_observation_payload.v1"
    )
    observation_type: Literal[ObservationType.UNKNOWN] = ObservationType.UNKNOWN
    source_type: NonEmptyStr
    reason_code: NonEmptyStr
    normalized_scalar: EventAttribute = None


ObservationPayload: TypeAlias = Annotated[
    HeartRatePayload
    | RespiratoryRatePayload
    | BedPresencePayload
    | MovementPayload
    | DeviceConnectivityPayload
    | VendorAlertPayload
    | SleepStageIntervalPayload
    | VendorSleepProfileMetricPayload
    | BedExitPayload
    | MissingIntervalPayload
    | UnknownObservationPayload,
    Field(discriminator="observation_type"),
]


class AdapterObservationCandidate(SleepDomainContract):
    """Device-scoped Adapter output.  Elder/binding/night identity is forbidden."""

    schema_version: Literal["adapter_observation_candidate.v1"] = (
        "adapter_observation_candidate.v1"
    )
    candidate_id: NonEmptyStr
    data_mode: DataMode
    observation_type: ObservationType
    payload: ObservationPayload
    source_kind: SourceKind
    provider_id: NonEmptyStr
    provider_account_id: NonEmptyStr
    provider_device: ProviderDeviceIdentity
    request_signed_at: datetime | None = None
    measurement_at: datetime | None = None
    event_occurred_at: datetime | None = None
    received_at: datetime
    source_timestamp_text: str | None = None
    timezone_status: TimezoneStatus
    quality: ObservationQuality
    provenance: ObservationProvenance
    source_key: NonEmptyStr
    idempotency_key: NonEmptyStr

    @model_validator(mode="after")
    def payload_type_matches_envelope(self) -> "AdapterObservationCandidate":
        if self.observation_type.value != self.payload.observation_type.value:
            raise ValueError("payload observation_type must match envelope")
        if isinstance(self.payload, MissingIntervalPayload):
            if self.quality.missing_state != self.payload.missing_state:
                raise ValueError(
                    "missing payload and quality must use the same missing_state"
                )
        elif self.quality.missing_state in {
            MissingState.MISSING,
            MissingState.INVALID,
        }:
            raise ValueError(
                "missing/invalid quality requires a MissingIntervalPayload"
            )
        if isinstance(
            self.payload,
            (
                VendorAlertPayload,
                SleepStageIntervalPayload,
                VendorSleepProfileMetricPayload,
            ),
        ) and self.source_kind != SourceKind.VENDOR_DERIVED:
            raise ValueError(
                "vendor-derived payload types require vendor_derived source_kind"
            )
        if self.provenance.provider_id != self.provider_id:
            raise ValueError("provenance provider_id must match envelope")
        if self.provenance.provider_account_id != self.provider_account_id:
            raise ValueError("provenance provider_account_id must match envelope")
        return self


class DeviceBindingStatus(str, Enum):
    ACTIVE = "active"
    ENDED = "ended"
    REVOKED = "revoked"


class DeviceBindingAuditAction(str, Enum):
    CREATED = "created"
    REBOUND = "rebound"


class DeviceBinding(SleepDomainContract):
    schema_version: Literal["device_binding.v1"] = "device_binding.v1"
    data_mode: DataMode
    device_binding_id: NonEmptyStr
    binding_version: int = Field(..., ge=1)
    device_id: NonEmptyStr
    provider_id: NonEmptyStr
    provider_account_id: NonEmptyStr
    provider_device: ProviderDeviceIdentity
    subject_id: NonEmptyStr
    timezone_name: NonEmptyStr
    effective_from: datetime
    effective_until: datetime | None = None
    status: DeviceBindingStatus
    changed_by_actor_id: NonEmptyStr
    change_reason: NonEmptyStr
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_binding(self) -> "DeviceBinding":
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone_name must be a valid IANA timezone") from exc
        if (
            self.effective_until is not None
            and self.effective_until <= self.effective_from
        ):
            raise ValueError("effective_until must be after effective_from")
        return self


class DeviceBindingAuditEvent(SleepDomainContract):
    schema_version: Literal["device_binding_audit_event.v1"] = (
        "device_binding_audit_event.v1"
    )
    audit_event_id: NonEmptyStr
    command_id: NonEmptyStr
    data_mode: DataMode
    action: DeviceBindingAuditAction
    provider_id: NonEmptyStr
    provider_account_id: NonEmptyStr
    provider_device_key: NonEmptyStr
    device_id: NonEmptyStr
    previous_device_binding_id: str | None = None
    previous_binding_version: int | None = Field(default=None, ge=1)
    new_device_binding_id: NonEmptyStr
    new_binding_version: int = Field(..., ge=1)
    effective_at: datetime
    actor_id: NonEmptyStr
    authorization_id: NonEmptyStr
    reason: NonEmptyStr
    occurred_at: datetime

    @model_validator(mode="after")
    def action_matches_previous_binding(self) -> "DeviceBindingAuditEvent":
        previous = (
            self.previous_device_binding_id,
            self.previous_binding_version,
        )
        if self.action == DeviceBindingAuditAction.CREATED and previous != (
            None,
            None,
        ):
            raise ValueError("created binding audit cannot reference a previous binding")
        if self.action == DeviceBindingAuditAction.REBOUND and (
            self.previous_device_binding_id is None
            or self.previous_binding_version is None
        ):
            raise ValueError("rebound binding audit requires the previous binding")
        return self


class SleepObservation(SleepDomainContract):
    schema_version: Literal["sleep_observation.v1"] = "sleep_observation.v1"
    observation_id: NonEmptyStr
    data_mode: DataMode
    observation_type: ObservationType
    payload: ObservationPayload
    subject_id: NonEmptyStr
    device_id: NonEmptyStr
    device_binding_id: NonEmptyStr
    binding_version: int = Field(..., ge=1)
    request_signed_at: datetime | None = None
    measurement_at: datetime | None = None
    event_occurred_at: datetime | None = None
    received_at: datetime
    source_timestamp_text: str | None = None
    timezone_status: TimezoneStatus
    source_kind: SourceKind
    quality: ObservationQuality
    provenance: ObservationProvenance
    source_key: NonEmptyStr
    idempotency_key: NonEmptyStr

    @model_validator(mode="after")
    def validate_observation(self) -> "SleepObservation":
        if self.observation_type.value != self.payload.observation_type.value:
            raise ValueError("payload observation_type must match envelope")
        if isinstance(self.payload, MissingIntervalPayload):
            if self.quality.missing_state != self.payload.missing_state:
                raise ValueError(
                    "missing payload and quality must use the same missing_state"
                )
        elif self.quality.missing_state in {
            MissingState.MISSING,
            MissingState.INVALID,
        }:
            raise ValueError(
                "missing/invalid quality requires a MissingIntervalPayload"
            )
        if isinstance(
            self.payload,
            (
                VendorAlertPayload,
                SleepStageIntervalPayload,
                VendorSleepProfileMetricPayload,
            ),
        ) and self.source_kind != SourceKind.VENDOR_DERIVED:
            raise ValueError(
                "vendor-derived payload types require vendor_derived source_kind"
            )
        return self


def bind_adapter_candidate(
    candidate: AdapterObservationCandidate,
    binding: DeviceBinding,
    *,
    observation_id: str | None = None,
) -> SleepObservation:
    """把已解析的设备绑定显式应用到候选观测，不做隐式历史查询。"""

    if binding.status == DeviceBindingStatus.REVOKED:
        raise ValueError("revoked binding cannot create observations")
    if candidate.data_mode != binding.data_mode:
        raise ValueError("candidate and binding data_mode must match")
    if candidate.provider_id != binding.provider_id:
        raise ValueError("candidate and binding provider_id must match")
    if candidate.provider_account_id != binding.provider_account_id:
        raise ValueError("candidate and binding provider_account_id must match")
    candidate_keys = {
        ("provider_device_id", candidate.provider_device.provider_device_id),
        ("provider_device_name", candidate.provider_device.provider_device_name),
        *{
            (f"native:{key}", value)
            for key, value in candidate.provider_device.native_keys.items()
        },
    }
    binding_keys = {
        ("provider_device_id", binding.provider_device.provider_device_id),
        ("provider_device_name", binding.provider_device.provider_device_name),
        *{
            (f"native:{key}", value)
            for key, value in binding.provider_device.native_keys.items()
        },
    }
    if not (
        {(key, value) for key, value in candidate_keys if value}
        & {(key, value) for key, value in binding_keys if value}
    ):
        raise ValueError("candidate does not match the supplied provider device")
    attribution_time = candidate.measurement_at or candidate.event_occurred_at
    if attribution_time is None:
        raise ValueError(
            "candidate needs measurement_at or event_occurred_at for binding"
        )
    if attribution_time < binding.effective_from:
        raise ValueError("candidate time precedes binding effective_from")
    if binding.effective_until is not None and attribution_time >= binding.effective_until:
        raise ValueError("candidate time is outside binding effective interval")
    return SleepObservation(
        observation_id=observation_id
        or f"observation:{candidate.candidate_id}:binding-v{binding.binding_version}",
        data_mode=candidate.data_mode,
        observation_type=candidate.observation_type,
        payload=candidate.payload,
        subject_id=binding.subject_id,
        device_id=binding.device_id,
        device_binding_id=binding.device_binding_id,
        binding_version=binding.binding_version,
        request_signed_at=candidate.request_signed_at,
        measurement_at=candidate.measurement_at,
        event_occurred_at=candidate.event_occurred_at,
        received_at=candidate.received_at,
        source_timestamp_text=candidate.source_timestamp_text,
        timezone_status=candidate.timezone_status,
        source_kind=candidate.source_kind,
        quality=candidate.quality,
        provenance=candidate.provenance,
        source_key=candidate.source_key,
        idempotency_key=candidate.idempotency_key,
    )


class AdapterCapability(str, Enum):
    PUSH = "push"
    PULL = "pull"
    RECONCILE = "reconcile"
    REALTIME_VITALS = "realtime_vitals"
    VITAL_PUSH = "vital_push"
    BED_PRESENCE = "bed_presence"
    MOVEMENT = "movement"
    ALERTS = "alerts"
    SLEEP_REPORT = "sleep_report"
    SLEEP_STAGES = "sleep_stages"
    HISTORICAL_RATE_SAMPLES = "historical_rate_samples"
    RAW_SIGNAL_ARTIFACTS = "raw_signal_artifacts"
    RAW_RESP_WAVEFORM = "raw_resp_waveform"


class CapabilitySupport(str, Enum):
    YES = "yes"
    NO = "no"


class CapabilityVerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"


class AdapterDeploymentStatus(str, Enum):
    REGISTERED = "registered"
    ENABLED = "enabled"
    DISABLED = "disabled"
    SUPERSEDED = "superseded"


class CapabilityDeclaration(SleepDomainContract):
    schema_version: Literal["capability_declaration.v1"] = (
        "capability_declaration.v1"
    )
    capability: AdapterCapability
    support: CapabilitySupport
    environment: NonEmptyStr
    verification_status: CapabilityVerificationStatus


class AdapterDescriptor(SleepDomainContract):
    schema_version: Literal["adapter_descriptor.v1"] = "adapter_descriptor.v1"
    adapter_id: NonEmptyStr
    provider_id: NonEmptyStr
    adapter_version: NonEmptyStr
    contract_version: NonEmptyStr
    supported_data_modes: frozenset[DataMode]
    supported_device_types: tuple[NonEmptyStr, ...]
    supported_provider_account_ids: tuple[NonEmptyStr, ...]
    capabilities: tuple[CapabilityDeclaration, ...]
    required_credential_names: tuple[NonEmptyStr, ...] = ()
    required_configuration_names: tuple[NonEmptyStr, ...] = ()
    adapter_artifact_sha256: Sha256Hex
    configuration_fingerprint: Sha256Hex
    accepted_input_variants: tuple[NonEmptyStr, ...] = ()
    known_limitations: tuple[NonEmptyStr, ...] = ()
    output_observation_schema_versions: tuple[NonEmptyStr, ...]
    deployment_status: AdapterDeploymentStatus
    activated_at: datetime | None = None
    superseded_by_version: str | None = None
    rollback_target_version: str | None = None

    @model_validator(mode="after")
    def descriptor_is_coherent(self) -> "AdapterDescriptor":
        if not self.supported_data_modes:
            raise ValueError("supported_data_modes cannot be empty")
        if not self.supported_device_types:
            raise ValueError("supported_device_types cannot be empty")
        if not self.output_observation_schema_versions:
            raise ValueError("output_observation_schema_versions cannot be empty")
        keys = [
            (item.capability, item.environment) for item in self.capabilities
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("capability/environment declarations must be unique")
        return self


class VerificationReviewerKind(str, Enum):
    HUMAN = "human"
    AUTOMATED = "automated"


class CapabilityVerificationReceipt(SleepDomainContract):
    schema_version: Literal["capability_verification_receipt.v1"] = (
        "capability_verification_receipt.v1"
    )
    receipt_id: NonEmptyStr
    data_mode: DataMode
    adapter_id: NonEmptyStr
    adapter_version: NonEmptyStr
    adapter_artifact_sha256: Sha256Hex
    configuration_fingerprint: Sha256Hex
    capability: AdapterCapability
    environment: NonEmptyStr
    status: CapabilityVerificationStatus
    evidence_references: tuple[NonEmptyStr, ...]
    evidence_sha256: tuple[Sha256Hex, ...]
    test_result_references: tuple[NonEmptyStr, ...]
    test_result_sha256: tuple[Sha256Hex, ...]
    conformance_result: NonEmptyStr
    reviewer_kind: VerificationReviewerKind
    reviewed_by_actor_id: str | None = None
    reviewed_at: datetime

    @model_validator(mode="after")
    def verified_requires_human_approval(self) -> "CapabilityVerificationReceipt":
        if len(self.evidence_references) != len(self.evidence_sha256):
            raise ValueError(
                "each evidence reference requires one corresponding SHA-256"
            )
        if len(self.test_result_references) != len(self.test_result_sha256):
            raise ValueError(
                "each test result reference requires one corresponding SHA-256"
            )
        if self.status == CapabilityVerificationStatus.VERIFIED:
            if self.reviewer_kind != VerificationReviewerKind.HUMAN:
                raise ValueError("VERIFIED capability requires a human reviewer")
            if not self.reviewed_by_actor_id:
                raise ValueError("VERIFIED capability requires reviewed_by_actor_id")
            if not self.evidence_references or not self.evidence_sha256:
                raise ValueError("VERIFIED capability requires immutable evidence")
            if not self.test_result_references or not self.test_result_sha256:
                raise ValueError("VERIFIED capability requires immutable test results")
        return self


class AdapterDeploymentEvent(SleepDomainContract):
    """Append-only lifecycle decision for one immutable Adapter version."""

    schema_version: Literal["adapter_deployment_event.v1"] = (
        "adapter_deployment_event.v1"
    )
    deployment_event_id: NonEmptyStr
    data_mode: DataMode
    adapter_id: NonEmptyStr
    adapter_version: NonEmptyStr
    previous_status: AdapterDeploymentStatus
    deployment_status: AdapterDeploymentStatus
    changed_by_actor_id: NonEmptyStr
    change_reason: NonEmptyStr
    changed_at: datetime
    superseded_by_version: str | None = None
    rollback_target_version: str | None = None


class ResolvedAdapterCapability(SleepDomainContract):
    schema_version: Literal["resolved_adapter_capability.v1"] = (
        "resolved_adapter_capability.v1"
    )
    capability: AdapterCapability
    verification_status: CapabilityVerificationStatus


class AdapterResolutionLock(SleepDomainContract):
    """Exact Adapter/config selection; deliberately has no subject or Episode id."""

    schema_version: Literal["adapter_resolution_lock.v1"] = (
        "adapter_resolution_lock.v1"
    )
    adapter_resolution_lock_id: NonEmptyStr
    data_mode: DataMode
    provider_id: NonEmptyStr
    provider_account_id: NonEmptyStr
    adapter_id: NonEmptyStr
    adapter_version: NonEmptyStr
    contract_version: NonEmptyStr
    adapter_artifact_sha256: Sha256Hex
    configuration_fingerprint: Sha256Hex
    environment: NonEmptyStr
    capabilities: tuple[ResolvedAdapterCapability, ...]
    descriptor_sha256: Sha256Hex
    resolution_request_id: NonEmptyStr
    resolved_at: datetime

    @model_validator(mode="after")
    def validate_resolution_lock(self) -> "AdapterResolutionLock":
        if not self.capabilities:
            raise ValueError("resolution lock requires at least one capability")
        names = [item.capability for item in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError("resolution lock capabilities must be unique")
        return self


class SignatureVerificationState(str, Enum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    NOT_PROVIDED = "not_provided"


class RawIngressRecord(SleepDomainContract):
    """Metadata for an immutable encrypted raw record; never the payload itself."""

    schema_version: Literal["raw_ingress_record.v1"] = "raw_ingress_record.v1"
    raw_ingress_record_id: NonEmptyStr
    data_mode: DataMode
    provider_id: NonEmptyStr
    provider_account_id: NonEmptyStr
    event_type: NonEmptyStr
    message_id: str | None = None
    request_signed_at: datetime | None = None
    measurement_at: datetime | None = None
    event_occurred_at: datetime | None = None
    received_at: datetime
    signature_profile: str | None = None
    signature_verification: SignatureVerificationState
    idempotency_identity: NonEmptyStr
    idempotency_version: NonEmptyStr
    pre_normalization_payload_sha256: Sha256Hex
    encrypted_payload_reference: NonEmptyStr
    content_type: NonEmptyStr
    payload_size_bytes: int = Field(..., ge=0)
    retention_deadline: datetime

    @model_validator(mode="after")
    def validate_retention(self) -> "RawIngressRecord":
        if self.retention_deadline <= self.received_at:
            raise ValueError("retention_deadline must be after received_at")
        return self


# Descriptive compatibility name; RawIngressRecord deliberately contains
# metadata and an encrypted reference, never the payload bytes.
RawIngressRecordMetadata = RawIngressRecord


class QuarantineReason(str, Enum):
    INVALID_SIGNATURE = "invalid_signature"
    UNSUPPORTED_SIGNATURE_PROFILE = "unsupported_signature_profile"
    STALE_OR_REPLAYED_REQUEST = "stale_or_replayed_request"
    UNKNOWN_FORMAT = "unknown_format"
    MALFORMED_PAYLOAD = "malformed_payload"
    UNSUPPORTED_UNIT = "unsupported_unit"
    UNPARSEABLE_TIME = "unparseable_time"
    TIMEZONE_UNKNOWN = "timezone_unknown"
    CLOCK_SKEW = "clock_skew"
    MESSAGE_ID_COLLISION = "message_id_collision"
    DEVICE_UNBOUND = "device_unbound"
    DEVICE_BINDING_AMBIGUOUS = "device_binding_ambiguous"
    BINDING_TIME_OUTSIDE_INTERVAL = "binding_time_outside_interval"
    OVERSIZED_PAYLOAD = "oversized_payload"
    ADAPTER_TIMEOUT = "adapter_timeout"
    ADAPTER_EXCEPTION = "adapter_exception"
    UNKNOWN = "unknown"


class ProcessingStage(str, Enum):
    INTAKE = "intake"
    AUTHENTICATION = "authentication"
    NORMALIZATION = "normalization"
    BINDING = "binding"
    REPAIR = "repair"
    DELETION = "deletion"


class ProcessingOutcome(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    RELEASED = "released"
    CRYPTO_SHREDDED = "crypto_shredded"


class ProcessingReceipt(SleepDomainContract):
    schema_version: Literal["processing_receipt.v1"] = "processing_receipt.v1"
    receipt_id: NonEmptyStr
    raw_ingress_record_id: NonEmptyStr
    data_mode: DataMode
    stage: ProcessingStage
    outcome: ProcessingOutcome
    occurred_at: datetime
    actor_id: NonEmptyStr
    processor_id: NonEmptyStr
    processor_version: NonEmptyStr
    cause_receipt_id: str | None = None
    quarantine_reason: QuarantineReason | None = None
    detail_code: str | None = None

    @model_validator(mode="after")
    def quarantine_has_reason(self) -> "ProcessingReceipt":
        if (
            self.outcome == ProcessingOutcome.QUARANTINED
            and self.quarantine_reason is None
        ):
            raise ValueError("quarantined processing receipt requires a reason")
        if (
            self.outcome != ProcessingOutcome.QUARANTINED
            and self.quarantine_reason is not None
        ):
            raise ValueError("quarantine_reason is only valid for quarantined outcome")
        return self


class QuarantineReprocessAudit(SleepDomainContract):
    schema_version: Literal["quarantine_reprocess_audit.v1"] = (
        "quarantine_reprocess_audit.v1"
    )
    request_id: NonEmptyStr
    data_mode: DataMode
    quarantine_ids: tuple[NonEmptyStr, ...]
    actor_id: NonEmptyStr
    authorization_id: NonEmptyStr
    requested_at: datetime
    reason: NonEmptyStr

    @model_validator(mode="after")
    def require_unique_targets(self) -> "QuarantineReprocessAudit":
        if not self.quarantine_ids:
            raise ValueError("quarantine reprocessing requires at least one target")
        if len(set(self.quarantine_ids)) != len(self.quarantine_ids):
            raise ValueError("quarantine reprocessing targets must be unique")
        return self


class MonitoringState(str, Enum):
    DORMANT = "dormant"
    ACTIVE = "active"


class LifecycleComponent(str, Enum):
    MONITORING = "monitoring"
    NIGHT_EPISODE = "night_episode"


class LifecycleTriggerSource(str, Enum):
    VERIFIED_DEVICE_EVENT = "verified_device_event"
    AUTHORIZED_COMMAND = "authorized_command"
    TIME_FALLBACK = "time_fallback"
    REPORT_DEADLINE = "report_deadline"
    SOURCE_REPORT = "source_report"
    EXPLICIT_REANALYSIS = "explicit_reanalysis"
    RESTART_RECOVERY = "restart_recovery"


class LifecycleTriggerKind(str, Enum):
    IN_BED = "in_bed"
    OUT_OF_BED = "out_of_bed"
    ACTIVATE = "activate"
    DEACTIVATE = "deactivate"
    FALLBACK_START = "fallback_start"
    FALLBACK_END = "fallback_end"
    REPORT_DEADLINE_REACHED = "report_deadline_reached"
    SOURCE_REPORT_AVAILABLE = "source_report_available"
    REANALYZE = "reanalyze"
    FEEDBACK = "feedback"
    RECOVER = "recover"


class LifecycleTrigger(SleepDomainContract):
    schema_version: Literal["lifecycle_trigger.v1"] = "lifecycle_trigger.v1"
    trigger_id: NonEmptyStr
    data_mode: DataMode
    subject_id: NonEmptyStr
    source: LifecycleTriggerSource
    kind: LifecycleTriggerKind
    occurred_at: datetime
    received_at: datetime
    observation_id: str | None = None
    source_report_version_id: str | None = None
    actor_id: str | None = None
    authorization_id: str | None = None
    correlation_id: NonEmptyStr

    @model_validator(mode="after")
    def source_has_required_proof(self) -> "LifecycleTrigger":
        if (
            self.source == LifecycleTriggerSource.VERIFIED_DEVICE_EVENT
            and not self.observation_id
        ):
            raise ValueError("verified device trigger requires observation_id")
        if self.source in {
            LifecycleTriggerSource.AUTHORIZED_COMMAND,
            LifecycleTriggerSource.EXPLICIT_REANALYSIS,
        } and not (self.actor_id and self.authorization_id):
            raise ValueError(
                "authorized command/reanalysis requires actor_id and "
                "authorization_id"
            )
        if (
            self.source == LifecycleTriggerSource.SOURCE_REPORT
            and not self.source_report_version_id
        ):
            raise ValueError(
                "source-report trigger requires source_report_version_id"
            )
        if self.received_at < self.occurred_at:
            raise ValueError("received_at cannot be before occurred_at")
        return self


class ManualMonitoringOverride(SleepDomainContract):
    schema_version: Literal["manual_monitoring_override.v1"] = (
        "manual_monitoring_override.v1"
    )
    state: MonitoringState
    actor_id: NonEmptyStr
    authorization_id: NonEmptyStr
    set_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def expiry_follows_start(self) -> "ManualMonitoringOverride":
        if self.expires_at <= self.set_at:
            raise ValueError("manual override expires_at must follow set_at")
        return self


class MonitoringSnapshot(SleepDomainContract):
    schema_version: Literal["monitoring_snapshot.v1"] = "monitoring_snapshot.v1"
    data_mode: DataMode
    subject_id: NonEmptyStr
    state: MonitoringState
    state_entered_at: datetime
    last_trigger_id: str | None = None
    last_trigger_source: LifecycleTriggerSource | None = None
    last_trigger_priority: int = Field(default=0, ge=0)
    debounce_until: datetime | None = None
    manual_override: ManualMonitoringOverride | None = None
    active_night_episode_id: str | None = None
    cas_version: int = Field(default=0, ge=0)
    updated_at: datetime

    @model_validator(mode="after")
    def snapshot_times_are_ordered(self) -> "MonitoringSnapshot":
        if self.updated_at < self.state_entered_at:
            raise ValueError("updated_at cannot be before state_entered_at")
        if self.state == MonitoringState.DORMANT and self.active_night_episode_id:
            raise ValueError("dormant monitoring cannot reference an active episode")
        return self


class EpisodeBoundaryPolicy(SleepDomainContract):
    schema_version: Literal["episode_boundary_policy.v1"] = (
        "episode_boundary_policy.v1"
    )
    policy_version: NonEmptyStr
    rollover_local_minute: int = Field(default=720, ge=0, le=1439)
    report_deadline_local_minute: int = Field(default=600, ge=0, le=1439)
    maximum_episode_seconds: int = Field(default=72_000, ge=3600)
    allowed_lateness_seconds: int = Field(default=21_600, ge=0)
    allow_received_at_fallback: bool = False

    def derive_local_sleep_date(
        self,
        event_at: datetime,
        timezone_name: str,
    ) -> date:
        local = event_at.astimezone(ZoneInfo(timezone_name))
        minute = local.hour * 60 + local.minute
        if minute < self.rollover_local_minute:
            return local.date() - timedelta(days=1)
        return local.date()

    def derive_night_key(
        self,
        *,
        subject_id: str,
        event_at: datetime,
        timezone_name: str,
    ) -> str:
        local_date = self.derive_local_sleep_date(event_at, timezone_name)
        return f"{subject_id}:{local_date.isoformat()}:{self.policy_version}"


class LifecycleTransitionPolicy(SleepDomainContract):
    schema_version: Literal["lifecycle_transition_policy.v1"] = (
        "lifecycle_transition_policy.v1"
    )
    policy_version: NonEmptyStr
    debounce_seconds: int = Field(default=30, ge=0)
    minimum_active_dwell_seconds: int = Field(default=300, ge=0)
    minimum_dormant_dwell_seconds: int = Field(default=60, ge=0)
    minimum_collecting_dwell_seconds: int = Field(default=300, ge=0)
    manual_override_seconds: int = Field(default=28_800, ge=1)
    lease_seconds: int = Field(default=30, ge=1)
    fallback_start_local_minute: int = Field(default=22 * 60, ge=0, le=1439)
    fallback_end_local_minute: int = Field(default=8 * 60, ge=0, le=1439)
    fallback_tolerance_seconds: int = Field(default=3600, ge=0)


class LifecycleTransitionReceipt(SleepDomainContract):
    schema_version: Literal["lifecycle_transition_receipt.v1"] = (
        "lifecycle_transition_receipt.v1"
    )
    transition_receipt_id: NonEmptyStr
    data_mode: DataMode
    component: LifecycleComponent
    aggregate_id: NonEmptyStr
    subject_id: NonEmptyStr
    trigger_id: NonEmptyStr
    trigger_source: LifecycleTriggerSource
    trigger_kind: LifecycleTriggerKind
    policy_version: NonEmptyStr
    from_state: NonEmptyStr
    to_state: NonEmptyStr
    reason_code: NonEmptyStr
    occurred_at: datetime
    committed_at: datetime

    @model_validator(mode="after")
    def committed_after_trigger(self) -> "LifecycleTransitionReceipt":
        if self.committed_at < self.occurred_at:
            raise ValueError("committed_at cannot be before occurred_at")
        return self


class NightEpisodeState(str, Enum):
    COLLECTING = "collecting"
    AWAITING_REPORT = "awaiting_report"
    ANALYZED = "analyzed"
    CLOSED = "closed"
    REVISED = "revised"


class CareFollowupState(str, Enum):
    NONE = "none"
    PENDING_FEEDBACK = "pending_feedback"
    FOLLOWING_UP = "following_up"
    COMPLETED = "completed"
    ENDED = "ended"


class ServiceMode(str, Enum):
    ACTIVE = "active"
    FOLLOW_UP = "follow_up"
    DORMANT = "dormant"


class QualityState(str, Enum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    DATA_INSUFFICIENT = "data_insufficient"


class MissingnessState(str, Enum):
    COMPLETE = "complete"
    GAPS_PRESENT = "gaps_present"
    UNKNOWN = "unknown"


class RiskState(str, Enum):
    UNKNOWN = "unknown"
    NO_REVIEWED_SIGNAL = "no_reviewed_signal"
    OPERATIONAL_REVIEW = "operational_review"
    REVIEWED_SIGNAL = "reviewed_signal"


class DomainRuleReviewStatus(str, Enum):
    APPROVED = "approved"
    PENDING_DOMAIN_REVIEW = "pending_domain_review"


class FastPathSignalType(str, Enum):
    RISK = "risk"
    BED = "bed"
    OFFLINE = "offline"
    QUALITY = "quality"


class FastPathReceiptOutcome(str, Enum):
    EMITTED = "emitted"
    SUPPRESSED_REPEAT = "suppressed_repeat"
    SUPPRESSED_HYSTERESIS = "suppressed_hysteresis"


class AlertCorrelationOutcome(str, Enum):
    OPENED = "opened"
    DUPLICATE_OPEN = "duplicate_open"
    CLOSED_UNIQUE_INSTANCE = "closed_unique_instance"
    ORPHAN_STOP = "orphan_stop"
    UNKEYED_SIGNAL = "unkeyed_signal"


class DeterministicSourceScope(SleepDomainContract):
    schema_version: Literal["deterministic_source_scope.v1"] = (
        "deterministic_source_scope.v1"
    )
    night_episode_id: NonEmptyStr
    night_episode_revision_id: str | None = None
    observation_ids: tuple[NonEmptyStr, ...]
    observation_types: tuple[ObservationType, ...]
    device_binding_ids: tuple[NonEmptyStr, ...]
    window_start_at: datetime
    window_end_at: datetime

    @model_validator(mode="after")
    def scope_is_coherent(self) -> "DeterministicSourceScope":
        if self.window_end_at <= self.window_start_at:
            raise ValueError("source scope window must have positive duration")
        for values, label in (
            (self.observation_ids, "observation ids"),
            (self.observation_types, "observation types"),
            (self.device_binding_ids, "binding ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"source scope {label} must be unique")
        return self


class DeterministicQualityPolicy(SleepDomainContract):
    schema_version: Literal["deterministic_quality_policy.v1"] = (
        "deterministic_quality_policy.v1"
    )
    policy_version: NonEmptyStr
    coverage_cadence_seconds: int = Field(default=180, ge=1)
    minimum_coverage_ratio: float = Field(default=0.75, ge=0, le=1)
    partial_coverage_ratio: float = Field(default=0.5, ge=0, le=1)
    stale_after_seconds: int = Field(default=600, ge=1)
    offline_after_seconds: int = Field(default=1200, ge=1)
    clock_invalid_flags: tuple[NonEmptyStr, ...] = (
        "clock_skew",
        "clock-skew",
        "clock_invalid",
        "time_untrusted",
        "timezone_unknown",
    )
    coverage_observation_types: tuple[ObservationType, ...] = (
        ObservationType.HEART_RATE,
        ObservationType.RESPIRATORY_RATE,
        ObservationType.BED_PRESENCE,
        ObservationType.MOVEMENT,
    )

    @model_validator(mode="after")
    def ratios_are_ordered(self) -> "DeterministicQualityPolicy":
        if self.partial_coverage_ratio > self.minimum_coverage_ratio:
            raise ValueError(
                "partial_coverage_ratio cannot exceed minimum_coverage_ratio"
            )
        if self.offline_after_seconds < self.stale_after_seconds:
            raise ValueError("offline threshold cannot precede stale threshold")
        if not self.coverage_observation_types:
            raise ValueError("coverage observation types cannot be empty")
        return self


class FastPathEventPolicy(SleepDomainContract):
    schema_version: Literal["fast_path_event_policy.v1"] = (
        "fast_path_event_policy.v1"
    )
    policy_version: NonEmptyStr
    recovery_hysteresis_observations: int = Field(default=2, ge=1)
    bed_hysteresis_observations: int = Field(default=2, ge=1)
    cooldown_seconds: int = Field(default=900, ge=0)


class DeterministicQualityAssessment(SleepDomainContract):
    schema_version: Literal["deterministic_quality_assessment.v1"] = (
        "deterministic_quality_assessment.v1"
    )
    assessment_id: NonEmptyStr
    data_mode: DataMode
    subject_id: NonEmptyStr
    night_episode_id: NonEmptyStr
    quality_state: QualityState
    data_sufficiency: DataSufficiency
    missingness_state: MissingnessState
    coverage_ratio: float = Field(..., ge=0, le=1)
    expected_bin_count: int = Field(..., ge=1)
    covered_bin_count: int = Field(..., ge=0)
    explicit_missing_interval_count: int = Field(..., ge=0)
    invalid_observation_count: int = Field(..., ge=0)
    stale: bool
    offline: bool
    clock_invalid: bool
    latest_observed_at: datetime | None = None
    source_scope: DeterministicSourceScope
    policy_version: NonEmptyStr
    reason_codes: tuple[NonEmptyStr, ...]
    assessed_at: datetime

    @model_validator(mode="after")
    def insufficiency_is_fail_closed(self) -> "DeterministicQualityAssessment":
        if self.covered_bin_count > self.expected_bin_count:
            raise ValueError("covered bins cannot exceed expected bins")
        if (
            self.stale
            or self.offline
            or self.clock_invalid
            or self.quality_state == QualityState.DATA_INSUFFICIENT
        ) and self.data_sufficiency != DataSufficiency.DATA_INSUFFICIENT:
            raise ValueError(
                "stale/offline/clock-invalid/insufficient quality must be "
                "data_insufficient"
            )
        return self


class ReviewedVendorAlertRule(SleepDomainContract):
    schema_version: Literal["reviewed_vendor_alert_rule.v1"] = (
        "reviewed_vendor_alert_rule.v1"
    )
    rule_id: NonEmptyStr
    rule_version: NonEmptyStr
    provider_id: NonEmptyStr
    alert_code: NonEmptyStr
    review_status: DomainRuleReviewStatus
    risk_state: RiskState
    health_escalation_allowed: bool = False
    evidence_references: tuple[NonEmptyStr, ...] = ()
    reviewed_by_actor_id: str | None = None
    reviewed_at: datetime | None = None

    @model_validator(mode="after")
    def only_approved_rules_can_escalate(self) -> "ReviewedVendorAlertRule":
        if self.review_status != DomainRuleReviewStatus.APPROVED and (
            self.risk_state == RiskState.REVIEWED_SIGNAL
            or self.health_escalation_allowed
        ):
            raise ValueError(
                "PENDING_DOMAIN_REVIEW rule cannot produce a reviewed signal "
                "or health escalation"
            )
        if self.review_status == DomainRuleReviewStatus.APPROVED and not (
            self.evidence_references
            and self.reviewed_by_actor_id
            and self.reviewed_at
        ):
            raise ValueError(
                "approved vendor alert rule requires immutable review evidence"
            )
        return self


class DeterministicRiskPolicy(SleepDomainContract):
    schema_version: Literal["deterministic_risk_policy.v1"] = (
        "deterministic_risk_policy.v1"
    )
    policy_version: NonEmptyStr
    vendor_alert_rules: tuple[ReviewedVendorAlertRule, ...] = ()

    @model_validator(mode="after")
    def rule_keys_are_unique(self) -> "DeterministicRiskPolicy":
        keys = [
            (rule.provider_id, rule.alert_code)
            for rule in self.vendor_alert_rules
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("vendor alert rule keys must be unique")
        return self


class CurrentRisk(SleepDomainContract):
    schema_version: Literal["current_risk.v1"] = "current_risk.v1"
    current_risk_id: NonEmptyStr
    data_mode: DataMode
    subject_id: NonEmptyStr
    night_episode_id: NonEmptyStr
    risk_state: RiskState
    data_sufficiency: DataSufficiency
    source_scope: DeterministicSourceScope
    policy_version: NonEmptyStr
    observed_at: datetime
    reason_codes: tuple[NonEmptyStr, ...]
    active_vendor_alert_instance_ids: tuple[NonEmptyStr, ...] = ()
    pending_domain_review_rule_ids: tuple[NonEmptyStr, ...] = ()
    health_escalation_allowed: bool = False
    is_all_clear: Literal[False] = False
    updated_at: datetime

    @model_validator(mode="after")
    def risk_is_fail_closed(self) -> "CurrentRisk":
        if self.data_sufficiency != DataSufficiency.SUFFICIENT and (
            self.risk_state != RiskState.UNKNOWN
            or self.health_escalation_allowed
        ):
            raise ValueError(
                "non-sufficient CurrentRisk must remain unknown without escalation"
            )
        if (
            self.risk_state != RiskState.REVIEWED_SIGNAL
            and self.health_escalation_allowed
        ):
            raise ValueError(
                "only an approved reviewed signal may allow health escalation"
            )
        return self


class VendorAlertInstance(SleepDomainContract):
    schema_version: Literal["vendor_alert_instance.v1"] = (
        "vendor_alert_instance.v1"
    )
    alert_instance_id: NonEmptyStr
    data_mode: DataMode
    subject_id: NonEmptyStr
    night_episode_id: NonEmptyStr
    provider_id: NonEmptyStr
    provider_account_id: NonEmptyStr
    device_id: NonEmptyStr
    vendor_alert_instance_id: NonEmptyStr
    alert_code: NonEmptyStr
    opened_by_observation_id: NonEmptyStr
    opened_at: datetime
    closed_by_observation_id: str | None = None
    closed_at: datetime | None = None
    cas_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def close_fields_match(self) -> "VendorAlertInstance":
        if (self.closed_by_observation_id is None) != (self.closed_at is None):
            raise ValueError("alert close observation and time must appear together")
        if self.closed_at is not None and self.closed_at < self.opened_at:
            raise ValueError("alert cannot close before it opens")
        return self


class AlertCorrelationReceipt(SleepDomainContract):
    schema_version: Literal["alert_correlation_receipt.v1"] = (
        "alert_correlation_receipt.v1"
    )
    receipt_id: NonEmptyStr
    data_mode: DataMode
    subject_id: NonEmptyStr
    night_episode_id: NonEmptyStr
    observation_id: NonEmptyStr
    vendor_alert_instance_id: str | None = None
    alert_code: NonEmptyStr
    outcome: AlertCorrelationOutcome
    matched_alert_instance_id: str | None = None
    occurred_at: datetime
    persisted_at: datetime


class FastPathSignalProjection(SleepDomainContract):
    schema_version: Literal["fast_path_signal_projection.v1"] = (
        "fast_path_signal_projection.v1"
    )
    data_mode: DataMode
    subject_id: NonEmptyStr
    night_episode_id: NonEmptyStr
    signal_type: FastPathSignalType
    current_state: NonEmptyStr
    candidate_state: str | None = None
    candidate_count: int = Field(default=0, ge=0)
    suppressed_repeat_count: int = Field(default=0, ge=0)
    last_emitted_at: datetime | None = None
    cooldown_until: datetime | None = None
    cas_version: int = Field(default=0, ge=0)
    policy_version: NonEmptyStr
    updated_at: datetime


class FastPathSignalReceipt(SleepDomainContract):
    schema_version: Literal["fast_path_signal_receipt.v1"] = (
        "fast_path_signal_receipt.v1"
    )
    receipt_id: NonEmptyStr
    evaluation_id: NonEmptyStr
    data_mode: DataMode
    subject_id: NonEmptyStr
    night_episode_id: NonEmptyStr
    signal_type: FastPathSignalType
    observed_state: NonEmptyStr
    previous_state: str | None = None
    outcome: FastPathReceiptOutcome
    suppressed_repeat_count: int = Field(default=0, ge=0)
    reason_code: NonEmptyStr
    policy_version: NonEmptyStr
    observed_at: datetime
    persisted_at: datetime


class CareFollowupSnapshot(SleepDomainContract):
    schema_version: Literal["care_followup_snapshot.v1"] = (
        "care_followup_snapshot.v1"
    )
    data_mode: DataMode
    subject_id: NonEmptyStr
    night_episode_id: NonEmptyStr
    state: CareFollowupState
    state_entered_at: datetime
    last_transition_id: str | None = None
    cas_version: int = Field(default=0, ge=0)
    updated_at: datetime


class CareFollowupCommand(SleepDomainContract):
    schema_version: Literal["care_followup_command.v1"] = (
        "care_followup_command.v1"
    )
    command_id: NonEmptyStr
    data_mode: DataMode
    subject_id: NonEmptyStr
    night_episode_id: NonEmptyStr
    target_state: CareFollowupState
    actor_id: NonEmptyStr
    authorization_id: NonEmptyStr
    reason_code: NonEmptyStr
    occurred_at: datetime
    correlation_id: NonEmptyStr

    @model_validator(mode="after")
    def target_is_a_real_transition_state(self) -> "CareFollowupCommand":
        if self.target_state == CareFollowupState.NONE:
            raise ValueError("CareFollowup command cannot target virtual none state")
        return self


class CareFollowupTransitionReceipt(SleepDomainContract):
    schema_version: Literal["care_followup_transition_receipt.v1"] = (
        "care_followup_transition_receipt.v1"
    )
    receipt_id: NonEmptyStr
    command_id: NonEmptyStr
    data_mode: DataMode
    subject_id: NonEmptyStr
    night_episode_id: NonEmptyStr
    from_state: CareFollowupState
    to_state: CareFollowupState
    actor_id: NonEmptyStr
    authorization_id: NonEmptyStr
    reason_code: NonEmptyStr
    occurred_at: datetime


class ServiceModeProjection(SleepDomainContract):
    schema_version: Literal["service_mode_projection.v1"] = (
        "service_mode_projection.v1"
    )
    data_mode: DataMode
    subject_id: NonEmptyStr
    mode: ServiceMode
    monitoring_state: MonitoringState
    active_night_episode_id: str | None = None
    open_followup_night_episode_ids: tuple[NonEmptyStr, ...] = ()
    projected_at: datetime

    @model_validator(mode="after")
    def mode_is_derived_only(self) -> "ServiceModeProjection":
        expected = (
            ServiceMode.ACTIVE
            if self.monitoring_state == MonitoringState.ACTIVE
            else (
                ServiceMode.FOLLOW_UP
                if self.open_followup_night_episode_ids
                else ServiceMode.DORMANT
            )
        )
        if self.mode != expected:
            raise ValueError(
                "ServiceMode must be derived from Monitoring and open follow-ups"
            )
        if (
            self.monitoring_state == MonitoringState.ACTIVE
            and self.active_night_episode_id is None
        ):
            raise ValueError("active ServiceMode requires an active NightEpisode")
        if len(self.open_followup_night_episode_ids) != len(
            set(self.open_followup_night_episode_ids)
        ):
            raise ValueError("open follow-up NightEpisode ids must be unique")
        return self


class CollectionWindowDerivation(str, Enum):
    VERIFIED_IN_BED = "verified_in_bed"
    EXTERNAL_COMMAND = "external_command"
    TIME_FALLBACK = "time_fallback"
    UNKNOWN = "unknown"


class DataSufficiency(str, Enum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    DATA_INSUFFICIENT = "data_insufficient"
    REPORT_PENDING = "report_pending"
    UNKNOWN = "unknown"


class DeviceBindingReference(SleepDomainContract):
    schema_version: Literal["device_binding_reference.v1"] = (
        "device_binding_reference.v1"
    )
    device_binding_id: NonEmptyStr
    binding_version: int = Field(..., ge=1)
    device_id: NonEmptyStr


class NightEpisode(SleepDomainContract):
    """Elder-centered sleep aggregate, distinct from product-Agent Episode runs."""

    schema_version: Literal["night_episode.v1"] = "night_episode.v1"
    night_episode_id: NonEmptyStr
    data_mode: DataMode
    subject_id: NonEmptyStr
    timezone_name: NonEmptyStr
    local_sleep_date: date
    night_key: NonEmptyStr
    collection_start_at: datetime
    collection_end_at: datetime | None = None
    allowed_lateness_watermark_at: datetime | None = None
    report_deadline_at: datetime | None = None
    collection_window_derivation: CollectionWindowDerivation
    binding_references: tuple[DeviceBindingReference, ...]
    observation_ids: tuple[NonEmptyStr, ...] = ()
    source_report_references: tuple[NonEmptyStr, ...] = ()
    night_episode_revision_ids: tuple[NonEmptyStr, ...] = ()
    transition_receipt_ids: tuple[NonEmptyStr, ...] = ()
    data_sufficiency: DataSufficiency
    quality_flags: tuple[NonEmptyStr, ...] = ()
    pinned_adapter_versions: dict[NonEmptyStr, NonEmptyStr]
    pinned_observation_schema_versions: tuple[NonEmptyStr, ...]
    pinned_policy_versions: dict[NonEmptyStr, NonEmptyStr]
    state: NightEpisodeState
    current_night_episode_revision_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_episode(self) -> "NightEpisode":
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone_name must be a valid IANA timezone") from exc
        if (
            self.collection_end_at is not None
            and self.collection_end_at <= self.collection_start_at
        ):
            raise ValueError("collection_end_at must be after collection_start_at")
        if (
            self.allowed_lateness_watermark_at is not None
            and self.collection_end_at is not None
            and self.allowed_lateness_watermark_at < self.collection_end_at
        ):
            raise ValueError(
                "allowed_lateness_watermark_at cannot precede collection_end_at"
            )
        if (
            self.report_deadline_at is not None
            and self.report_deadline_at <= self.collection_start_at
        ):
            raise ValueError("report_deadline_at must follow collection_start_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be before created_at")
        if not self.binding_references:
            raise ValueError("NightEpisode requires at least one binding reference")
        if not self.pinned_adapter_versions:
            raise ValueError("NightEpisode requires pinned Adapter versions")
        if not self.pinned_observation_schema_versions:
            raise ValueError("NightEpisode requires pinned observation Schema versions")
        if not self.pinned_policy_versions:
            raise ValueError("NightEpisode requires pinned policy versions")
        for values, label in (
            (self.observation_ids, "observation ids"),
            (self.source_report_references, "source-report references"),
            (self.night_episode_revision_ids, "revision ids"),
            (self.transition_receipt_ids, "transition receipt ids"),
            (
                self.pinned_observation_schema_versions,
                "pinned observation Schema versions",
            ),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"NightEpisode {label} must be unique")
        return self


class AssociationKind(str, Enum):
    OBSERVATION = "observation"
    SOURCE_REPORT = "source_report"


class AssociationStatus(str, Enum):
    PENDING = "pending"
    ASSOCIATED = "associated"
    QUARANTINED = "quarantined"


class PendingEpisodeAssociation(SleepDomainContract):
    schema_version: Literal["pending_episode_association.v1"] = (
        "pending_episode_association.v1"
    )
    association_id: NonEmptyStr
    data_mode: DataMode
    subject_id: str | None = None
    association_kind: AssociationKind
    source_resource_id: NonEmptyStr
    event_at: datetime | None = None
    binding_id: str | None = None
    candidate_night_episode_ids: tuple[NonEmptyStr, ...] = ()
    status: AssociationStatus
    reason_code: NonEmptyStr
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_night_episode_id: str | None = None

    @model_validator(mode="after")
    def resolution_matches_status(self) -> "PendingEpisodeAssociation":
        if self.status == AssociationStatus.ASSOCIATED and not (
            self.resolved_at and self.resolved_night_episode_id
        ):
            raise ValueError(
                "associated pending record requires resolution time and episode"
            )
        if self.status == AssociationStatus.PENDING and (
            self.resolved_at or self.resolved_night_episode_id
        ):
            raise ValueError("pending record cannot carry a resolution")
        return self


class NightRevisionCause(str, Enum):
    INITIAL_PUBLICATION = "initial_publication"
    LATE_OBSERVATION = "late_observation"
    CORRECTED_VENDOR_REPORT = "corrected_vendor_report"
    FEEDBACK = "feedback"
    EXPLICIT_REANALYSIS = "explicit_reanalysis"
    REPORT_DEADLINE = "report_deadline"


class NightEpisodeRevision(SleepDomainContract):
    schema_version: Literal["night_episode_revision.v1"] = (
        "night_episode_revision.v1"
    )
    night_episode_revision_id: NonEmptyStr
    night_episode_id: NonEmptyStr
    data_mode: DataMode
    subject_id: NonEmptyStr
    revision_number: int = Field(..., ge=1)
    parent_revision_id: str | None = None
    revision_cause: NightRevisionCause
    observation_ids: tuple[NonEmptyStr, ...]
    source_report_references: tuple[NonEmptyStr, ...] = ()
    source_report_sha256: tuple[Sha256Hex, ...] = ()
    observation_set_sha256: Sha256Hex
    data_sufficiency: DataSufficiency
    quality_flags: tuple[NonEmptyStr, ...] = ()
    created_at: datetime

    @model_validator(mode="after")
    def validate_revision_parent(self) -> "NightEpisodeRevision":
        if self.revision_number == 1 and self.parent_revision_id is not None:
            raise ValueError("revision 1 cannot have a parent_revision_id")
        if self.revision_number > 1 and not self.parent_revision_id:
            raise ValueError("revision >1 requires parent_revision_id")
        if len(self.observation_ids) != len(set(self.observation_ids)):
            raise ValueError("revision observation ids must be unique")
        if len(self.source_report_references) != len(
            self.source_report_sha256
        ):
            raise ValueError(
                "each source-report reference requires one exact content hash"
            )
        if len(self.source_report_references) != len(
            set(self.source_report_references)
        ):
            raise ValueError("revision source-report references must be unique")
        return self


class AnalysisStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    DEGRADED = "degraded"


class AnalysisRevision(SleepDomainContract):
    """Analysis of one exact NightEpisode revision.

    ``analysis_run_id`` identifies the existing internal product-Agent Episode;
    it is intentionally not the NightEpisode identity.
    """

    schema_version: Literal["analysis_revision.v1"] = "analysis_revision.v1"
    analysis_revision_id: NonEmptyStr
    night_episode_id: NonEmptyStr
    night_episode_revision_id: NonEmptyStr
    night_episode_revision_number: int = Field(..., ge=1)
    data_mode: DataMode
    subject_id: NonEmptyStr
    revision_number: int = Field(..., ge=1)
    parent_analysis_revision_id: str | None = None
    analysis_run_id: NonEmptyStr
    observation_set_sha256: Sha256Hex
    source_report_sha256: tuple[Sha256Hex, ...] = ()
    adapter_versions: dict[NonEmptyStr, NonEmptyStr]
    observation_schema_versions: tuple[NonEmptyStr, ...]
    policy_versions: dict[NonEmptyStr, NonEmptyStr]
    skill_versions: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)
    model_versions: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)
    data_sufficiency: DataSufficiency
    status: AnalysisStatus = AnalysisStatus.PENDING
    execution_mode: Literal[
        "intelligent",
        "safe_degraded",
        "deterministic_only",
        "pending",
    ] = "pending"
    failure_codes: tuple[NonEmptyStr, ...] = ()
    result_resource_id: str | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_analysis_parent(self) -> "AnalysisRevision":
        if self.revision_number == 1 and self.parent_analysis_revision_id is not None:
            raise ValueError("analysis revision 1 cannot have a parent")
        if self.revision_number > 1 and not self.parent_analysis_revision_id:
            raise ValueError("analysis revision >1 requires a parent")
        if self.status == AnalysisStatus.PENDING and self.execution_mode != "pending":
            raise ValueError("pending analysis requires pending execution mode")
        if self.status == AnalysisStatus.READY and self.execution_mode == "pending":
            raise ValueError("ready analysis cannot use pending execution mode")
        if self.status == AnalysisStatus.DEGRADED and not self.failure_codes:
            raise ValueError("degraded analysis requires an explicit failure code")
        return self


class AgentAnalysisTrigger(str, Enum):
    """The complete slow-path trigger allowlist.

    Webhook receipt, normalization, individual samples and committed-result
    queries deliberately have no representation here.
    """

    MORNING_ANALYSIS = "morning_analysis"
    EXPLICIT_REANALYSIS = "explicit_reanalysis"
    FEEDBACK = "feedback"
    FOLLOW_UP = "follow_up"
    INTERNAL_ANALYSIS = "internal_analysis"


class AnalysisRole(str, Enum):
    ELDER = "elder"
    FAMILY = "family"
    DOCTOR = "doctor"


class RoleViewStatus(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    PENDING = "pending"
    BLOCKED = "blocked"


class AnalysisRoleView(SleepDomainContract):
    """Append-only, role-scoped projection of one AnalysisRevision."""

    schema_version: Literal["analysis_role_view.v1"] = "analysis_role_view.v1"
    role_view_id: NonEmptyStr
    analysis_revision_id: NonEmptyStr
    night_episode_id: NonEmptyStr
    night_episode_revision_id: NonEmptyStr
    data_mode: DataMode
    subject_id: NonEmptyStr
    role: AnalysisRole
    status: RoleViewStatus
    product_agent_episode_id: NonEmptyStr
    execution_mode: Literal[
        "intelligent",
        "safe_degraded",
        "deterministic_only",
        "pending",
    ]
    content: str | None = Field(default=None, max_length=6000)
    context_notice: str | None = Field(default=None, max_length=500)
    claim_refs: tuple[NonEmptyStr, ...] = ()
    source_refs: tuple[NonEmptyStr, ...] = ()
    failure_codes: tuple[NonEmptyStr, ...] = ()
    generated_at: datetime

    @model_validator(mode="after")
    def validate_role_view_state(self) -> "AnalysisRoleView":
        if self.status == RoleViewStatus.READY and not self.content:
            raise ValueError("ready role view requires content")
        if self.status == RoleViewStatus.PENDING and self.execution_mode != "pending":
            raise ValueError("pending role view requires pending execution mode")
        if self.status in {RoleViewStatus.DEGRADED, RoleViewStatus.BLOCKED} and (
            not self.failure_codes
        ):
            raise ValueError(
                "degraded/blocked role view requires an explicit failure code"
            )
        return self


class OperationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Operation(SleepDomainContract):
    schema_version: Literal["operation.v1"] = "operation.v1"
    operation_id: NonEmptyStr
    data_mode: DataMode
    operation_type: NonEmptyStr
    subject_id: NonEmptyStr
    service_principal_id: NonEmptyStr
    actor_id: NonEmptyStr
    target_resource_id: str | None = None
    idempotency_key: NonEmptyStr
    request_sha256: Sha256Hex
    status: OperationStatus
    attempt_count: int = Field(default=0, ge=0)
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    result_resource_id: str | None = None
    error_code: str | None = None
    correlation_id: NonEmptyStr
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_operation_state(self) -> "Operation":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be before created_at")
        if self.status == OperationStatus.RUNNING and (
            not self.lease_owner or self.lease_expires_at is None
        ):
            raise ValueError("running operation requires an active lease")
        if self.status == OperationStatus.SUCCEEDED and not self.result_resource_id:
            raise ValueError("succeeded operation requires result_resource_id")
        if self.status == OperationStatus.FAILED and not self.error_code:
            raise ValueError("failed operation requires error_code")
        return self


class DomainEventType(str, Enum):
    MONITORING_ACTIVATED = "MONITORING_ACTIVATED"
    MONITORING_DORMANT = "MONITORING_DORMANT"
    ELDER_IN_BED = "ELDER_IN_BED"
    BED_EXIT_DETECTED = "BED_EXIT_DETECTED"
    DEVICE_OFFLINE = "DEVICE_OFFLINE"
    DATA_QUALITY_INSUFFICIENT = "DATA_QUALITY_INSUFFICIENT"
    DEVICE_BINDING_REQUIRED = "DEVICE_BINDING_REQUIRED"
    RISK_SIGNAL_DETECTED = "RISK_SIGNAL_DETECTED"
    NIGHT_EPISODE_AWAITING_REPORT = "NIGHT_EPISODE_AWAITING_REPORT"
    NIGHT_EPISODE_ANALYZED = "NIGHT_EPISODE_ANALYZED"
    NIGHT_EPISODE_REVISED = "NIGHT_EPISODE_REVISED"
    NIGHT_EPISODE_CLOSED = "NIGHT_EPISODE_CLOSED"
    MORNING_REPORT_READY = "MORNING_REPORT_READY"
    FEEDBACK_REQUIRED = "FEEDBACK_REQUIRED"
    FAMILY_ATTENTION_SUGGESTED = "FAMILY_ATTENTION_SUGGESTED"
    DOCTOR_CONTACT_SUGGESTED = "DOCTOR_CONTACT_SUGGESTED"
    RISK_STATE_CHANGED = "RISK_STATE_CHANGED"
    BED_STATE_CHANGED = "BED_STATE_CHANGED"
    DEVICE_ONLINE = "DEVICE_ONLINE"
    DATA_QUALITY_RECOVERED = "DATA_QUALITY_RECOVERED"
    AGENT_ANALYSIS_READY = "AGENT_ANALYSIS_READY"
    AGENT_ANALYSIS_DEGRADED = "AGENT_ANALYSIS_DEGRADED"
    CARE_FOLLOWUP_PENDING = "CARE_FOLLOWUP_PENDING"
    CARE_FOLLOWUP_STARTED = "CARE_FOLLOWUP_STARTED"
    CARE_FOLLOWUP_COMPLETED = "CARE_FOLLOWUP_COMPLETED"
    CARE_FOLLOWUP_ENDED = "CARE_FOLLOWUP_ENDED"
    FEEDBACK_SUBMITTED = "FEEDBACK_SUBMITTED"


class DomainEvent(SleepDomainContract):
    schema_version: Literal["domain_event.v1"] = "domain_event.v1"
    event_id: NonEmptyStr
    event_type: DomainEventType
    event_version: NonEmptyStr
    data_mode: DataMode
    aggregate_type: NonEmptyStr
    aggregate_id: NonEmptyStr
    aggregate_version: int = Field(..., ge=1)
    per_aggregate_sequence: int = Field(..., ge=1)
    delivery_offset: int = Field(..., ge=1)
    subject_id: NonEmptyStr
    night_episode_id: str | None = None
    night_episode_revision_id: str | None = None
    operation_id: str | None = None
    event_occurred_at: datetime
    persisted_at: datetime
    correlation_id: NonEmptyStr
    causation_id: str | None = None
    attributes: dict[NonEmptyStr, EventAttribute] = Field(default_factory=dict)

    @model_validator(mode="after")
    def persisted_not_before_occurrence(self) -> "DomainEvent":
        if self.persisted_at < self.event_occurred_at:
            raise ValueError("persisted_at cannot be before event_occurred_at")
        return self


__all__ = [
    "AdapterCapability",
    "AdapterDeploymentEvent",
    "AdapterDeploymentStatus",
    "AdapterDescriptor",
    "AdapterObservationCandidate",
    "AdapterResolutionLock",
    "AlertLifecycleState",
    "AlertSeverity",
    "AlgorithmVersionValue",
    "AgentAnalysisTrigger",
    "AnalysisRole",
    "AnalysisRoleView",
    "AnalysisRevision",
    "AnalysisStatus",
    "AvailabilityState",
    "BedExitKind",
    "BedExitPayload",
    "BedPresencePayload",
    "BedPresenceState",
    "bind_adapter_candidate",
    "CalibrationValue",
    "CapabilityDeclaration",
    "CapabilitySupport",
    "CapabilityVerificationReceipt",
    "CapabilityVerificationStatus",
    "CareFollowupState",
    "CareFollowupCommand",
    "CareFollowupSnapshot",
    "CareFollowupTransitionReceipt",
    "CollectionWindowDerivation",
    "ConfidenceValue",
    "CurrentRevisionPointer",
    "DataMode",
    "DataSufficiency",
    "DeterministicQualityAssessment",
    "DeterministicQualityPolicy",
    "DeterministicRiskPolicy",
    "DeterministicSourceScope",
    "DomainRuleReviewStatus",
    "DomainNamespace",
    "EpisodeBoundaryPolicy",
    "DeviceBinding",
    "DeviceBindingAuditAction",
    "DeviceBindingAuditEvent",
    "DeviceBindingReference",
    "DeviceBindingStatus",
    "DeviceConnectivityPayload",
    "DeviceConnectivityState",
    "DomainEvent",
    "DomainEventType",
    "FastPathEventPolicy",
    "FastPathReceiptOutcome",
    "FastPathSignalProjection",
    "FastPathSignalReceipt",
    "FastPathSignalType",
    "HeartRatePayload",
    "MissingIntervalPayload",
    "MissingState",
    "MonitoringState",
    "MissingnessState",
    "MonitoringSnapshot",
    "ManualMonitoringOverride",
    "LifecycleComponent",
    "LifecycleTransitionPolicy",
    "LifecycleTransitionReceipt",
    "LifecycleTrigger",
    "LifecycleTriggerKind",
    "LifecycleTriggerSource",
    "MovementPayload",
    "NightEpisode",
    "NightEpisodeRevision",
    "NightEpisodeState",
    "NightRevisionCause",
    "NamespaceMismatchError",
    "AssociationKind",
    "AssociationStatus",
    "PendingEpisodeAssociation",
    "ObservationPayload",
    "ObservationProvenance",
    "ObservationQuality",
    "ObservationType",
    "Operation",
    "OperationStatus",
    "QualityState",
    "ProcessingOutcome",
    "ProcessingReceipt",
    "ProcessingStage",
    "ProviderDeviceIdentity",
    "QuarantineReprocessAudit",
    "QuarantineReason",
    "RawIngressRecord",
    "RawIngressRecordMetadata",
    "RespiratoryRatePayload",
    "ResolvedAdapterCapability",
    "ServiceMode",
    "ServiceModeProjection",
    "CurrentRisk",
    "RiskState",
    "RoleViewStatus",
    "ReviewedVendorAlertRule",
    "AlertCorrelationOutcome",
    "AlertCorrelationReceipt",
    "VendorAlertInstance",
    "SignatureVerificationState",
    "SleepObservation",
    "SleepStageIntervalPayload",
    "SleepStageState",
    "SourceKind",
    "SubjectLifecycleLease",
    "TimezoneStatus",
    "UnknownObservationPayload",
    "VendorAlertPayload",
    "VendorSleepProfileMetricPayload",
    "VerificationReviewerKind",
]
