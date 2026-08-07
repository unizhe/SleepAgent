"""Deterministic Perceptor pull normalization over encrypted Raw Inbox records."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from sleepagent.sleep_domain.contracts import (
    AdapterDescriptor,
    AdapterObservationCandidate,
    AlgorithmVersionValue,
    AvailabilityState,
    BedExitKind,
    BedExitPayload,
    CalibrationValue,
    ConfidenceValue,
    HeartRatePayload,
    MissingIntervalPayload,
    MissingState,
    MovementPayload,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    ProviderDeviceIdentity,
    RespiratoryRatePayload,
    SleepStageIntervalPayload,
    SleepStageState,
    SourceKind,
    TimezoneStatus,
    VendorSleepProfileMetricPayload,
)
from sleepagent.sleep_domain.registry import AdapterInputEnvelope


PULL_ENVELOPE_SCHEMA = "perceptor_pull_envelope.v1"
SLEEP_REPORT_EVENT = "PerceptorSleepReportPull.v1"
HISTORY_EVENT = "PerceptorHistoryPull.v1"
REALTIME_FALLBACK_EVENT = "PerceptorRealtimeFallbackPull.v1"

_PROFILE_FIELDS: dict[str, str | None] = {
    "sleep_score": "score",
    "total_sleep_minutes": "minutes",
    "deep_sleep_minutes": "minutes",
    "light_sleep_minutes": "minutes",
    "rem_sleep_minutes": "minutes",
    "awake_minutes": "minutes",
    "sleep_efficiency": "percent",
    "movement_count": "count",
    "get_up_count": "count",
    "sleep_latency": "minutes",
}
_AMBIGUOUS_RANGE = re.compile(r"^\s*[+-]?\d+(?:\.\d+)?\s*-\s*[+-]?\d+(?:\.\d+)?\s*$")


def is_perceptor_pull_envelope(payload: Mapping[str, Any]) -> bool:
    return payload.get("schema") == PULL_ENVELOPE_SCHEMA


def normalize_perceptor_pull(
    adapter_input: AdapterInputEnvelope,
    descriptor: AdapterDescriptor,
    payload: Mapping[str, Any],
) -> Sequence[AdapterObservationCandidate]:
    event_type = payload.get("event_type")
    if event_type == SLEEP_REPORT_EVENT:
        return _normalize_sleep_report(adapter_input, descriptor, payload)
    if event_type == HISTORY_EVENT:
        return _normalize_history(adapter_input, descriptor, payload)
    if event_type == REALTIME_FALLBACK_EVENT:
        return _normalize_realtime_fallback(adapter_input, descriptor, payload)
    raise ValueError(f"unsupported Perceptor pull envelope: {event_type!r}")


def _normalize_history(
    adapter_input: AdapterInputEnvelope,
    descriptor: AdapterDescriptor,
    envelope: Mapping[str, Any],
) -> tuple[AdapterObservationCandidate, ...]:
    response_data = _response_data(envelope)
    records = _record_list(response_data)
    candidates: list[AdapterObservationCandidate] = []
    ordinal = 0
    cadence_seconds = float(envelope.get("cadence_seconds", 3.0))
    if cadence_seconds <= 0:
        raise ValueError("history cadence must be positive")
    for record in records:
        device = _provider_device(record, envelope)
        send_raw = _first(record, "send_time", "sendTime", "timestamp")
        send_at, timezone_status, source_text = _safe_aware_time(send_raw)
        heart = _series(record, "heart_rate", "heartRate", "heart_rates")
        respiratory = _series(
            record,
            "respiratory_rate",
            "respiratoryRate",
            "breath_rate",
            "breathRate",
        )
        movement = _series(
            record,
            "movement",
            "body_movement",
            "bodyMovement",
        )
        if not (len(heart) == len(respiratory) == len(movement)):
            raise ValueError("Perceptor history series lengths must be equal")
        for index, (heart_value, respiratory_value, movement_value) in enumerate(
            zip(heart, respiratory, movement, strict=True)
        ):
            reconstructed_at = (
                None
                if send_at is None
                else send_at
                - timedelta(seconds=(len(heart) - 1 - index) * cadence_seconds)
            )
            common = {
                "adapter_input": adapter_input,
                "descriptor": descriptor,
                "provider_device": device,
                "measurement_at": reconstructed_at,
                "timezone_status": timezone_status,
                "source_timestamp_text": source_text,
                "processing_steps": (
                    "perceptor_history_parse.v1",
                    "reconstruct_backward_from_send_time.v1",
                ),
                "quality_flags": ("reconstructed_time",),
                "limitations": (
                    f"documented_nominal_cadence_seconds:{cadence_seconds:g}",
                ),
            }
            candidates.append(
                _numeric_or_missing_candidate(
                    **common,
                    observation_type=ObservationType.HEART_RATE,
                    raw_value=heart_value,
                    suffix=f"history:{ordinal}:heart_rate",
                )
            )
            ordinal += 1
            candidates.append(
                _numeric_or_missing_candidate(
                    **common,
                    observation_type=ObservationType.RESPIRATORY_RATE,
                    raw_value=respiratory_value,
                    suffix=f"history:{ordinal}:respiratory_rate_series",
                    extra_limitations=(
                        "respiratory_rate_series_not_raw_waveform",
                        "not_signal_artifact_or_ressleepnet_input",
                    ),
                    producer_name="respiratory_rate_series",
                )
            )
            ordinal += 1
            candidates.append(
                _numeric_or_missing_candidate(
                    **common,
                    observation_type=ObservationType.MOVEMENT,
                    raw_value=movement_value,
                    suffix=f"history:{ordinal}:movement",
                )
            )
            ordinal += 1
    return tuple(candidates)


def _normalize_sleep_report(
    adapter_input: AdapterInputEnvelope,
    descriptor: AdapterDescriptor,
    envelope: Mapping[str, Any],
) -> tuple[AdapterObservationCandidate, ...]:
    report = _response_data(envelope)
    if report in (None, {}, []):
        return ()
    if not isinstance(report, Mapping):
        raise ValueError("Perceptor sleep report data must be an object")
    device = _provider_device(report, envelope)
    candidates: list[AdapterObservationCandidate] = []
    ordinal = 0
    stage_starts: list[datetime] = []

    for stage in _entry_list(
        report,
        "sleep_stages",
        "sleepStages",
        "stage_intervals",
        "stageIntervals",
    ):
        start_raw = _first(stage, "start_time", "startTime", "start_at")
        end_raw = _first(stage, "end_time", "endTime", "end_at")
        start, start_status, start_text = _safe_aware_time(start_raw)
        end, end_status, end_text = _safe_aware_time(end_raw)
        source_start = _parse_source_time_text(start_raw)
        source_end = _parse_source_time_text(end_raw)
        if start is None and source_start is None:
            raise ValueError("sleep stage interval timestamps are unparseable")
        if end is None and source_end is None:
            raise ValueError("sleep stage interval timestamps are unparseable")
        if start is not None:
            stage_starts.append(start)
        known_timezone = (
            start_status == TimezoneStatus.KNOWN
            and end_status == TimezoneStatus.KNOWN
        )
        candidates.append(
            _candidate(
                adapter_input=adapter_input,
                descriptor=descriptor,
                provider_device=device,
                observation_type=ObservationType.SLEEP_STAGE_INTERVAL,
                payload=SleepStageIntervalPayload(
                    stage=_sleep_stage(_first(stage, "stage", "stage_name", "name")),
                    start_at=start if known_timezone else None,
                    end_at=end if known_timezone else None,
                    source_start_text=(
                        None if known_timezone else source_start
                    ),
                    source_end_text=(
                        None if known_timezone else source_end
                    ),
                ),
                measurement_at=start if known_timezone else None,
                timezone_status=(
                    TimezoneStatus.KNOWN
                    if known_timezone
                    else TimezoneStatus.TIMEZONE_UNKNOWN
                ),
                source_timestamp_text=f"{start_text or start_raw}|{end_text or end_raw}",
                suffix=f"report:{ordinal}:sleep_stage",
                processing_steps=("perceptor_sleep_report_parse.v1",),
            )
        )
        ordinal += 1

    series_specs = (
        (
            ("minute_heart_rates", "minuteHeartRates"),
            ObservationType.HEART_RATE,
            "minute_heart_rate",
        ),
        (
            ("minute_respiratory_rates", "minuteRespiratoryRates"),
            ObservationType.RESPIRATORY_RATE,
            "minute_respiratory_rate",
        ),
        (
            ("body_movements", "bodyMovements", "movement_entries"),
            ObservationType.MOVEMENT,
            "body_movement",
        ),
    )
    for aliases, observation_type, label in series_specs:
        for entry in _entry_list(report, *aliases):
            time_raw = _first(entry, "time", "timestamp", "measurement_at")
            measured_at, status, source_text = _safe_aware_time(time_raw)
            limitations = (
                (
                    "respiratory_rate_series_not_raw_waveform",
                    "not_signal_artifact_or_ressleepnet_input",
                )
                if observation_type == ObservationType.RESPIRATORY_RATE
                else ()
            )
            candidates.append(
                _numeric_or_missing_candidate(
                    adapter_input=adapter_input,
                    descriptor=descriptor,
                    provider_device=device,
                    observation_type=observation_type,
                    raw_value=_first(entry, "value", "rate", "count", "index"),
                    measurement_at=measured_at,
                    timezone_status=status,
                    source_timestamp_text=source_text,
                    suffix=f"report:{ordinal}:{label}",
                    processing_steps=("perceptor_sleep_report_parse.v1",),
                    limitations=(),
                    quality_flags=(),
                    extra_limitations=limitations,
                    producer_name=(
                        "respiratory_rate_series"
                        if observation_type == ObservationType.RESPIRATORY_RATE
                        else None
                    ),
                )
            )
            ordinal += 1

    for entry in _entry_list(report, "get_up_events", "getUpEvents", "bed_exits"):
        time_raw = _first(entry, "time", "timestamp", "event_time")
        occurred_at, status, source_text = _safe_aware_time(time_raw)
        candidates.append(
            _candidate(
                adapter_input=adapter_input,
                descriptor=descriptor,
                provider_device=device,
                observation_type=ObservationType.BED_EXIT,
                payload=BedExitPayload(kind=BedExitKind.GET_UP),
                measurement_at=occurred_at,
                timezone_status=status,
                source_timestamp_text=source_text,
                suffix=f"report:{ordinal}:get_up",
                processing_steps=("perceptor_sleep_report_parse.v1",),
            )
        )
        ordinal += 1

    report_time_raw = _first(
        report,
        "report_time",
        "reportTime",
        "generated_at",
        "generatedAt",
    )
    report_time, report_status, report_source_text = _safe_aware_time(report_time_raw)
    if report_time is None and stage_starts:
        report_time = min(stage_starts)
        report_status = TimezoneStatus.KNOWN
        report_source_text = "derived_from_earliest_aware_stage_start"
    profile = report.get("profile")
    profile_values = profile if isinstance(profile, Mapping) else report
    for metric_name, unit in _PROFILE_FIELDS.items():
        if metric_name not in profile_values:
            continue
        raw_value = profile_values[metric_name]
        ambiguous = isinstance(raw_value, str) and bool(
            _AMBIGUOUS_RANGE.fullmatch(raw_value)
        )
        known = (
            not ambiguous
            and isinstance(raw_value, (int, float, bool))
            and not isinstance(raw_value, complex)
        )
        payload = VendorSleepProfileMetricPayload(
            metric_name=metric_name,
            value_state=(
                AvailabilityState.KNOWN
                if known
                else AvailabilityState.UNKNOWN
            ),
            value=raw_value if known else None,
            source_text=None if known else str(raw_value),
            unit=unit,
        )
        candidates.append(
            _candidate(
                adapter_input=adapter_input,
                descriptor=descriptor,
                provider_device=device,
                observation_type=ObservationType.VENDOR_SLEEP_PROFILE_METRIC,
                payload=payload,
                measurement_at=report_time,
                timezone_status=report_status,
                source_timestamp_text=report_source_text,
                suffix=f"report:{ordinal}:profile:{metric_name}",
                processing_steps=("perceptor_sleep_report_parse.v1",),
                limitations=(
                    ("ambiguous_profile_source_text_not_interpreted",)
                    if ambiguous
                    else ()
                ),
            )
        )
        ordinal += 1
    return tuple(candidates)


def _normalize_realtime_fallback(
    adapter_input: AdapterInputEnvelope,
    descriptor: AdapterDescriptor,
    envelope: Mapping[str, Any],
) -> tuple[AdapterObservationCandidate, ...]:
    data = _response_data(envelope)
    if not isinstance(data, Mapping):
        return ()
    device = _provider_device(data, envelope)
    time_raw = _first(data, "DateTime", "dateTime", "timestamp", "time")
    measured_at, status, source_text = _safe_aware_time(time_raw)
    candidates: list[AdapterObservationCandidate] = []
    specs = (
        (ObservationType.HEART_RATE, ("heart_rate", "heartRate"), "heart_rate"),
        (
            ObservationType.RESPIRATORY_RATE,
            ("respiratory_rate", "respiratoryRate", "breath_rate"),
            "respiratory_rate_series",
        ),
        (ObservationType.MOVEMENT, ("movement", "bodyMovement"), "movement"),
    )
    for observation_type, aliases, suffix in specs:
        raw_value = _first(data, *aliases)
        if raw_value is None:
            continue
        candidates.append(
            _numeric_or_missing_candidate(
                adapter_input=adapter_input,
                descriptor=descriptor,
                provider_device=device,
                observation_type=observation_type,
                raw_value=raw_value,
                measurement_at=measured_at,
                timezone_status=status,
                source_timestamp_text=source_text,
                suffix=f"fallback:{suffix}",
                processing_steps=("perceptor_configured_realtime_fallback.v1",),
                quality_flags=(),
                limitations=("configured_gap_fallback_not_parallel_truth_source",),
                extra_limitations=(
                    ("respiratory_rate_series_not_raw_waveform",)
                    if observation_type == ObservationType.RESPIRATORY_RATE
                    else ()
                ),
                producer_name=(
                    "respiratory_rate_series"
                    if observation_type == ObservationType.RESPIRATORY_RATE
                    else None
                ),
            )
        )
    return tuple(candidates)


def _numeric_or_missing_candidate(
    *,
    adapter_input: AdapterInputEnvelope,
    descriptor: AdapterDescriptor,
    provider_device: ProviderDeviceIdentity,
    observation_type: ObservationType,
    raw_value: Any,
    measurement_at: datetime | None,
    timezone_status: TimezoneStatus,
    source_timestamp_text: str | None,
    suffix: str,
    processing_steps: tuple[str, ...],
    quality_flags: tuple[str, ...],
    limitations: tuple[str, ...],
    extra_limitations: tuple[str, ...] = (),
    producer_name: str | None = None,
) -> AdapterObservationCandidate:
    invalid_reason: str | None = None
    if isinstance(raw_value, bool):
        numeric = 0.0
        invalid_reason = "perceptor_invalid_non_numeric_value"
    else:
        try:
            numeric = float(raw_value)
        except (TypeError, ValueError):
            numeric = 0.0
            invalid_reason = "perceptor_invalid_non_numeric_value"
    if not math.isfinite(numeric):
        invalid_reason = "perceptor_invalid_non_finite_value"
    if numeric == -1:
        invalid_reason = "perceptor_sentinel_minus_one"
    if invalid_reason is not None:
        payload: Any = MissingIntervalPayload(
            target_observation_type=observation_type,
            missing_state=MissingState.INVALID,
            reason_code=invalid_reason,
            interval_start_at=measurement_at,
        )
        envelope_type = ObservationType.MISSING_INTERVAL
        missing_state = MissingState.INVALID
    else:
        payload = {
            ObservationType.HEART_RATE: HeartRatePayload,
            ObservationType.RESPIRATORY_RATE: RespiratoryRatePayload,
            ObservationType.MOVEMENT: MovementPayload,
        }[observation_type](value=numeric)
        envelope_type = observation_type
        missing_state = MissingState.PRESENT
    return _candidate(
        adapter_input=adapter_input,
        descriptor=descriptor,
        provider_device=provider_device,
        observation_type=envelope_type,
        payload=payload,
        measurement_at=measurement_at,
        timezone_status=timezone_status,
        source_timestamp_text=source_timestamp_text,
        suffix=suffix,
        processing_steps=processing_steps,
        quality_flags=quality_flags,
        limitations=limitations + extra_limitations,
        missing_state=missing_state,
        producer_name=producer_name,
    )


def _candidate(
    *,
    adapter_input: AdapterInputEnvelope,
    descriptor: AdapterDescriptor,
    provider_device: ProviderDeviceIdentity,
    observation_type: ObservationType,
    payload: Any,
    measurement_at: datetime | None,
    timezone_status: TimezoneStatus,
    source_timestamp_text: str | None,
    suffix: str,
    processing_steps: tuple[str, ...],
    quality_flags: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
    missing_state: MissingState = MissingState.PRESENT,
    producer_name: str | None = None,
) -> AdapterObservationCandidate:
    raw = adapter_input.raw_record
    identity = hashlib.sha256(
        (
            f"{raw.raw_ingress_record_id}|{suffix}|{descriptor.adapter_version}"
        ).encode("utf-8")
    ).hexdigest()
    return AdapterObservationCandidate(
        candidate_id=f"perceptor:{identity}",
        data_mode=raw.data_mode,
        observation_type=observation_type,
        payload=payload,
        source_kind=SourceKind.VENDOR_DERIVED,
        provider_id=raw.provider_id,
        provider_account_id=raw.provider_account_id,
        provider_device=provider_device,
        request_signed_at=raw.request_signed_at,
        measurement_at=measurement_at,
        received_at=raw.received_at,
        source_timestamp_text=source_timestamp_text,
        timezone_status=timezone_status,
        quality=ObservationQuality(
            missing_state=missing_state,
            confidence=ConfidenceValue(state=AvailabilityState.UNKNOWN),
            algorithm_version=AlgorithmVersionValue(state=AvailabilityState.UNKNOWN),
            calibration=CalibrationValue(state=AvailabilityState.NOT_PROVIDED),
            quality_flags=quality_flags,
            processing_steps=processing_steps,
            limitations=limitations,
        ),
        provenance=ObservationProvenance(
            provider_id=raw.provider_id,
            provider_account_id=raw.provider_account_id,
            adapter_id=descriptor.adapter_id,
            adapter_version=descriptor.adapter_version,
            raw_ingress_record_id=raw.raw_ingress_record_id,
            raw_payload_sha256=raw.pre_normalization_payload_sha256,
            source_record_id=raw.message_id,
            source_idempotency_key=raw.idempotency_identity,
            producer_name=producer_name or "perceptor_pull_adapter",
        ),
        source_key=f"perceptor:{raw.raw_ingress_record_id}:{suffix}",
        idempotency_key=(
            f"perceptor:{raw.idempotency_identity}:{suffix}:"
            f"{descriptor.adapter_version}"
        ),
    )


def _response_data(envelope: Mapping[str, Any]) -> Any:
    response = envelope.get("response")
    if not isinstance(response, Mapping):
        raise ValueError("Perceptor pull envelope response must be an object")
    if str(response.get("code")) != "200" or response.get("success") is not True:
        raise ValueError("Perceptor pull response is not successful")
    return response.get("data")


def _record_list(data: Any) -> list[Mapping[str, Any]]:
    if data in (None, {}, []):
        return []
    if isinstance(data, list):
        records = data
    elif isinstance(data, Mapping):
        nested = _first(data, "records", "list", "items", "history")
        records = nested if isinstance(nested, list) else [data]
    else:
        raise ValueError("Perceptor history data must contain records")
    if not all(isinstance(item, Mapping) for item in records):
        raise ValueError("Perceptor history record must be an object")
    return list(records)


def _entry_list(payload: Mapping[str, Any], *keys: str) -> list[Mapping[str, Any]]:
    value = _first(payload, *keys)
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, Mapping) for item in value
    ):
        raise ValueError(f"{keys[0]} must be a list of objects")
    return list(value)


def _series(payload: Mapping[str, Any], *keys: str) -> list[Any]:
    value = _first(payload, *keys)
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",")]
    if isinstance(value, list):
        return value
    raise ValueError(f"{keys[0]} must be a comma-separated string or list")


def _provider_device(
    payload: Mapping[str, Any],
    envelope: Mapping[str, Any],
) -> ProviderDeviceIdentity:
    request = envelope.get("request")
    request = request if isinstance(request, Mapping) else {}
    device_name = _first(payload, "device_name", "deviceName") or _first(
        request, "device_name", "deviceName"
    )
    identity_request: Mapping[str, Any] = request
    requested_devices = request.get("devices")
    if isinstance(requested_devices, list) and device_name is not None:
        matched = [
            item
            for item in requested_devices
            if isinstance(item, Mapping)
            and str(_first(item, "device_name", "deviceName")) == str(device_name)
        ]
        if len(matched) == 1:
            identity_request = matched[0]
    device_id = _first(payload, "device_id", "deviceId") or _first(
        identity_request, "device_id", "deviceId"
    )
    if device_name is None and device_id is None:
        names = request.get("device_names")
        if isinstance(names, list) and len(names) == 1:
            device_name = names[0]
    return ProviderDeviceIdentity(
        provider_device_id=None if device_id is None else str(device_id),
        provider_device_name=None if device_name is None else str(device_name),
        product_id=(
            None
            if _first(identity_request, "product_id", "productId") is None
            else str(_first(identity_request, "product_id", "productId"))
        ),
        home_id=(
            None
            if _first(identity_request, "home_id", "homeId") is None
            else str(_first(identity_request, "home_id", "homeId"))
        ),
        project_id=(
            None
            if _first(identity_request, "project_id", "projectId") is None
            else str(_first(identity_request, "project_id", "projectId"))
        ),
    )


def _safe_aware_time(
    value: Any,
) -> tuple[datetime | None, TimezoneStatus, str | None]:
    if value is None or value == "":
        return None, TimezoneStatus.NOT_PROVIDED, None
    source_text = str(value)
    try:
        if isinstance(value, (int, float)) or (
            isinstance(value, str) and value.strip().isdigit()
        ):
            numeric = float(value)
            if numeric > 10_000_000_000:
                numeric /= 1000
            return (
                datetime.fromtimestamp(numeric, tz=timezone.utc),
                TimezoneStatus.KNOWN,
                source_text,
            )
        parsed = datetime.fromisoformat(source_text.strip().replace("Z", "+00:00"))
    except (OSError, OverflowError, ValueError):
        return None, TimezoneStatus.TIMEZONE_UNKNOWN, source_text
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, TimezoneStatus.TIMEZONE_UNKNOWN, source_text
    return parsed, TimezoneStatus.KNOWN, source_text


def _parse_source_time_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return value


def _sleep_stage(value: Any) -> SleepStageState:
    normalized = str(value or "").strip().lower()
    return {
        "deep": SleepStageState.DEEP,
        "light": SleepStageState.LIGHT,
        "rem": SleepStageState.REM,
        "awake": SleepStageState.AWAKE,
        "wake": SleepStageState.AWAKE,
    }.get(normalized, SleepStageState.UNKNOWN)


def _first(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def canonical_content_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "HISTORY_EVENT",
    "PULL_ENVELOPE_SCHEMA",
    "REALTIME_FALLBACK_EVENT",
    "SLEEP_REPORT_EVENT",
    "canonical_content_sha256",
    "is_perceptor_pull_envelope",
    "normalize_perceptor_pull",
]
