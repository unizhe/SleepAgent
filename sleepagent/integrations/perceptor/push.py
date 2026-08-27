"""Strict offline parsing and normalization for YunYun V2.5.2 Push data."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TypeAlias, cast

from sleepagent.domain.contracts import (
    AdapterObservationCandidate,
    AlgorithmVersionValue,
    AlertLifecycleState,
    AlertSeverity,
    AvailabilityState,
    BedPresencePayload,
    BedPresenceState,
    CalibrationValue,
    ConfidenceValue,
    DataMode,
    DeviceConnectivityPayload,
    DeviceConnectivityState,
    HeartRatePayload,
    MissingIntervalPayload,
    MissingState,
    MovementPayload,
    ObservationPayload,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    ProviderDeviceIdentity,
    RespiratoryRatePayload,
    SourceKind,
    TimezoneStatus,
    VendorAlertPayload,
)
from .signing import (
    PUSH_SIGNING_PATH,
    PUSH_SIGNING_KEY_MODE,
    SigningRepresentationUnresolved,
    verify_parameters_signature,
)


PROVIDER_ID = "perceptor"
ADAPTER_ID = "yunyun-v2.5.2-push"
ADAPTER_VERSION = "2.5.2+p4c-r1.1"
PARSER_VERSION = "yunyun_push_parser.v2.5.2-p4c-r1.1"
NORMALIZER_VERSION = "yunyun_push_normalizer.v2.5.2-p4c-r1.1"

SUPPORTED_EVENT_TYPES = frozenset(
    {
        "VitalSignsDataEvent",
        "ConnectedEvent",
        "DisconnectedEvent",
        "AlarmEvent",
        "AlarmStopEvent",
    }
)
OUTER_FIELDS = frozenset(
    {
        "client_id",
        "version",
        "timestamp",
        "sign",
        "sign_version",
        "sign_nonce",
        "sign_method",
        "message_id",
        "product_id",
        "device_id",
        "device_name",
        "home_id",
        "type",
        "data",
    }
)

JsonObject: TypeAlias = dict[str, object]


class PushContractError(ValueError):
    """Malformed or contract-invalid Push input."""


class DuplicateJsonKeyError(PushContractError):
    pass


class UnsupportedPushContractError(PushContractError):
    pass


class DataRepresentation(str, Enum):
    ESCAPED_JSON_STRING = "escaped_json_string"
    EMPTY_STRING = "empty_string"
    OBJECT_DOCUMENTED_EXAMPLE = "object_documented_example"


class FieldState(str, Enum):
    ABSENT = "absent"
    NULL = "null"
    EMPTY = "empty"
    PRESENT = "present"


@dataclass(frozen=True)
class VendorField:
    name: str
    state: FieldState
    value: object = None

    @property
    def present_value(self) -> object | None:
        return self.value if self.state == FieldState.PRESENT else None


@dataclass(frozen=True)
class VitalSignsDataEvent:
    event_type: str
    heart_rate: VendorField
    breath_rate: VendorField
    body_shake: VendorField
    onbed: VendorField
    report_time: VendorField
    date_time: VendorField


@dataclass(frozen=True)
class ConnectivityEvent:
    event_type: str


@dataclass(frozen=True)
class AlarmEvent:
    event_type: str
    alarm_id: VendorField
    alarm_level: VendorField
    value: VendorField
    alarm_reason: VendorField
    alarm_timestamp: VendorField
    alarm_params: VendorField
    firmware_version: VendorField
    algorithm_version: VendorField


@dataclass(frozen=True)
class AlarmStopEvent:
    event_type: str
    alarm_id: VendorField
    alarm_level: VendorField
    value: VendorField
    stop_mode: VendorField
    stop_timestamp: VendorField


@dataclass(frozen=True)
class UnknownPushEvent:
    """Authenticated future event retained without semantic interpretation."""

    event_type: str


VendorPushEvent: TypeAlias = (
    VitalSignsDataEvent
    | ConnectivityEvent
    | AlarmEvent
    | AlarmStopEvent
    | UnknownPushEvent
)


@dataclass(frozen=True)
class ParsedPushEnvelope:
    raw_body: bytes = field(repr=False)
    raw_payload_sha256: str
    client_id: str
    version: str
    timestamp: object
    signature: str = field(repr=False)
    sign_version: str
    sign_nonce: str
    sign_method: str
    message_id: VendorField
    product_id: str
    device_id: VendorField
    device_name: str
    home_id: str
    event_type: str
    data_representation: DataRepresentation
    event: VendorPushEvent
    signing_parameters: tuple[tuple[str, object], ...] = field(repr=False)
    parser_version: str = PARSER_VERSION

    @property
    def idempotency_identity(self) -> str:
        message_id = _field_text(self.message_id)
        if message_id:
            return "provider-message-id.v1:" + _sha256_text(message_id)
        alarm_id = _event_alarm_id(self.event)
        if alarm_id:
            return "provider-alarm-id.v1:" + _sha256_text(alarm_id)
        return f"raw-payload-sha256.v1:{self.raw_payload_sha256}"


def parse_push_envelope(
    raw_body: bytes,
    *,
    allow_unknown_event: bool = False,
) -> ParsedPushEnvelope:
    """Parse exact raw bytes without reserialization or transport assumptions."""

    if not isinstance(raw_body, bytes):
        raise TypeError("raw_body must be bytes")
    payload = _strict_json_object(raw_body, context="Push envelope")
    unknown = sorted(set(payload) - OUTER_FIELDS)
    if unknown:
        raise UnsupportedPushContractError(
            f"unsupported top-level fields: {', '.join(unknown)}"
        )

    client_id = _require_nonempty_text(payload, "client_id")
    version = _require_nonempty_text(payload, "version")
    sign_version = _require_nonempty_text(payload, "sign_version")
    sign_method = _require_nonempty_text(payload, "sign_method")
    if version != "2.0":
        raise UnsupportedPushContractError("unsupported Push version")
    if sign_version != "2.0":
        raise UnsupportedPushContractError("unsupported signature version")
    if sign_method != "HMAC-SHA1":
        raise UnsupportedPushContractError("unsupported signature method")

    signature = _require_nonempty_text(payload, "sign")
    _validate_hmac_sha1_base64(signature)
    sign_nonce = _require_nonempty_text(payload, "sign_nonce")
    event_type = _require_nonempty_text(payload, "type")
    event_supported = event_type in SUPPORTED_EVENT_TYPES
    if not event_supported and not allow_unknown_event:
        raise UnsupportedPushContractError(
            f"unsupported Push event type: {event_type}"
        )

    product_id = _require_identifier(payload, "product_id")
    device_name = _require_nonempty_text(payload, "device_name")
    home_id = _require_identifier(payload, "home_id")
    timestamp = _require_scalar(payload, "timestamp")
    message_id = _capture_field(payload, "message_id")
    device_id = _capture_field(payload, "device_id")
    if message_id.state == FieldState.PRESENT:
        _identifier_text(message_id.value, "message_id")
    if device_id.state == FieldState.PRESENT:
        _identifier_text(device_id.value, "device_id")

    data, representation = _parse_event_data(
        payload,
        event_type=event_type,
        allow_unknown_event=allow_unknown_event,
    )
    event = (
        _parse_typed_event(event_type, data)
        if event_supported
        else UnknownPushEvent(event_type=event_type)
    )
    signing_parameters = tuple(
        (key, value) for key, value in payload.items() if key != "sign"
    )
    return ParsedPushEnvelope(
        raw_body=raw_body,
        raw_payload_sha256=hashlib.sha256(raw_body).hexdigest(),
        client_id=client_id,
        version=version,
        timestamp=timestamp,
        signature=signature,
        sign_version=sign_version,
        sign_nonce=sign_nonce,
        sign_method=sign_method,
        message_id=message_id,
        product_id=product_id,
        device_id=device_id,
        device_name=device_name,
        home_id=home_id,
        event_type=event_type,
        data_representation=representation,
        event=event,
        signing_parameters=signing_parameters,
    )


def verify_push_envelope_signature(
    envelope: ParsedPushEnvelope,
    *,
    client_secret: str,
) -> bool:
    """Verify the one P4-C-R2-proven incoming Push construction.

    The real tenant differs from the written key formula: it uses the empty
    Push path with ``ClientSecret + "&"``. Object-form ``data`` remains
    unverifiable because the genuine golden proved escaped-string data only.
    """

    if envelope.data_representation == DataRepresentation.OBJECT_DOCUMENTED_EXAMPLE:
        raise SigningRepresentationUnresolved(
            "object-form Push data signing needs a vendor/live golden"
        )
    return verify_parameters_signature(
        dict(envelope.signing_parameters),
        supplied_signature=envelope.signature,
        client_secret=client_secret,
        signing_path=PUSH_SIGNING_PATH,
        key_mode=PUSH_SIGNING_KEY_MODE,
    )


def push_request_signed_at(envelope: ParsedPushEnvelope) -> datetime | None:
    """Return the normalized outer signing timestamp, or ``None`` if invalid."""

    parsed, _status, _source_text = _parse_source_timestamp(envelope.timestamp)
    return parsed


def normalize_push_envelope(
    envelope: ParsedPushEnvelope,
    *,
    provider_account_id: str,
    data_mode: DataMode,
    received_at: datetime,
    raw_ingress_record_id: str | None = None,
) -> tuple[AdapterObservationCandidate, ...]:
    if not provider_account_id:
        raise ValueError("provider_account_id is required")
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("received_at must be timezone-aware")
    provider_device = ProviderDeviceIdentity(
        provider_device_id=(
            _identifier_text(envelope.device_id.value, "device_id")
            if envelope.device_id.state == FieldState.PRESENT
            else None
        ),
        provider_device_name=envelope.device_name,
        product_id=envelope.product_id,
        home_id=envelope.home_id,
    )
    request_signed_at, _, _ = _parse_source_timestamp(envelope.timestamp)
    logical_event_key = _sha256_text(
        "|".join(
            (
                PROVIDER_ID,
                provider_account_id,
                envelope.idempotency_identity,
            )
        )
    )
    raw_event_key = _sha256_text(
        "|".join(
            (
                logical_event_key,
                envelope.raw_payload_sha256,
            )
        )
    )
    if raw_ingress_record_id is not None and not raw_ingress_record_id.strip():
        raise ValueError("raw_ingress_record_id must be non-empty when supplied")
    # Offline contract replay has no durable row and therefore retains the
    # deterministic reference used by P4-B/C.  A durable processor must pass
    # the database-owned identifier so embedded provenance and relational
    # foreign keys describe the same immutable raw evidence.
    raw_record_id = raw_ingress_record_id or f"perceptor:raw:{raw_event_key}"
    common = _CandidateContext(
        envelope=envelope,
        provider_account_id=provider_account_id,
        provider_device=provider_device,
        data_mode=data_mode,
        received_at=received_at,
        request_signed_at=request_signed_at,
        raw_record_id=raw_record_id,
        logical_event_key=logical_event_key,
        raw_event_key=raw_event_key,
    )
    event = envelope.event
    if isinstance(event, VitalSignsDataEvent):
        return _normalize_vital(event, common)
    if isinstance(event, ConnectivityEvent):
        return (_normalize_connectivity(event, common),)
    if isinstance(event, AlarmEvent):
        return (_normalize_alarm(event, common),)
    if isinstance(event, AlarmStopEvent):
        return (_normalize_alarm_stop(event, common),)
    raise AssertionError("unreachable supported event")


@dataclass(frozen=True)
class _CandidateContext:
    envelope: ParsedPushEnvelope
    provider_account_id: str
    provider_device: ProviderDeviceIdentity
    data_mode: DataMode
    received_at: datetime
    request_signed_at: datetime | None
    raw_record_id: str
    logical_event_key: str
    raw_event_key: str


def _normalize_vital(
    event: VitalSignsDataEvent,
    context: _CandidateContext,
) -> tuple[AdapterObservationCandidate, ...]:
    measured_at, timezone_status, source_text = _timestamp_from_field(
        event.report_time,
        unix_milliseconds=True,
    )
    specs = (
        (
            "heart-rate",
            event.heart_rate,
            ObservationType.HEART_RATE,
            0.0,
            300.0,
            HeartRatePayload,
        ),
        (
            "respiratory-rate",
            event.breath_rate,
            ObservationType.RESPIRATORY_RATE,
            0.0,
            150.0,
            RespiratoryRatePayload,
        ),
        (
            "movement",
            event.body_shake,
            ObservationType.MOVEMENT,
            -1.0,
            math.inf,
            MovementPayload,
        ),
    )
    candidates: list[AdapterObservationCandidate] = []
    for suffix, vendor_field, observation_type, lower, upper, payload_type in specs:
        numeric, missing_state, reason = _normalize_numeric_field(
            vendor_field,
            lower_exclusive=lower,
            upper_inclusive=upper,
        )
        if numeric is None:
            candidates.append(
                _candidate(
                    context,
                    suffix=suffix,
                    observation_type=ObservationType.MISSING_INTERVAL,
                    payload=MissingIntervalPayload(
                        target_observation_type=observation_type,
                        missing_state=missing_state,
                        reason_code=reason,
                        interval_start_at=measured_at,
                    ),
                    source_kind=SourceKind.DEVICE_MEASURED,
                    measurement_at=measured_at,
                    event_occurred_at=None,
                    timezone_status=timezone_status,
                    source_timestamp_text=source_text,
                    missing_state=missing_state,
                    quality_flags=(reason,),
                )
            )
            continue
        payload: ObservationPayload
        if payload_type is HeartRatePayload:
            payload = HeartRatePayload(value=numeric)
        elif payload_type is RespiratoryRatePayload:
            payload = RespiratoryRatePayload(value=numeric)
        else:
            payload = MovementPayload(value=numeric)
        candidates.append(
            _candidate(
                context,
                suffix=suffix,
                observation_type=observation_type,
                payload=payload,
                source_kind=SourceKind.DEVICE_MEASURED,
                measurement_at=measured_at,
                event_occurred_at=None,
                timezone_status=timezone_status,
                source_timestamp_text=source_text,
            )
        )

    bed_state, bed_missing, bed_flag = _normalize_push_onbed(event.onbed)
    if bed_state is None:
        candidates.append(
            _candidate(
                context,
                suffix="bed-presence",
                observation_type=ObservationType.MISSING_INTERVAL,
                payload=MissingIntervalPayload(
                    target_observation_type=ObservationType.BED_PRESENCE,
                    missing_state=bed_missing,
                    reason_code=bed_flag,
                    interval_start_at=measured_at,
                ),
                source_kind=SourceKind.DEVICE_MEASURED,
                measurement_at=measured_at,
                event_occurred_at=None,
                timezone_status=timezone_status,
                source_timestamp_text=source_text,
                missing_state=bed_missing,
                quality_flags=(bed_flag,),
            )
        )
    else:
        candidates.append(
            _candidate(
                context,
                suffix="bed-presence",
                observation_type=ObservationType.BED_PRESENCE,
                payload=BedPresencePayload(state=bed_state),
                source_kind=SourceKind.DEVICE_MEASURED,
                measurement_at=measured_at,
                event_occurred_at=None,
                timezone_status=timezone_status,
                source_timestamp_text=source_text,
                missing_state=(
                    MissingState.UNKNOWN
                    if bed_state == BedPresenceState.UNKNOWN
                    else MissingState.PRESENT
                ),
                quality_flags=((bed_flag,) if bed_flag else ()),
            )
        )
    return tuple(candidates)


def _normalize_connectivity(
    event: ConnectivityEvent,
    context: _CandidateContext,
) -> AdapterObservationCandidate:
    occurred_at, status, source_text = _parse_source_timestamp(
        context.envelope.timestamp
    )
    state = (
        DeviceConnectivityState.ONLINE
        if event.event_type == "ConnectedEvent"
        else DeviceConnectivityState.OFFLINE
    )
    return _candidate(
        context,
        suffix="connectivity",
        observation_type=ObservationType.DEVICE_CONNECTIVITY,
        payload=DeviceConnectivityPayload(
            state=state,
            vendor_status_code=event.event_type,
        ),
        source_kind=SourceKind.DEVICE_MEASURED,
        measurement_at=None,
        event_occurred_at=occurred_at,
        timezone_status=status,
        source_timestamp_text=source_text,
    )


def _normalize_alarm(
    event: AlarmEvent,
    context: _CandidateContext,
) -> AdapterObservationCandidate:
    occurred_at, status, source_text = _timestamp_from_field(event.alarm_timestamp)
    return _candidate(
        context,
        suffix="alarm-active",
        observation_type=ObservationType.VENDOR_ALERT,
        payload=VendorAlertPayload(
            alert_code=_required_field_text(event.value),
            severity=AlertSeverity.UNKNOWN,
            lifecycle_state=AlertLifecycleState.ACTIVE,
            vendor_alert_instance_id=_required_field_text(event.alarm_id),
            message=_field_text(event.alarm_reason),
        ),
        source_kind=SourceKind.VENDOR_DERIVED,
        measurement_at=None,
        event_occurred_at=occurred_at,
        timezone_status=status,
        source_timestamp_text=source_text,
        quality_flags=("vendor_alarm_code_uninterpreted",),
        limitations=(
            "vendor_alarm_level_not_mapped_to_sleepagent_severity",
            "no_clinical_urgency_inferred",
        ),
    )


def _normalize_alarm_stop(
    event: AlarmStopEvent,
    context: _CandidateContext,
) -> AdapterObservationCandidate:
    occurred_at, status, source_text = _timestamp_from_field(event.stop_timestamp)
    return _candidate(
        context,
        suffix="alarm-stopped",
        observation_type=ObservationType.VENDOR_ALERT,
        payload=VendorAlertPayload(
            alert_code=_required_field_text(event.value),
            severity=AlertSeverity.UNKNOWN,
            lifecycle_state=AlertLifecycleState.STOPPED,
            vendor_alert_instance_id=_required_field_text(event.alarm_id),
        ),
        source_kind=SourceKind.VENDOR_DERIVED,
        measurement_at=None,
        event_occurred_at=occurred_at,
        timezone_status=status,
        source_timestamp_text=source_text,
        quality_flags=("vendor_alarm_code_uninterpreted",),
        limitations=(
            "vendor_alarm_level_not_mapped_to_sleepagent_severity",
            "no_clinical_urgency_inferred",
        ),
    )


def _candidate(
    context: _CandidateContext,
    *,
    suffix: str,
    observation_type: ObservationType,
    payload: ObservationPayload,
    source_kind: SourceKind,
    measurement_at: datetime | None,
    event_occurred_at: datetime | None,
    timezone_status: TimezoneStatus,
    source_timestamp_text: str | None,
    missing_state: MissingState = MissingState.PRESENT,
    quality_flags: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
) -> AdapterObservationCandidate:
    identity = _sha256_text(
        f"{context.raw_event_key}|{suffix}|{ADAPTER_VERSION}"
    )
    idempotency_key = (
        f"perceptor:event.v1:{context.logical_event_key}:"
        f"{suffix}"
    )
    return AdapterObservationCandidate(
        candidate_id=f"perceptor:candidate:{identity}",
        data_mode=context.data_mode,
        observation_type=observation_type,
        payload=payload,
        source_kind=source_kind,
        provider_id=PROVIDER_ID,
        provider_account_id=context.provider_account_id,
        provider_device=context.provider_device,
        request_signed_at=context.request_signed_at,
        measurement_at=measurement_at,
        event_occurred_at=event_occurred_at,
        received_at=context.received_at,
        source_timestamp_text=source_timestamp_text,
        timezone_status=timezone_status,
        quality=ObservationQuality(
            missing_state=missing_state,
            confidence=ConfidenceValue(state=AvailabilityState.NOT_PROVIDED),
            algorithm_version=AlgorithmVersionValue(
                state=AvailabilityState.NOT_PROVIDED
            ),
            calibration=CalibrationValue(state=AvailabilityState.NOT_PROVIDED),
            quality_flags=quality_flags,
            processing_steps=(PARSER_VERSION, NORMALIZER_VERSION),
            limitations=("offline_contract_not_live_verified",) + limitations,
        ),
        provenance=ObservationProvenance(
            provider_id=PROVIDER_ID,
            provider_account_id=context.provider_account_id,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            raw_ingress_record_id=context.raw_record_id,
            raw_payload_sha256=context.envelope.raw_payload_sha256,
            source_record_id=_field_text(context.envelope.message_id),
            source_idempotency_key=context.envelope.idempotency_identity,
            producer_name="yunyun_v2_5_2_offline_normalizer",
        ),
        source_key=f"perceptor:{context.raw_event_key}:{suffix}",
        idempotency_key=idempotency_key,
    )


def _parse_typed_event(event_type: str, data: JsonObject) -> VendorPushEvent:
    if event_type == "VitalSignsDataEvent":
        _reject_unknown_event_fields(
            data,
            {
                "HeartRate",
                "BreathRate",
                "BodyShake",
                "Onbed",
                "OnBed",
                "ReportTime",
                "DateTime",
            },
        )
        return VitalSignsDataEvent(
            event_type=event_type,
            heart_rate=_capture_field(data, "HeartRate"),
            breath_rate=_capture_field(data, "BreathRate"),
            body_shake=_capture_field(data, "BodyShake"),
            onbed=_capture_vital_onbed(data),
            report_time=_capture_field(data, "ReportTime"),
            # P4-C-R1 recorded-real traffic carries this additional source time.
            # It remains typed vendor evidence and is not promoted canonically.
            date_time=_capture_field(data, "DateTime"),
        )
    if event_type in {"ConnectedEvent", "DisconnectedEvent"}:
        if data:
            raise UnsupportedPushContractError(
                f"{event_type} does not define event-specific data"
            )
        return ConnectivityEvent(event_type=event_type)
    if event_type == "AlarmEvent":
        allowed = {
            "AlarmId",
            "AlarmLevel",
            "value",
            "AlarmReason",
            "AlarmTStamp",
            "AlarmParams",
            "FwVer",
            "AlgoVer",
        }
        _reject_unknown_event_fields(data, allowed)
        event = AlarmEvent(
            event_type=event_type,
            alarm_id=_capture_field(data, "AlarmId"),
            alarm_level=_capture_field(data, "AlarmLevel"),
            value=_capture_field(data, "value"),
            alarm_reason=_capture_field(data, "AlarmReason"),
            alarm_timestamp=_capture_field(data, "AlarmTStamp"),
            alarm_params=_capture_field(data, "AlarmParams"),
            firmware_version=_capture_field(data, "FwVer"),
            algorithm_version=_capture_field(data, "AlgoVer"),
        )
        for required in (
            event.alarm_id,
            event.alarm_level,
            event.value,
            event.alarm_timestamp,
        ):
            _require_present_field(required)
        return event
    if event_type == "AlarmStopEvent":
        allowed = {"AlarmId", "AlarmLevel", "value", "StopMode", "StopTStamp"}
        _reject_unknown_event_fields(data, allowed)
        event = AlarmStopEvent(
            event_type=event_type,
            alarm_id=_capture_field(data, "AlarmId"),
            alarm_level=_capture_field(data, "AlarmLevel"),
            value=_capture_field(data, "value"),
            stop_mode=_capture_field(data, "StopMode"),
            stop_timestamp=_capture_field(data, "StopTStamp"),
        )
        for required in (
            event.alarm_id,
            event.alarm_level,
            event.value,
            event.stop_mode,
            event.stop_timestamp,
        ):
            _require_present_field(required)
        return event
    raise UnsupportedPushContractError(f"unsupported Push event type: {event_type}")


def _parse_event_data(
    payload: JsonObject,
    *,
    event_type: str,
    allow_unknown_event: bool = False,
) -> tuple[JsonObject, DataRepresentation]:
    if "data" not in payload:
        raise PushContractError("missing required field: data")
    raw_data = payload["data"]
    if isinstance(raw_data, dict):
        if event_type != "VitalSignsDataEvent":
            raise UnsupportedPushContractError(
                "object-form data is documented only for VitalSignsDataEvent"
            )
        return cast(JsonObject, raw_data), DataRepresentation.OBJECT_DOCUMENTED_EXAMPLE
    if not isinstance(raw_data, str):
        raise PushContractError("data must be an object or JSON object string")
    if raw_data == "":
        if (
            event_type not in {"ConnectedEvent", "DisconnectedEvent"}
            and not (allow_unknown_event and event_type not in SUPPORTED_EVENT_TYPES)
        ):
            raise PushContractError("empty data is valid only for connectivity events")
        return {}, DataRepresentation.EMPTY_STRING
    try:
        decoded = _strict_json_text(raw_data, context="data")
    except UnicodeEncodeError as exc:
        raise PushContractError("data contains invalid Unicode") from exc
    if not isinstance(decoded, dict):
        raise PushContractError("data JSON string must decode to an object")
    return cast(JsonObject, decoded), DataRepresentation.ESCAPED_JSON_STRING


def _strict_json_object(raw_body: bytes, *, context: str) -> JsonObject:
    try:
        text = raw_body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PushContractError(f"{context} is not valid UTF-8") from exc
    decoded = _strict_json_text(text, context=context)
    if not isinstance(decoded, dict):
        raise PushContractError(f"{context} must be a JSON object")
    return cast(JsonObject, decoded)


def _strict_json_text(text: str, *, context: str) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> JsonObject:
        result: JsonObject = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateJsonKeyError(f"duplicate JSON key in {context}: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise PushContractError(f"non-finite JSON number in {context}: {value}")

    try:
        return cast(
            object,
            json.loads(
                text,
                object_pairs_hook=object_pairs,
                parse_constant=reject_constant,
            ),
        )
    except json.JSONDecodeError as exc:
        raise PushContractError(f"malformed JSON in {context}") from exc


def _capture_field(payload: JsonObject, name: str) -> VendorField:
    if name not in payload:
        return VendorField(name=name, state=FieldState.ABSENT)
    value = payload[name]
    if value is None:
        return VendorField(name=name, state=FieldState.NULL)
    if value == "":
        return VendorField(name=name, state=FieldState.EMPTY, value="")
    return VendorField(name=name, state=FieldState.PRESENT, value=value)


def _capture_vital_onbed(payload: JsonObject) -> VendorField:
    """Accept the documented casing or the one recorded-real casing, never both."""

    if "Onbed" in payload and "OnBed" in payload:
        raise UnsupportedPushContractError(
            "ambiguous Vital bed-presence fields: Onbed and OnBed"
        )
    if "OnBed" in payload:
        return _capture_field(payload, "OnBed")
    return _capture_field(payload, "Onbed")


def _require_present_field(value: VendorField) -> None:
    if value.state != FieldState.PRESENT:
        raise PushContractError(f"missing or empty required event field: {value.name}")


def _require_nonempty_text(payload: JsonObject, name: str) -> str:
    if name not in payload or not isinstance(payload[name], str) or not payload[name]:
        raise PushContractError(f"missing or invalid text field: {name}")
    return cast(str, payload[name])


def _require_identifier(payload: JsonObject, name: str) -> str:
    if name not in payload:
        raise PushContractError(f"missing required identity field: {name}")
    return _identifier_text(payload[name], name)


def _identifier_text(value: object, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PushContractError(f"invalid identity field: {name}")
    text = str(value)
    if not text:
        raise PushContractError(f"empty identity field: {name}")
    return text


def _require_scalar(payload: JsonObject, name: str) -> object:
    if name not in payload:
        raise PushContractError(f"missing required field: {name}")
    value = payload[name]
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise PushContractError(f"invalid scalar field: {name}")
    if isinstance(value, str) and not value:
        raise PushContractError(f"empty scalar field: {name}")
    if isinstance(value, float) and not math.isfinite(value):
        raise PushContractError(f"non-finite scalar field: {name}")
    return value


def _validate_hmac_sha1_base64(value: str) -> None:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PushContractError("sign must be valid Base64") from exc
    if len(decoded) != hashlib.sha1().digest_size:
        raise PushContractError("sign must contain one HMAC-SHA1 digest")


def _reject_unknown_event_fields(data: JsonObject, allowed: set[str]) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise UnsupportedPushContractError(
            f"unsupported event data fields: {', '.join(unknown)}"
        )


def _normalize_numeric_field(
    value: VendorField,
    *,
    lower_exclusive: float,
    upper_inclusive: float,
) -> tuple[float | None, MissingState, str]:
    if value.state == FieldState.ABSENT:
        return None, MissingState.MISSING, f"{value.name}_absent"
    if value.state == FieldState.NULL:
        return None, MissingState.NOT_PROVIDED, f"{value.name}_null"
    if value.state == FieldState.EMPTY:
        return None, MissingState.INVALID, f"{value.name}_empty"
    raw = value.value
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, MissingState.INVALID, f"{value.name}_not_numeric"
    numeric = float(raw)
    if not math.isfinite(numeric):
        return None, MissingState.INVALID, f"{value.name}_not_finite"
    if numeric == -1:
        return None, MissingState.INVALID, f"{value.name}_invalid_minus_one"
    if numeric <= lower_exclusive or numeric > upper_inclusive:
        return None, MissingState.INVALID, f"{value.name}_out_of_range"
    return numeric, MissingState.PRESENT, ""


def _normalize_push_onbed(
    value: VendorField,
) -> tuple[BedPresenceState | None, MissingState, str]:
    if value.state == FieldState.ABSENT:
        return None, MissingState.MISSING, f"{value.name}_absent"
    if value.state == FieldState.NULL:
        return None, MissingState.NOT_PROVIDED, f"{value.name}_null"
    if value.state == FieldState.EMPTY:
        return None, MissingState.INVALID, f"{value.name}_empty"
    raw = value.value
    if raw is False or (isinstance(raw, int) and not isinstance(raw, bool) and raw == 0):
        return BedPresenceState.OUT_OF_BED, MissingState.PRESENT, ""
    if raw is True or (isinstance(raw, int) and not isinstance(raw, bool) and raw == 1):
        return BedPresenceState.IN_BED, MissingState.PRESENT, ""
    return (
        BedPresenceState.UNKNOWN,
        MissingState.UNKNOWN,
        f"push_{value.name}_unknown_value",
    )


def _timestamp_from_field(
    value: VendorField,
    *,
    unix_milliseconds: bool = False,
) -> tuple[datetime | None, TimezoneStatus, str | None]:
    if value.state in {FieldState.ABSENT, FieldState.NULL, FieldState.EMPTY}:
        return None, TimezoneStatus.NOT_PROVIDED, None
    if unix_milliseconds:
        return _parse_unix_milliseconds(value.value)
    return _parse_source_timestamp(value.value)


def _parse_unix_milliseconds(
    value: object,
) -> tuple[datetime | None, TimezoneStatus, str | None]:
    source_text = str(value)
    numeric: float | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped.replace(".", "", 1).isdigit():
            numeric = float(stripped)
    if numeric is None or not math.isfinite(numeric):
        return None, TimezoneStatus.TIMEZONE_UNKNOWN, source_text
    try:
        return (
            datetime.fromtimestamp(numeric / 1000.0, tz=timezone.utc),
            TimezoneStatus.KNOWN,
            source_text,
        )
    except (OSError, OverflowError, ValueError):
        return None, TimezoneStatus.TIMEZONE_UNKNOWN, source_text


def _parse_source_timestamp(
    value: object,
) -> tuple[datetime | None, TimezoneStatus, str | None]:
    source_text = str(value)
    numeric: float | None = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped.replace(".", "", 1).isdigit():
            numeric = float(stripped)
    if numeric is not None:
        try:
            seconds = numeric / 1000.0 if abs(numeric) >= 10_000_000_000 else numeric
            return (
                datetime.fromtimestamp(seconds, tz=timezone.utc),
                TimezoneStatus.KNOWN,
                source_text,
            )
        except (OSError, OverflowError, ValueError):
            return None, TimezoneStatus.TIMEZONE_UNKNOWN, source_text
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None, TimezoneStatus.TIMEZONE_UNKNOWN, source_text
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None, TimezoneStatus.TIMEZONE_UNKNOWN, source_text
        return parsed.astimezone(timezone.utc), TimezoneStatus.KNOWN, source_text
    return None, TimezoneStatus.TIMEZONE_UNKNOWN, source_text


def _field_text(value: VendorField) -> str | None:
    if value.state != FieldState.PRESENT:
        return None
    if isinstance(value.value, bool):
        return "true" if value.value else "false"
    if isinstance(value.value, (str, int, float)):
        return str(value.value)
    return None


def _required_field_text(value: VendorField) -> str:
    text = _field_text(value)
    if not text:
        raise PushContractError(f"required event field is not scalar: {value.name}")
    return text


def _event_alarm_id(event: VendorPushEvent) -> str | None:
    if isinstance(event, (AlarmEvent, AlarmStopEvent)):
        return _field_text(event.alarm_id)
    return None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "AlarmEvent",
    "AlarmStopEvent",
    "ConnectivityEvent",
    "DataRepresentation",
    "DuplicateJsonKeyError",
    "FieldState",
    "ParsedPushEnvelope",
    "PushContractError",
    "UnsupportedPushContractError",
    "UnknownPushEvent",
    "VendorField",
    "VitalSignsDataEvent",
    "normalize_push_envelope",
    "parse_push_envelope",
    "push_request_signed_at",
    "verify_push_envelope_signature",
]
