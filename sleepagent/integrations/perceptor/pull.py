"""Deterministic YunYun V2.5.2 Pull normalization.

The functions in this module are deliberately side-effect free.  Exact vendor
responses remain in the caller-owned private evidence store; only typed,
provider-neutral candidates cross this boundary.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sleepagent.domain.contracts import (
    AdapterObservationCandidate,
    AlgorithmVersionValue,
    AvailabilityState,
    BedExitKind,
    BedExitPayload,
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
    SleepStageIntervalPayload,
    SleepStageState,
    SourceKind,
    TimezoneStatus,
    VendorSleepProfileMetricPayload,
)
from sleepagent.domain.canonical_observation import (
    CanonicalObservationFactoryV2,
    CanonicalObservationV2,
)
from sleepagent.domain.observation_semantics import (
    MovementMetricId,
    MovementPayloadV2,
)


UTC = timezone.utc
PROVIDER_ID = "perceptor"
ADAPTER_ID = "perceptor-pull"
ADAPTER_VERSION = "p4-d2-b1.v1"
PARSER_VERSION = "perceptor-pull-parser.v1"
NORMALIZER_VERSION = "perceptor-pull-normalizer.v1"
HISTORY_CADENCE = timedelta(seconds=3)
HISTORY_RECONSTRUCTION_FLAGS = (
    "timestamp_origin_reconstructed",
    "perceptor_history_3s.v1",
    "documented_history_3_second_reconstruction",
)
HISTORY_RECONSTRUCTION_LIMITATIONS = (
    "measurement_time_reconstructed_not_vendor_emitted",
)
SLEEP_REPORT_SERIES_FIELDS = (
    "sleep_stage_list",
    "heart_rate_data",
    "breathe_data",
    "body_shake_data",
    "getups",
    "apnea_images",
    "apnea_data",
    "breath_pause_data",
)
SLEEP_REPORT_SUMMARY_FIELDS = (
    "heart_rate_avg",
    "breathe_avg",
    "sum_body_shake_times",
    "apnea_count",
)
SLEEP_REPORT_PROFILE_FIELDS = frozenset(
    {
        "deep_sleep_rate",
        "in_bed_time",
        "in_sleep_time",
        "leave_bed_time",
        "sleep_duration",
        "sleep_efficiency",
        "sleep_time",
        "wake_ups",
        "wakeup_time",
    }
)
SLEEP_REPORT_TRUSTED_PROFILE_FIELDS = frozenset(
    {"deep_sleep_rate", "sleep_efficiency"}
)
SLEEP_REPORT_UNSUPPORTED_TOP_LEVEL_FIELDS = frozenset(
    {"apnea_images", "apnea_data", "breath_pause_data", "apnea_count"}
)
SLEEP_REPORT_NO_DATA_REQUIRED_FIELDS = frozenset(
    {
        "sleep_profile",
        "sleep_stage_list",
        "heart_rate_data",
        "heart_rate_avg",
        "breathe_data",
        "breathe_avg",
        "body_shake_data",
        "sum_body_shake_times",
        "getups",
    }
)
SLEEP_REPORT_KNOWN_FIELDS = frozenset(
    {"sleep_profile", *SLEEP_REPORT_SERIES_FIELDS, *SLEEP_REPORT_SUMMARY_FIELDS}
)


class PullContractError(ValueError):
    """A real response cannot safely cross the normalization boundary."""


@dataclass(frozen=True, slots=True)
class PullNormalizationResult:
    candidates: tuple[AdapterObservationCandidate, ...]
    unknown_fields: tuple[str, ...] = ()
    intentionally_unsupported_fields: tuple[str, ...] = ()
    intentionally_ignored_fields: tuple[str, ...] = ()
    parser_notes: tuple[str, ...] = ()
    history_window_classification: HistoryWindowClassification | None = None


@dataclass(frozen=True, slots=True)
class HistoryWindowClassification:
    """Audit-only classification of reconstructed history candidates."""

    requested_start_at: datetime
    requested_end_at: datetime
    earliest_reconstructed_at: datetime
    latest_reconstructed_at: datetime
    before_window_candidate_count: int
    in_window_candidate_count: int
    after_window_candidate_count: int


def canonicalize_pull_result_v2(
    result: PullNormalizationResult,
    *,
    factory: CanonicalObservationFactoryV2 | None = None,
) -> tuple[CanonicalObservationV2, ...]:
    """Map proved Pull vendor meanings, then use the shared V2 authority."""

    authority = factory or CanonicalObservationFactoryV2()
    canonical: list[CanonicalObservationV2] = []
    for candidate in result.candidates:
        movement_payload = None
        if (
            candidate.observation_type is ObservationType.MOVEMENT
            and isinstance(candidate.payload, MovementPayload)
        ):
            if "vendor_report_hour_bucket" in candidate.quality.quality_flags:
                value = candidate.payload.value
                semantic_value: int | float = (
                    int(value) if value.is_integer() else value
                )
                start = candidate.measurement_at
                movement_payload = MovementPayloadV2(
                    metric_id=MovementMetricId.MOVEMENT_EVENT_COUNT,
                    value=semantic_value,
                    unit="count",
                    aggregation_start_at=start,
                    aggregation_end_at=(
                        None if start is None else start + timedelta(hours=1)
                    ),
                    vendor_semantic_code="perceptor.body_shake.hourly_count",
                )
            elif candidate.source_kind is SourceKind.DEVICE_MEASURED:
                movement_payload = MovementPayloadV2(
                    metric_id=MovementMetricId.MOVEMENT_INDEX,
                    value=candidate.payload.value,
                    unit="vendor_index",
                    vendor_semantic_code="perceptor.body_shake.index",
                )
            else:
                movement_payload = MovementPayloadV2(
                    metric_id=MovementMetricId.LEGACY_AMBIGUOUS_MOVEMENT,
                    value=candidate.payload.value,
                    unit="legacy_unknown",
                    vendor_semantic_code="body_shake_data.time_long.value",
                )
        canonical.append(
            authority.build(
                candidate=candidate,
                movement_payload=movement_payload,
                normalizer_version=NORMALIZER_VERSION,
            )
        )
    return tuple(canonical)


def with_durable_raw_reference(
    result: PullNormalizationResult,
    raw_ingress_record_id: str,
) -> PullNormalizationResult:
    """Return a copy whose candidates reference the committed raw record."""

    if not isinstance(raw_ingress_record_id, str) or not raw_ingress_record_id.strip():
        raise PullContractError("raw_ingress_record_id must be non-empty")
    candidates = tuple(
        candidate.model_copy(
            update={
                "provenance": candidate.provenance.model_copy(
                    update={"raw_ingress_record_id": raw_ingress_record_id}
                )
            }
        )
        for candidate in result.candidates
    )
    return PullNormalizationResult(
        candidates=candidates,
        unknown_fields=result.unknown_fields,
        intentionally_unsupported_fields=result.intentionally_unsupported_fields,
        intentionally_ignored_fields=result.intentionally_ignored_fields,
        parser_notes=result.parser_notes,
        history_window_classification=result.history_window_classification,
    )


def assert_requested_device_matches_binding(
    requested_device_name: str,
    provider_device: ProviderDeviceIdentity,
) -> None:
    """Fail closed before a request whose name is not in the bound identity."""

    if requested_device_name != provider_device.provider_device_name:
        raise PullContractError("requested device does not match DeviceBinding")


def normalize_current(
    data: Mapping[str, Any],
    *,
    provider_account_id: str,
    provider_device: ProviderDeviceIdentity,
    raw_sha256: str,
    requested_at: datetime,
    received_at: datetime,
) -> PullNormalizationResult:
    _validate_context(raw_sha256, requested_at, received_at)
    known = {
        "deviceState", "guardDays", "protectLength", "probStatus", "probData",
        "alarmLog", "keepTime", "showSleep", "smbdFlag",
        "device_state", "guard_days", "protect_length", "prob_status", "prob_data",
        "alarm_log", "keep_time", "show_sleep", "smbd_flag",
    }
    candidates: list[AdapterObservationCandidate] = []
    device_state = _alias(data, "deviceState", "device_state")
    if device_state is not None:
        source = _required_scalar_text(device_state, "device_state")
        normalized = source.lower()
        if normalized in {"online", "alarm-on", "weakalarm-on"}:
            state = DeviceConnectivityState.ONLINE
        elif normalized in {"offline", "alarm-off", "weakalarm-off"}:
            state = DeviceConnectivityState.OFFLINE
        else:
            state = DeviceConnectivityState.UNKNOWN
        candidates.append(
            _candidate(
                endpoint="getCurrent", suffix="connectivity", payload=DeviceConnectivityPayload(
                    state=state, vendor_status_code=source
                ), source_kind=SourceKind.DEVICE_MEASURED,
                provider_account_id=provider_account_id, provider_device=provider_device,
                raw_sha256=raw_sha256, requested_at=requested_at,
                received_at=received_at, measurement_at=received_at,
                timezone_status=TimezoneStatus.NOT_PROVIDED,
                quality_flags=("vendor_device_state",),
                limitations=("measurement_time_uses_response_receipt",),
            )
        )
    smbd_flag = _alias(data, "smbdFlag", "smbd_flag")
    if smbd_flag is not None:
        code = _integer(smbd_flag, "smbd_flag")
        state = {
            1: BedPresenceState.IN_BED,
            2: BedPresenceState.OUT_OF_BED,
            3: BedPresenceState.UNKNOWN,
        }.get(code, BedPresenceState.UNKNOWN)
        candidates.append(
            _candidate(
                endpoint="getCurrent", suffix="bed-presence", payload=BedPresencePayload(state=state),
                source_kind=SourceKind.VENDOR_DERIVED,
                provider_account_id=provider_account_id, provider_device=provider_device,
                raw_sha256=raw_sha256, requested_at=requested_at,
                received_at=received_at, measurement_at=received_at,
                timezone_status=TimezoneStatus.NOT_PROVIDED,
                quality_flags=("vendor_smbd_flag", f"vendor_smbd_flag_{code}"),
                limitations=("measurement_time_uses_response_receipt",),
            )
        )
    notes = tuple(
        note for field, note in (
            ("probStatus", "probStatus_preserved_as_vendor_evidence_not_clinical_truth"),
            ("prob_status", "prob_status_preserved_as_vendor_evidence_not_clinical_truth"),
            ("probData", "probData_preserved_private_not_promoted"),
            ("prob_data", "prob_data_preserved_private_not_promoted"),
            ("alarmLog", "alarmLog_preserved_private_not_promoted"),
            ("alarm_log", "alarm_log_preserved_private_not_promoted"),
        ) if field in data
    )
    return PullNormalizationResult(
        candidates=tuple(candidates),
        unknown_fields=tuple(sorted(set(data) - known)),
        parser_notes=notes,
    )


def normalize_realtime(
    data: Mapping[str, Any],
    *,
    provider_account_id: str,
    provider_device: ProviderDeviceIdentity,
    raw_sha256: str,
    requested_at: datetime,
    received_at: datetime,
    binding_timezone_name: str,
) -> PullNormalizationResult:
    _validate_context(raw_sha256, requested_at, received_at)
    known = {"endTime", "HeartRate", "BreathRate", "probStatus", "keepTime", "time", "timeFormat"}
    measured_at, source_text, tz_status, time_flags = _realtime_time(
        data, received_at=received_at, timezone_name=binding_timezone_name
    )
    if data.get("endTime") not in (None, ""):
        session_end = _epoch_time(
            data["endTime"], "endTime", unit="milliseconds"
        )
        if measured_at > session_end:
            raise PullContractError("realtime sample exceeds vendor session end")
    candidates: list[AdapterObservationCandidate] = []
    for field, observation_type, payload_type, upper, unit_suffix in (
        ("HeartRate", ObservationType.HEART_RATE, HeartRatePayload, 300.0, "heart-rate"),
        ("BreathRate", ObservationType.RESPIRATORY_RATE, RespiratoryRatePayload, 150.0, "breath-rate"),
    ):
        if field not in data:
            continue
        candidates.append(
            _measurement_candidate(
                value=data[field], field=field, observation_type=observation_type,
                payload_type=payload_type, upper=upper, endpoint="getRealTimes",
                suffix=unit_suffix, provider_account_id=provider_account_id,
                provider_device=provider_device, raw_sha256=raw_sha256,
                requested_at=requested_at, received_at=received_at,
                measurement_at=measured_at, source_timestamp_text=source_text,
                timezone_status=tz_status, time_flags=time_flags,
            )
        )
    if "probStatus" in data:
        status = _integer(data["probStatus"], "probStatus")
        state = {
            5: BedPresenceState.IN_BED,
            6: BedPresenceState.OUT_OF_BED,
        }.get(status, BedPresenceState.UNKNOWN)
        candidates.append(
            _candidate(
                endpoint="getRealTimes", suffix="bed-presence", payload=BedPresencePayload(state=state),
                source_kind=SourceKind.VENDOR_DERIVED,
                provider_account_id=provider_account_id, provider_device=provider_device,
                raw_sha256=raw_sha256, requested_at=requested_at, received_at=received_at,
                measurement_at=measured_at, source_timestamp_text=source_text,
                timezone_status=tz_status,
                quality_flags=("vendor_prob_status", f"vendor_prob_status_{status}") + time_flags,
                limitations=("vendor_status_not_clinical_truth",),
            )
        )
    return PullNormalizationResult(
        candidates=tuple(candidates), unknown_fields=tuple(sorted(set(data) - known))
    )


def validate_realtime_start(data: Mapping[str, Any]) -> datetime | None:
    """Validate an optional end marker from a successful start envelope.

    The real tenant returned an empty data object with vendor success.  The
    caller therefore remains responsible for a stricter local sample/time
    bound when no vendor end marker is supplied.
    """

    if not data:
        return None
    return _epoch_time(data.get("end_time"), "end_time", unit="seconds")


def normalize_history(
    records: Sequence[Mapping[str, Any]],
    *,
    provider_account_id: str,
    provider_device: ProviderDeviceIdentity,
    raw_sha256: str,
    requested_at: datetime,
    received_at: datetime,
    binding_timezone_name: str,
    requested_window_start: datetime | None = None,
    requested_window_end: datetime | None = None,
) -> PullNormalizationResult:
    _validate_context(raw_sha256, requested_at, received_at)
    if (requested_window_start is None) != (requested_window_end is None):
        raise PullContractError("history window requires both boundaries")
    if requested_window_start is not None and requested_window_end is not None:
        _require_aware(requested_window_start, "requested_window_start")
        _require_aware(requested_window_end, "requested_window_end")
        if requested_window_end <= requested_window_start:
            raise PullContractError("history window must have positive duration")
    known = {"device_id", "heart_rate", "body_shake", "breath_rate", "create_time", "send_time"}
    series_contract = (
        ("heart_rate", ObservationType.HEART_RATE, HeartRatePayload, 300.0, "heart-rate"),
        ("breath_rate", ObservationType.RESPIRATORY_RATE, RespiratoryRatePayload, 150.0, "breath-rate"),
        ("body_shake", ObservationType.MOVEMENT, MovementPayload, None, "movement"),
    )
    candidates: list[AdapterObservationCandidate] = []
    unknown: set[str] = set()
    zone = ZoneInfo(binding_timezone_name)
    validated_records: list[
        tuple[
            int,
            str,
            datetime,
            tuple[tuple[str, ...], ...],
        ]
    ] = []
    for record_index, record in enumerate(records):
        unknown.update(set(record) - known)
        response_device = _required_scalar_text(record.get("device_id"), "device_id")
        if response_device not in {
            provider_device.provider_device_id,
            provider_device.provider_device_name,
        }:
            raise PullContractError("history response device does not match DeviceBinding")
        source_send_time = _required_scalar_text(record.get("send_time"), "send_time")
        send_time = _parse_naive_local(source_send_time, zone, "send_time")
        compact_series = tuple(
            _compact_series(record.get(field), field)
            for field, _observation_type, _payload_type, _upper, _suffix
            in series_contract
        )
        if len({len(values) for values in compact_series}) != 1:
            raise PullContractError("history compact series lengths must align")
        validated_records.append(
            (record_index, source_send_time, send_time, compact_series)
        )

    for record_index, source_send_time, send_time, compact_series in validated_records:
        for (
            field,
            observation_type,
            payload_type,
            upper,
            suffix,
        ), values in zip(series_contract, compact_series, strict=True):
            for value_index, value in enumerate(values):
                offset = len(values) - value_index - 1
                measured_at = send_time - HISTORY_CADENCE * offset
                candidates.append(
                    _measurement_candidate(
                        value=value, field=field, observation_type=observation_type,
                        payload_type=payload_type, upper=upper, endpoint="getHistoryData",
                        suffix=f"r{record_index}-{suffix}-{value_index}",
                        provider_account_id=provider_account_id, provider_device=provider_device,
                        raw_sha256=raw_sha256, requested_at=requested_at,
                        received_at=received_at, measurement_at=measured_at,
                        source_timestamp_text=source_send_time,
                        timezone_status=TimezoneStatus.NORMALIZED_FROM_BINDING,
                        time_flags=HISTORY_RECONSTRUCTION_FLAGS,
                        limitations=HISTORY_RECONSTRUCTION_LIMITATIONS,
                        allow_zero=observation_type == ObservationType.MOVEMENT,
                    )
                )
    classification = None
    if requested_window_start is not None and requested_window_end is not None:
        before = tuple(
            candidate
            for candidate in candidates
            if candidate.measurement_at is not None
            and candidate.measurement_at < requested_window_start
        )
        inside = tuple(
            candidate
            for candidate in candidates
            if candidate.measurement_at is not None
            and requested_window_start
            <= candidate.measurement_at
            <= requested_window_end
        )
        after = tuple(
            candidate
            for candidate in candidates
            if candidate.measurement_at is None
            or candidate.measurement_at > requested_window_end
        )
        reconstructed = tuple(
            candidate.measurement_at
            for candidate in candidates
            if candidate.measurement_at is not None
        )
        if not reconstructed:
            raise PullContractError("history response has no reconstructed timestamps")
        classification = HistoryWindowClassification(
            requested_start_at=requested_window_start,
            requested_end_at=requested_window_end,
            earliest_reconstructed_at=min(reconstructed),
            latest_reconstructed_at=max(reconstructed),
            before_window_candidate_count=len(before),
            in_window_candidate_count=len(inside),
            after_window_candidate_count=len(after),
        )
        if after:
            raise PullContractError(
                "history candidate falls after the exact requested window"
            )
        candidates = list(inside)
    return PullNormalizationResult(
        candidates=tuple(candidates),
        unknown_fields=tuple(sorted(unknown)),
        parser_notes=(
            "history_last_sample_equals_send_time_previous_samples_minus_3_seconds",
            "history_leading_context_preserved_in_raw_and_excluded_from_window",
        ),
        history_window_classification=classification,
    )


def sleep_report_is_no_data(data: Mapping[str, Any]) -> bool:
    """Recognize only the source-grounded successful no-report contract.

    A valid report with any positive data signal returns ``False``. Shapes
    that contain no data signal but do not match the recorded null/empty/zero
    contract fail closed instead of being turned into an empty night.
    """

    if not isinstance(data, Mapping):
        raise PullContractError("sleep report data must be an object")
    unknown = set(data) - SLEEP_REPORT_KNOWN_FIELDS
    if unknown:
        raise PullContractError(
            "sleep report contains unknown top-level fields: "
            + ", ".join(sorted(unknown))
        )

    for field in SLEEP_REPORT_SERIES_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if value is not None and not isinstance(value, list):
            raise PullContractError(f"{field} must be null or an array")

    profile = data.get("sleep_profile")
    if "sleep_profile" in data and profile is not None and not isinstance(profile, Mapping):
        raise PullContractError("sleep_profile must be null or an object")
    if isinstance(profile, Mapping):
        profile_unknown = set(profile) - SLEEP_REPORT_PROFILE_FIELDS
        if profile_unknown:
            raise PullContractError(
                "sleep_profile contains unknown fields: "
                + ", ".join(sorted(profile_unknown))
            )

    for field in SLEEP_REPORT_SUMMARY_FIELDS:
        if field not in data or data[field] is None:
            continue
        value = data[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PullContractError(f"{field} must be numeric or null")
        number = float(value)
        if number != number or number in {float("inf"), float("-inf")}:
            raise PullContractError(f"{field} must be finite")

    if any(
        isinstance(data.get(field), list) and bool(data[field])
        for field in SLEEP_REPORT_SERIES_FIELDS
    ):
        return False
    if isinstance(profile, Mapping) and any(value is not None for value in profile.values()):
        return False
    if any(
        data.get(field) not in (None, 0, 0.0)
        for field in SLEEP_REPORT_SUMMARY_FIELDS
    ):
        return False

    missing = SLEEP_REPORT_NO_DATA_REQUIRED_FIELDS - set(data)
    if missing:
        raise PullContractError("sleep report no-data shape is incomplete")
    if not isinstance(profile, Mapping) or not profile:
        raise PullContractError("sleep report no-data profile must be a non-empty all-null object")
    if any(data[field] not in (0, 0.0) for field in (
        "heart_rate_avg", "breathe_avg", "sum_body_shake_times"
    )):
        raise PullContractError("sleep report no-data summaries must be zero")
    return True


def normalize_sleep_report(
    data: Mapping[str, Any],
    *,
    provider_account_id: str,
    provider_device: ProviderDeviceIdentity,
    raw_sha256: str,
    requested_at: datetime,
    received_at: datetime,
    report_date: date,
    binding_timezone_name: str,
) -> PullNormalizationResult:
    _validate_context(raw_sha256, requested_at, received_at)
    unknown = set(data) - SLEEP_REPORT_KNOWN_FIELDS
    if unknown:
        raise PullContractError(
            "sleep report contains unknown top-level fields: "
            + ", ".join(sorted(unknown))
        )
    if sleep_report_is_no_data(data):
        return PullNormalizationResult(
            candidates=(),
            parser_notes=(
                f"binding_timezone:{binding_timezone_name}",
                "sleep_report_no_data",
            ),
        )
    candidates: list[AdapterObservationCandidate] = []
    unsupported: set[str] = {
        field for field in SLEEP_REPORT_UNSUPPORTED_TOP_LEVEL_FIELDS if field in data
    }
    ignored: set[str] = set()
    for item in _optional_mapping_list(
        data.get("apnea_images"), "apnea_images"
    ):
        expected_apnea_fields = {
            "apnea_images",
            "begin_time",
            "end_time",
        }
        if set(item) != expected_apnea_fields:
            raise PullContractError(
                "apnea_images entries must contain exactly the documented fields"
            )
        chart = item["apnea_images"]
        if not isinstance(chart, list):
            raise PullContractError("apnea_images.apnea_images must be an array")
        for chart_value in chart:
            _number(chart_value, "apnea_images.apnea_images[]")
        for time_field in ("begin_time", "end_time"):
            if not isinstance(item[time_field], str) or not item[time_field].strip():
                raise PullContractError(
                    f"apnea_images.{time_field} must be non-empty text"
                )
    stage_items = _optional_mapping_list(
        data.get("sleep_stage_list"), "sleep_stage_list"
    )
    report_window_start: datetime | None = None
    report_window_end: datetime | None = None
    for index, item in enumerate(stage_items):
        extra = set(item) - {
            "start_time", "end_time", "type", "start_time_str", "end_time_str"
        }
        if extra:
            raise PullContractError(
                "sleep_stage_list contains unknown fields: "
                + ", ".join(sorted(extra))
            )
        for display_field in ("start_time_str", "end_time_str"):
            if (
                display_field in item
                and item[display_field] is not None
                and not isinstance(item[display_field], str)
            ):
                raise PullContractError(
                    f"sleep_stage_list.{display_field} must be text"
                )
            if display_field in item:
                ignored.add(f"sleep_stage_list[].{display_field}")
        start = _epoch_time(
            item.get("start_time"), "sleep_stage_list.start_time", unit="seconds"
        )
        end = _epoch_time(
            item.get("end_time"), "sleep_stage_list.end_time", unit="seconds"
        )
        stage_code = _integer(item.get("type"), "sleep_stage_list.type")
        try:
            stage = {
                1: SleepStageState.DEEP,
                2: SleepStageState.LIGHT,
                3: SleepStageState.REM,
                4: SleepStageState.AWAKE,
            }[stage_code]
        except KeyError as exc:
            raise PullContractError(
                "sleep_stage_list.type must be one of the documented codes 1-4"
            ) from exc
        report_window_start = (
            start if report_window_start is None else min(report_window_start, start)
        )
        report_window_end = (
            end if report_window_end is None else max(report_window_end, end)
        )
        candidates.append(
            _candidate(
                endpoint="getSleepReport", suffix=f"stage-{index}",
                payload=SleepStageIntervalPayload(stage=stage, start_at=start, end_at=end,
                                                  source_start_text=str(item.get("start_time")),
                                                  source_end_text=str(item.get("end_time"))),
                source_kind=SourceKind.VENDOR_DERIVED,
                provider_account_id=provider_account_id, provider_device=provider_device,
                raw_sha256=raw_sha256, requested_at=requested_at, received_at=received_at,
                measurement_at=start, source_timestamp_text=str(item.get("start_time")),
                timezone_status=TimezoneStatus.KNOWN,
                quality_flags=("vendor_inferred_sleep_stage", f"vendor_stage_code_{stage_code}"),
                limitations=("not_sleepagent_independent_stage_classification", "not_clinical_truth"),
            )
        )
    for field, observation_type, payload_type, upper, suffix in (
        ("heart_rate_data", ObservationType.HEART_RATE, HeartRatePayload, 300.0, "heart-rate"),
        ("breathe_data", ObservationType.RESPIRATORY_RATE, RespiratoryRatePayload, 150.0, "breath-rate"),
    ):
        for index, item in enumerate(_optional_mapping_list(data.get(field), field)):
            extra = set(item) - {"time_long", "value", "type"}
            if extra:
                raise PullContractError(
                    f"{field} contains unknown fields: "
                    + ", ".join(sorted(extra))
                )
            if "type" in item and item["type"] is not None:
                _integer(item["type"], f"{field}.type")
            if "type" in item:
                ignored.add(f"{field}[].type")
            measured_at = _epoch_time(
                item.get("time_long"), f"{field}.time_long", unit="seconds"
            )
            candidates.append(
                _measurement_candidate(
                    value=item.get("value"), field=f"{field}.value", observation_type=observation_type,
                    payload_type=payload_type, upper=upper, endpoint="getSleepReport",
                    suffix=f"{suffix}-{index}", provider_account_id=provider_account_id,
                    provider_device=provider_device, raw_sha256=raw_sha256,
                    requested_at=requested_at, received_at=received_at,
                    measurement_at=measured_at, source_timestamp_text=str(item.get("time_long")),
                    timezone_status=TimezoneStatus.KNOWN, time_flags=("vendor_report_series",),
                    source_kind=SourceKind.DEVICE_MEASURED,
                    allow_zero=observation_type == ObservationType.MOVEMENT,
                    limitations=(
                        "provider_documented_sleep_period_sensor_measurement",
                    ),
                )
            )
    body_shake_items = _optional_mapping_list(
        data.get("body_shake_data"), "body_shake_data"
    )
    if body_shake_items:
        hourly_shape = all(
            set(item) == {"hour", "count"} for item in body_shake_items
        )
        series_shape = all(
            {"time_long", "value"}.issubset(item) for item in body_shake_items
        )
        if hourly_shape:
            observed_hours: set[int] = set()
            for index, item in enumerate(body_shake_items):
                hour = _report_hour(item.get("hour"))
                if hour in observed_hours:
                    raise PullContractError(
                        "body_shake_data repeats an hourly report bucket"
                    )
                observed_hours.add(hour)
                measured_at = _parse_naive_local(
                    f"{report_date.isoformat()}T{hour:02d}:00:00",
                    ZoneInfo(binding_timezone_name),
                    "body_shake_data.hour",
                )
                candidates.append(
                    _measurement_candidate(
                        value=item.get("count"),
                        field="body_shake_data.count",
                        observation_type=ObservationType.MOVEMENT,
                        payload_type=MovementPayload,
                        upper=None,
                        endpoint="getSleepReport",
                        suffix=f"movement-hour-{index}",
                        provider_account_id=provider_account_id,
                        provider_device=provider_device,
                        raw_sha256=raw_sha256,
                        requested_at=requested_at,
                        received_at=received_at,
                        measurement_at=measured_at,
                        source_timestamp_text=str(item.get("hour")),
                        timezone_status=TimezoneStatus.NORMALIZED_FROM_BINDING,
                        time_flags=("vendor_report_hour_bucket",),
                        source_kind=SourceKind.VENDOR_DERIVED,
                        allow_zero=True,
                        limitations=(
                            "vendor_hourly_movement_count_not_continuous_sample",
                        ),
                    )
                )
        elif series_shape:
            for item in body_shake_items:
                extra = set(item) - {"time_long", "value"}
                if extra:
                    raise PullContractError(
                        "body_shake_data contains unknown fields: "
                        + ", ".join(sorted(extra))
                    )
                _epoch_time(
                    item.get("time_long"), "body_shake_data.time_long",
                    unit="seconds",
                )
                _number(item.get("value"), "body_shake_data.value")
            unsupported.add("body_shake_data[].time_long/value")
        else:
            raise PullContractError(
                "body_shake_data must use one supported deterministic shape"
            )
    summary_contract = {
        "heart_rate_avg": ("heart_rate_mean", "beats_per_minute", 1.0, 300.0),
        "breathe_avg": (
            "respiratory_rate_mean", "breaths_per_minute", 1.0, 150.0
        ),
        "sum_body_shake_times": ("movement_event_total", "count", 0.0, None),
    }
    for vendor_field, (metric_name, unit, lower, upper) in summary_contract.items():
        if vendor_field not in data or data[vendor_field] in (None, "", -1):
            continue
        value = _integer(data[vendor_field], vendor_field)
        if value < lower or (upper is not None and value > upper):
            raise PullContractError(f"{vendor_field} is outside its documented metric contract")
        window_start, window_end = _sleep_report_window(
            report_window_start, report_window_end, vendor_field
        )
        payload = VendorSleepProfileMetricPayload(
            metric_name=metric_name,
            value_state=AvailabilityState.KNOWN,
            value=value,
            source_text=str(data[vendor_field]),
            unit=unit,
            aggregation_start_at=window_start,
            aggregation_end_at=window_end,
            vendor_semantic_code=f"perceptor.sleep_report.{vendor_field}",
        )
        candidates.append(
            _candidate(
                endpoint="getSleepReport", suffix=f"summary-{vendor_field}",
                payload=payload, source_kind=SourceKind.VENDOR_DERIVED,
                provider_account_id=provider_account_id,
                provider_device=provider_device, raw_sha256=raw_sha256,
                requested_at=requested_at, received_at=received_at,
                measurement_at=window_end,
                source_timestamp_text=report_date.isoformat(),
                timezone_status=TimezoneStatus.NORMALIZED_FROM_BINDING,
                quality_flags=(
                    "vendor_sleep_report_summary_metric",
                    "sleep_report_local_day_anchor",
                ),
                limitations=(
                    "vendor_derived_not_clinical_truth",
                    "report_metric_has_local_day_granularity",
                ),
            )
        )
    for index, source_text in enumerate(data.get("getups") or []):
        if not isinstance(source_text, str):
            raise PullContractError("getups entries must be local datetime strings")
        occurred_at = _parse_naive_local(
            source_text, ZoneInfo(binding_timezone_name), "getups"
        )
        candidates.append(
            _candidate(
                endpoint="getSleepReport", suffix=f"getup-{index}",
                payload=BedExitPayload(kind=BedExitKind.GET_UP),
                source_kind=SourceKind.VENDOR_DERIVED,
                provider_account_id=provider_account_id,
                provider_device=provider_device, raw_sha256=raw_sha256,
                requested_at=requested_at, received_at=received_at,
                measurement_at=occurred_at, source_timestamp_text=source_text,
                timezone_status=TimezoneStatus.NORMALIZED_FROM_BINDING,
                quality_flags=("vendor_reported_getup",),
                limitations=("vendor_derived_not_clinical_truth",),
            )
        )
    profile = data.get("sleep_profile")
    if profile is not None:
        if not isinstance(profile, Mapping):
            raise PullContractError("sleep_profile must be an object")
        profile_unknown = set(profile) - SLEEP_REPORT_PROFILE_FIELDS
        if profile_unknown:
            raise PullContractError(
                "sleep_profile contains unknown fields: "
                + ", ".join(sorted(profile_unknown))
            )
        for vendor_field in sorted(profile):
            value = profile[vendor_field]
            if value in (None, ""):
                continue
            if vendor_field not in SLEEP_REPORT_TRUSTED_PROFILE_FIELDS:
                unsupported.add(f"sleep_profile.{vendor_field}")
                continue
            number = _percent(value, f"sleep_profile.{vendor_field}")
            metric_name = {
                "deep_sleep_rate": "deep_sleep_ratio",
                "sleep_efficiency": "sleep_efficiency",
            }[vendor_field]
            window_start, window_end = _sleep_report_window(
                report_window_start,
                report_window_end,
                f"sleep_profile.{vendor_field}",
            )
            payload = VendorSleepProfileMetricPayload(
                metric_name=metric_name,
                value_state=AvailabilityState.KNOWN,
                value=number,
                source_text=str(value),
                unit="percent",
                aggregation_start_at=window_start,
                aggregation_end_at=window_end,
                vendor_semantic_code=(
                    f"perceptor.sleep_report.sleep_profile.{vendor_field}"
                ),
            )
            candidates.append(
                _candidate(
                    endpoint="getSleepReport", suffix=f"profile-{metric_name}", payload=payload,
                    source_kind=SourceKind.VENDOR_DERIVED,
                    provider_account_id=provider_account_id, provider_device=provider_device,
                    raw_sha256=raw_sha256, requested_at=requested_at, received_at=received_at,
                    measurement_at=window_end,
                    source_timestamp_text=report_date.isoformat(),
                    timezone_status=TimezoneStatus.NORMALIZED_FROM_BINDING,
                    quality_flags=(
                        "vendor_sleep_profile_metric",
                        "sleep_report_local_day_anchor",
                    ),
                    limitations=(
                        "vendor_derived_not_clinical_truth",
                        "report_metric_has_local_day_granularity",
                    ),
                )
            )
    return PullNormalizationResult(
        candidates=tuple(candidates),
        intentionally_unsupported_fields=tuple(sorted(unsupported)),
        intentionally_ignored_fields=tuple(sorted(ignored)),
        parser_notes=(
            f"binding_timezone:{binding_timezone_name}",
            "vendor_sleep_stages_remain_vendor_derived",
            "sleep_report_unknown_fields_fail_closed",
        ),
    )


def _sleep_report_window(
    start: datetime | None,
    end: datetime | None,
    field: str,
) -> tuple[datetime, datetime]:
    if start is None or end is None:
        raise PullContractError(
            f"{field} requires the authoritative sleep-stage report window"
        )
    return start, end


def _percent(value: object, field: str) -> float:
    if not isinstance(value, str) or re.fullmatch(
        r"(?:0|[1-9][0-9]?|100)(?:\.[0-9]+)?%", value
    ) is None:
        raise PullContractError(f"{field} must be a documented percentage string")
    number = float(value[:-1])
    if not 0 <= number <= 100:
        raise PullContractError(f"{field} percentage is outside 0-100")
    return number


def _measurement_candidate(
    *, value: object, field: str, observation_type: ObservationType,
    payload_type: type[HeartRatePayload] | type[RespiratoryRatePayload] | type[MovementPayload],
    upper: float | None, endpoint: str, suffix: str, provider_account_id: str,
    provider_device: ProviderDeviceIdentity, raw_sha256: str, requested_at: datetime,
    received_at: datetime, measurement_at: datetime, source_timestamp_text: str | None,
    timezone_status: TimezoneStatus, time_flags: tuple[str, ...] = (),
    source_kind: SourceKind = SourceKind.DEVICE_MEASURED,
    allow_zero: bool = False, limitations: tuple[str, ...] = (),
) -> AdapterObservationCandidate:
    number = _number(value, field)
    if number == -1 or number < 0 or (number == 0 and not allow_zero) or (upper is not None and number > upper):
        payload: ObservationPayload = MissingIntervalPayload(
            target_observation_type=observation_type, missing_state=MissingState.INVALID,
            reason_code="vendor_invalid_sentinel" if number == -1 else "vendor_value_out_of_contract",
            interval_start_at=measurement_at,
        )
        return _candidate(
            endpoint=endpoint, suffix=f"{suffix}-invalid", payload=payload,
            source_kind=source_kind, provider_account_id=provider_account_id,
            provider_device=provider_device, raw_sha256=raw_sha256,
            requested_at=requested_at, received_at=received_at,
            measurement_at=measurement_at, source_timestamp_text=source_timestamp_text,
            timezone_status=timezone_status, missing_state=MissingState.INVALID,
            quality_flags=("vendor_invalid_measurement", field) + time_flags,
            limitations=("invalid_vendor_value_not_promoted_to_physiology",) + limitations,
        )
    payload = payload_type(value=number)
    return _candidate(
        endpoint=endpoint, suffix=suffix, payload=payload, source_kind=source_kind,
        provider_account_id=provider_account_id, provider_device=provider_device,
        raw_sha256=raw_sha256, requested_at=requested_at, received_at=received_at,
        measurement_at=measurement_at, source_timestamp_text=source_timestamp_text,
        timezone_status=timezone_status, quality_flags=time_flags,
        limitations=limitations,
    )


def _candidate(
    *, endpoint: str, suffix: str, payload: ObservationPayload, source_kind: SourceKind,
    provider_account_id: str, provider_device: ProviderDeviceIdentity,
    raw_sha256: str, requested_at: datetime, received_at: datetime,
    measurement_at: datetime, timezone_status: TimezoneStatus,
    source_timestamp_text: str | None = None,
    missing_state: MissingState = MissingState.PRESENT,
    quality_flags: tuple[str, ...] = (), limitations: tuple[str, ...] = (),
) -> AdapterObservationCandidate:
    identity = hashlib.sha256(f"{raw_sha256}|{endpoint}|{suffix}".encode()).hexdigest()
    observation_type = payload.observation_type
    return AdapterObservationCandidate(
        candidate_id=f"perceptor:pull:candidate:{identity}", data_mode=DataMode.LIVE,
        observation_type=observation_type, payload=payload, source_kind=source_kind,
        provider_id=PROVIDER_ID, provider_account_id=provider_account_id,
        provider_device=provider_device, request_signed_at=requested_at,
        measurement_at=measurement_at, received_at=received_at,
        source_timestamp_text=source_timestamp_text, timezone_status=timezone_status,
        quality=ObservationQuality(
            missing_state=missing_state,
            confidence=ConfidenceValue(state=AvailabilityState.NOT_PROVIDED),
            algorithm_version=AlgorithmVersionValue(state=AvailabilityState.NOT_PROVIDED),
            calibration=CalibrationValue(state=AvailabilityState.NOT_PROVIDED),
            quality_flags=quality_flags,
            processing_steps=(PARSER_VERSION, NORMALIZER_VERSION),
            limitations=limitations,
        ),
        provenance=ObservationProvenance(
            provider_id=PROVIDER_ID, provider_account_id=provider_account_id,
            adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION,
            raw_ingress_record_id=f"private-pull-raw:{raw_sha256}",
            raw_payload_sha256=raw_sha256, producer_name="yunyun_v2_5_2_pull_normalizer",
        ),
        source_key=f"perceptor:pull:{endpoint}:{identity}",
        idempotency_key=f"perceptor:pull.v1:{identity}",
    )


def _realtime_time(data: Mapping[str, Any], *, received_at: datetime, timezone_name: str) -> tuple[datetime, str | None, TimezoneStatus, tuple[str, ...]]:
    if data.get("time") not in (None, ""):
        source = _required_scalar_text(data["time"], "time")
        return _epoch_time(
            data["time"], "time", unit="milliseconds"
        ), source, TimezoneStatus.KNOWN, ("vendor_epoch_milliseconds",)
    if data.get("timeFormat") not in (None, ""):
        source = _required_scalar_text(data["timeFormat"], "timeFormat")
        return _parse_naive_local(source, ZoneInfo(timezone_name), "timeFormat"), source, TimezoneStatus.NORMALIZED_FROM_BINDING, ("vendor_naive_time_binding_timezone",)
    return received_at, None, TimezoneStatus.NOT_PROVIDED, ("measurement_time_uses_response_receipt",)


def _report_hour(value: object) -> int:
    if isinstance(value, bool):
        raise PullContractError("body_shake_data.hour must be an hour integer")
    if isinstance(value, int):
        hour = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        hour = int(value)
    else:
        raise PullContractError("body_shake_data.hour must be an hour integer")
    if not 0 <= hour <= 23:
        raise PullContractError("body_shake_data.hour must be between 0 and 23")
    return hour


def _epoch_time(
    value: object, field: str, *, unit: str
) -> datetime:
    number = _number(value, field)
    if number <= 0:
        raise PullContractError(f"{field} must be a positive epoch")
    if unit == "seconds":
        seconds = number
    elif unit == "milliseconds":
        seconds = number / 1000
    else:  # pragma: no cover - internal call sites are frozen above
        raise PullContractError(f"{field} epoch unit is unsupported")
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise PullContractError(f"{field} epoch is invalid") from exc


def _parse_naive_local(value: str, zone: ZoneInfo, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PullContractError(f"{field} is not ISO local datetime") from exc
    if parsed.tzinfo is not None:
        raise PullContractError(f"{field} unexpectedly includes a timezone")
    # ``replace(tzinfo=...)`` alone silently chooses one side of a daylight-
    # saving fold and also invents an instant for a nonexistent wall time.
    # Vendor-local timestamps carry no fold/offset evidence, so accept them
    # only when exactly one UTC instant round-trips to the original wall time.
    candidates = {
        parsed.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        for fold in (0, 1)
        if (
            parsed.replace(tzinfo=zone, fold=fold)
            .astimezone(UTC)
            .astimezone(zone)
            .replace(tzinfo=None)
            == parsed
        )
    }
    if not candidates:
        raise PullContractError(f"{field} is a nonexistent binding-local datetime")
    if len(candidates) != 1:
        raise PullContractError(f"{field} is an ambiguous binding-local datetime")
    return next(iter(candidates))


def _compact_series(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise PullContractError(f"{field} must be a compact comma string")
    parts = tuple(part.strip() for part in value.split(","))
    if not parts or any(not part for part in parts):
        raise PullContractError(f"{field} compact series is empty or malformed")
    return parts


def _mapping_list(value: object, field: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise PullContractError(f"{field} must be a list of objects")
    return tuple(value)


def _optional_mapping_list(
    value: object, field: str
) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    return _mapping_list(value, field)


def _alias(data: Mapping[str, Any], first: str, second: str) -> object | None:
    present = tuple(key for key in (first, second) if key in data)
    if len(present) == 2 and data[first] != data[second]:
        raise PullContractError(f"conflicting aliases for {first}")
    return data[present[0]] if present else None


def _number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise PullContractError(f"{field} must be numeric")
    try:
        number = float(value)  # documented responses mix strings and numbers
    except (TypeError, ValueError) as exc:
        raise PullContractError(f"{field} must be numeric") from exc
    if number != number or number in {float("inf"), float("-inf")}:
        raise PullContractError(f"{field} must be finite")
    return number


def _integer(value: object, field: str) -> int:
    number = _number(value, field)
    if not number.is_integer():
        raise PullContractError(f"{field} must be an integer")
    return int(number)


def _required_scalar_text(value: object, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise PullContractError(f"{field} must be a scalar")
    text = str(value).strip()
    if not text:
        raise PullContractError(f"{field} must be non-empty")
    return text


def _validate_context(raw_sha256: str, requested_at: datetime, received_at: datetime) -> None:
    if len(raw_sha256) != 64 or any(char not in "0123456789abcdef" for char in raw_sha256):
        raise PullContractError("raw_sha256 must be lowercase SHA-256")
    for field, value in (("requested_at", requested_at), ("received_at", received_at)):
        if value.tzinfo is None or value.utcoffset() is None:
            raise PullContractError(f"{field} must be timezone-aware")
    if received_at < requested_at:
        raise PullContractError("received_at precedes requested_at")


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PullContractError(f"{field} must be timezone-aware")


__all__ = [
    "HISTORY_CADENCE", "HistoryWindowClassification", "PullContractError",
    "PullNormalizationResult",
    "canonicalize_pull_result_v2",
    "assert_requested_device_matches_binding", "normalize_current", "normalize_history",
    "normalize_realtime", "normalize_sleep_report", "sleep_report_is_no_data",
    "validate_realtime_start", "with_durable_raw_reference",
]
