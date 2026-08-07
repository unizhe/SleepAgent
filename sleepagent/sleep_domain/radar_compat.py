"""One-way compatibility conversion from legacy product-device Radar schemas.

Legacy payload-bearing models remain unchanged.  Conversion consumes their raw
fields only to compute an immutable digest and emits reference-only provenance.
There is intentionally no conversion from provider-neutral contracts back to
legacy Radar models.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import NoReturn

from sleepagent.product_device.schemas import (
    RadarAlertEvent,
    RadarAlertSeverity,
    RadarBedPresence,
    RadarSleepReport,
    RadarSleepStage,
    RadarSleepStageSegment,
    RadarSourceMetadata,
    RadarVitalSnapshot,
    RawVendorEvent,
    normalize_vendor_timestamp,
)
from sleepagent.sleep_domain.contracts import (
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
    DeviceBinding,
    DeviceBindingStatus,
    HeartRatePayload,
    MissingIntervalPayload,
    MissingState,
    MovementPayload,
    ObservationPayload,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    ProviderDeviceIdentity,
    RawIngressRecordMetadata,
    RespiratoryRatePayload,
    SignatureVerificationState,
    SleepObservation,
    SleepStageIntervalPayload,
    SleepStageState,
    SourceKind,
    TimezoneStatus,
    VendorAlertPayload,
    VendorSleepProfileMetricPayload,
)


def _legacy_payload_sha256(source: RadarSourceMetadata) -> str:
    serialized = json.dumps(
        {
            "raw_payload": source.raw_payload,
            "data_payload": source.data_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _legacy_raw_event_sha256(event: RawVendorEvent) -> str:
    serialized = json.dumps(
        {
            "raw_payload": event.raw_payload,
            "data_payload": event.data_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _provider_device(
    source: RadarSourceMetadata,
    *,
    fallback_device_id: str,
) -> ProviderDeviceIdentity:
    return ProviderDeviceIdentity(
        provider_device_id=source.vendor_device_id,
        provider_device_name=source.vendor_device_name or fallback_device_id,
        product_id=source.vendor_product_id,
        home_id=source.vendor_home_id,
    )


def _explicitly_zoned_timestamp(
    value: object,
) -> tuple[datetime | None, TimezoneStatus, str | None]:
    if value is None or value == "":
        return None, TimezoneStatus.NOT_PROVIDED, None
    source_text = value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None, TimezoneStatus.TIMEZONE_UNKNOWN, source_text
        return normalize_vendor_timestamp(value), TimezoneStatus.KNOWN, source_text
    if isinstance(value, (int, float)):
        return normalize_vendor_timestamp(value), TimezoneStatus.KNOWN, source_text
    if isinstance(value, str):
        stripped = value.strip()
        numeric = stripped.replace(".", "", 1).replace("-", "", 1).isdigit()
        if numeric:
            return (
                normalize_vendor_timestamp(stripped),
                TimezoneStatus.KNOWN,
                source_text,
            )
        normalized = stripped.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            raise ValueError(f"unsupported legacy timestamp: {value!r}") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None, TimezoneStatus.TIMEZONE_UNKNOWN, source_text
        return normalize_vendor_timestamp(parsed), TimezoneStatus.KNOWN, source_text
    raise ValueError(f"unsupported legacy timestamp type: {type(value).__name__}")


def _legacy_request_signed_at(source: RadarSourceMetadata) -> datetime | None:
    # The provider's top-level timestamp belongs to request signing/replay
    # validation.  It must never be used as a physiological measurement time.
    value = source.raw_payload.get("timestamp")
    normalized, _, _ = _explicitly_zoned_timestamp(value)
    return normalized


def _legacy_measurement_time(
    source: RadarSourceMetadata,
) -> tuple[datetime | None, TimezoneStatus, str | None]:
    for key in ("DateTime", "dateTime", "datetime", "measurement_at"):
        if key in source.data_payload:
            return _explicitly_zoned_timestamp(source.data_payload[key])
    # The already-normalized legacy snapshot does not retain enough
    # information to prove whether its fallback came from a signed request.
    return None, TimezoneStatus.NOT_PROVIDED, None


def _legacy_event_time_from_data(event: RawVendorEvent) -> datetime | None:
    for key in (
        "DateTime",
        "dateTime",
        "datetime",
        "event_time",
        "eventTime",
        "occurred_at",
        "timestamp",
    ):
        if key in event.data_payload:
            normalized, _, _ = _explicitly_zoned_timestamp(
                event.data_payload[key]
            )
            return normalized
    return None


def _provenance(
    source: RadarSourceMetadata,
    *,
    adapter_id: str,
    adapter_version: str,
    provider_account_id: str,
) -> ObservationProvenance:
    payload_hash = _legacy_payload_sha256(source)
    raw_reference = source.raw_event_id or f"legacy-radar:{payload_hash}"
    return ObservationProvenance(
        provider_id=source.vendor,
        provider_account_id=provider_account_id,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        raw_ingress_record_id=raw_reference,
        raw_payload_sha256=payload_hash,
        source_record_id=source.vendor_message_id,
        source_idempotency_key=source.vendor_message_id,
        producer_name=source.vendor,
    )


def _unknown_confidence() -> ConfidenceValue:
    return ConfidenceValue(
        state=AvailabilityState.NOT_PROVIDED,
        reason="legacy Radar schema did not provide confidence",
    )


def _unknown_algorithm_version() -> AlgorithmVersionValue:
    return AlgorithmVersionValue(
        state=AvailabilityState.NOT_PROVIDED,
        reason="legacy Radar schema did not provide algorithm version",
    )


def _unknown_calibration() -> CalibrationValue:
    return CalibrationValue(
        state=AvailabilityState.NOT_PROVIDED,
        description="legacy Radar schema did not provide calibration",
    )


def _quality(
    missing_state: MissingState,
    *,
    flags: tuple[str, ...] = (),
    confidence: ConfidenceValue | None = None,
) -> ObservationQuality:
    return ObservationQuality(
        missing_state=missing_state,
        confidence=confidence or _unknown_confidence(),
        algorithm_version=_unknown_algorithm_version(),
        calibration=_unknown_calibration(),
        quality_flags=flags,
        processing_steps=("legacy_radar_one_way_conversion.v1",),
        limitations=(
            "legacy source did not distinguish algorithm and calibration metadata",
        ),
    )


def _candidate(
    *,
    payload: ObservationPayload,
    source: RadarSourceMetadata,
    fallback_device_id: str,
    source_kind: SourceKind,
    data_mode: DataMode,
    adapter_id: str,
    adapter_version: str,
    provider_account_id: str,
    measurement_at: datetime | None,
    event_occurred_at: datetime | None,
    received_at: datetime,
    timezone_status: TimezoneStatus,
    source_timestamp_text: str | None,
    quality: ObservationQuality,
    suffix: str,
) -> AdapterObservationCandidate:
    provenance = _provenance(
        source,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        provider_account_id=provider_account_id,
    )
    source_key = (
        f"{provenance.raw_ingress_record_id}:"
        f"{payload.observation_type.value}:{suffix}"
    )
    return AdapterObservationCandidate(
        candidate_id=f"legacy-candidate:{source_key}",
        data_mode=data_mode,
        observation_type=payload.observation_type,
        payload=payload,
        source_kind=source_kind,
        provider_id=source.vendor,
        provider_account_id=provider_account_id,
        provider_device=_provider_device(
            source,
            fallback_device_id=fallback_device_id,
        ),
        request_signed_at=_legacy_request_signed_at(source),
        measurement_at=measurement_at,
        event_occurred_at=event_occurred_at,
        received_at=received_at,
        source_timestamp_text=source_timestamp_text,
        timezone_status=timezone_status,
        quality=quality,
        provenance=provenance,
        source_key=source_key,
        idempotency_key=f"{source_key}:{provenance.raw_payload_sha256}",
    )


def _missing_payload(
    observation_type: ObservationType,
    flags: tuple[str, ...],
) -> MissingIntervalPayload:
    invalid = any("invalid" in flag for flag in flags)
    state = MissingState.INVALID if invalid else MissingState.MISSING
    reason = flags[0] if flags else f"{observation_type.value}_missing"
    return MissingIntervalPayload(
        target_observation_type=observation_type,
        missing_state=state,
        reason_code=reason,
    )


def radar_vital_snapshot_to_candidates(
    snapshot: RadarVitalSnapshot,
    *,
    provider_account_id: str,
    adapter_id: str,
    adapter_version: str,
    data_mode: DataMode,
) -> tuple[AdapterObservationCandidate, ...]:
    """Convert one legacy snapshot into explicit, independently typed facts."""

    candidates: list[AdapterObservationCandidate] = []
    all_flags = tuple(snapshot.invalid_reading_flags)
    measurement_at, timezone_status, timestamp_text = _legacy_measurement_time(
        snapshot.source_metadata
    )

    heart_flags = tuple(flag for flag in all_flags if flag.startswith("heart_rate_"))
    heart_payload: ObservationPayload
    if snapshot.heart_rate_bpm is None:
        heart_payload = _missing_payload(ObservationType.HEART_RATE, heart_flags)
        heart_missing_state = heart_payload.missing_state
    else:
        heart_payload = HeartRatePayload(value=snapshot.heart_rate_bpm)
        heart_missing_state = MissingState.PRESENT
    candidates.append(
        _candidate(
            payload=heart_payload,
            source=snapshot.source_metadata,
            fallback_device_id=snapshot.radar_device_id,
            source_kind=SourceKind.DEVICE_MEASURED,
            data_mode=data_mode,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            provider_account_id=provider_account_id,
            measurement_at=measurement_at,
            event_occurred_at=None,
            received_at=snapshot.received_at,
            timezone_status=timezone_status,
            source_timestamp_text=timestamp_text,
            quality=_quality(heart_missing_state, flags=heart_flags),
            suffix="heart-rate",
        )
    )

    respiratory_flags = tuple(
        flag for flag in all_flags if flag.startswith("breath_rate_")
    )
    respiratory_payload: ObservationPayload
    if snapshot.breath_rate_bpm is None:
        respiratory_payload = _missing_payload(
            ObservationType.RESPIRATORY_RATE,
            respiratory_flags,
        )
        respiratory_missing_state = respiratory_payload.missing_state
    else:
        respiratory_payload = RespiratoryRatePayload(
            value=snapshot.breath_rate_bpm
        )
        respiratory_missing_state = MissingState.PRESENT
    candidates.append(
        _candidate(
            payload=respiratory_payload,
            source=snapshot.source_metadata,
            fallback_device_id=snapshot.radar_device_id,
            source_kind=SourceKind.DEVICE_MEASURED,
            data_mode=data_mode,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            provider_account_id=provider_account_id,
            measurement_at=measurement_at,
            event_occurred_at=None,
            received_at=snapshot.received_at,
            timezone_status=timezone_status,
            source_timestamp_text=timestamp_text,
            quality=_quality(
                respiratory_missing_state,
                flags=respiratory_flags,
            ),
            suffix="respiratory-rate",
        )
    )

    movement_payload: ObservationPayload
    if snapshot.body_movement is None:
        movement_payload = _missing_payload(ObservationType.MOVEMENT, ())
        movement_missing_state = MissingState.MISSING
    else:
        movement_payload = MovementPayload(
            value=snapshot.body_movement,
            unit="index",
        )
        movement_missing_state = MissingState.PRESENT
    candidates.append(
        _candidate(
            payload=movement_payload,
            source=snapshot.source_metadata,
            fallback_device_id=snapshot.radar_device_id,
            source_kind=SourceKind.DEVICE_MEASURED,
            data_mode=data_mode,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            provider_account_id=provider_account_id,
            measurement_at=measurement_at,
            event_occurred_at=None,
            received_at=snapshot.received_at,
            timezone_status=timezone_status,
            source_timestamp_text=timestamp_text,
            quality=_quality(movement_missing_state),
            suffix="movement",
        )
    )

    bed_state = {
        RadarBedPresence.IN_BED: BedPresenceState.IN_BED,
        RadarBedPresence.OUT_OF_BED: BedPresenceState.OUT_OF_BED,
        RadarBedPresence.UNKNOWN: BedPresenceState.UNKNOWN,
    }[snapshot.bed_presence]
    bed_missing_state = (
        MissingState.PRESENT
        if bed_state != BedPresenceState.UNKNOWN
        else MissingState.UNKNOWN
    )
    candidates.append(
        _candidate(
            payload=BedPresencePayload(state=bed_state),
            source=snapshot.source_metadata,
            fallback_device_id=snapshot.radar_device_id,
            source_kind=SourceKind.DEVICE_MEASURED,
            data_mode=data_mode,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            provider_account_id=provider_account_id,
            measurement_at=measurement_at,
            event_occurred_at=None,
            received_at=snapshot.received_at,
            timezone_status=timezone_status,
            source_timestamp_text=timestamp_text,
            quality=_quality(bed_missing_state),
            suffix="bed-presence",
        )
    )
    return tuple(candidates)


def radar_sleep_stage_to_candidate(
    segment: RadarSleepStageSegment,
    *,
    provider_account_id: str,
    adapter_id: str,
    adapter_version: str,
    data_mode: DataMode,
    ordinal: int = 0,
) -> AdapterObservationCandidate:
    stage = {
        RadarSleepStage.DEEP: SleepStageState.DEEP,
        RadarSleepStage.LIGHT: SleepStageState.LIGHT,
        RadarSleepStage.REM: SleepStageState.REM,
        RadarSleepStage.AWAKE: SleepStageState.AWAKE,
        RadarSleepStage.UNKNOWN: SleepStageState.UNKNOWN,
    }[segment.stage]
    confidence = (
        ConfidenceValue(state=AvailabilityState.KNOWN, value=segment.confidence)
        if segment.confidence is not None
        else _unknown_confidence()
    )
    return _candidate(
        payload=SleepStageIntervalPayload(
            stage=stage,
            start_at=segment.start_at,
            end_at=segment.end_at,
        ),
        source=segment.source_metadata,
        fallback_device_id=segment.radar_device_id,
        source_kind=SourceKind.VENDOR_DERIVED,
        data_mode=data_mode,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        provider_account_id=provider_account_id,
        measurement_at=None,
        event_occurred_at=segment.start_at,
        received_at=segment.source_metadata.received_at,
        timezone_status=TimezoneStatus.KNOWN,
        source_timestamp_text=segment.start_at.isoformat(),
        quality=_quality(
            MissingState.PRESENT
            if stage != SleepStageState.UNKNOWN
            else MissingState.UNKNOWN,
            confidence=confidence,
        ),
        suffix=f"sleep-stage-{ordinal}",
    )


def radar_alert_to_candidate(
    alert: RadarAlertEvent,
    *,
    provider_account_id: str,
    adapter_id: str,
    adapter_version: str,
    data_mode: DataMode,
) -> AdapterObservationCandidate:
    severity = {
        RadarAlertSeverity.INFO: AlertSeverity.INFO,
        RadarAlertSeverity.WARNING: AlertSeverity.WARNING,
        RadarAlertSeverity.CRITICAL: AlertSeverity.CRITICAL,
        RadarAlertSeverity.UNKNOWN: AlertSeverity.UNKNOWN,
    }[alert.severity]
    return _candidate(
        payload=VendorAlertPayload(
            alert_code=alert.alert_type,
            severity=severity,
            lifecycle_state=AlertLifecycleState.ACTIVE,
            vendor_alert_instance_id=alert.radar_alert_event_id,
            title=alert.title,
            message=alert.message,
        ),
        source=alert.source_metadata,
        fallback_device_id=alert.radar_device_id,
        source_kind=SourceKind.VENDOR_DERIVED,
        data_mode=data_mode,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        provider_account_id=provider_account_id,
        measurement_at=None,
        event_occurred_at=alert.occurred_at,
        received_at=alert.source_metadata.received_at,
        timezone_status=TimezoneStatus.KNOWN,
        source_timestamp_text=alert.occurred_at.isoformat(),
        quality=_quality(
            MissingState.PRESENT
            if severity != AlertSeverity.UNKNOWN
            else MissingState.UNKNOWN
        ),
        suffix=f"alert-{alert.radar_alert_event_id}",
    )


def radar_sleep_report_to_candidates(
    report: RadarSleepReport,
    *,
    provider_account_id: str,
    adapter_id: str,
    adapter_version: str,
    data_mode: DataMode,
) -> tuple[AdapterObservationCandidate, ...]:
    candidates = [
        radar_sleep_stage_to_candidate(
            segment,
            provider_account_id=provider_account_id,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            data_mode=data_mode,
            ordinal=index,
        )
        for index, segment in enumerate(report.stage_segments)
    ]
    metrics: tuple[tuple[str, str | int | float | bool | None, str | None], ...] = (
        ("total_sleep_minutes", report.total_sleep_minutes, "minutes"),
        ("sleep_score", report.sleep_score, "score"),
        ("deep_sleep_minutes", report.deep_sleep_minutes, "minutes"),
        ("light_sleep_minutes", report.light_sleep_minutes, "minutes"),
        ("rem_sleep_minutes", report.rem_sleep_minutes, "minutes"),
        ("awake_minutes", report.awake_minutes, "minutes"),
        ("movement_count", report.movement_count, "count"),
        ("getup_count", report.getup_count, "count"),
    )
    report_time = report.sleep_end_at or report.sleep_start_at
    for metric_name, value, unit in metrics:
        value_state = (
            AvailabilityState.KNOWN
            if value is not None
            else AvailabilityState.NOT_PROVIDED
        )
        candidates.append(
            _candidate(
                payload=VendorSleepProfileMetricPayload(
                    metric_name=metric_name,
                    value_state=value_state,
                    value=value,
                    unit=unit,
                ),
                source=report.source_metadata,
                fallback_device_id=report.radar_device_id,
                source_kind=SourceKind.VENDOR_DERIVED,
                data_mode=data_mode,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                provider_account_id=provider_account_id,
                measurement_at=None,
                event_occurred_at=report_time,
                received_at=report.source_metadata.received_at,
                timezone_status=(
                    TimezoneStatus.KNOWN
                    if report_time is not None
                    else TimezoneStatus.NOT_PROVIDED
                ),
                source_timestamp_text=(
                    report_time.isoformat() if report_time is not None else None
                ),
                quality=_quality(
                    MissingState.PRESENT
                    if value is not None
                    else MissingState.NOT_PROVIDED
                ),
                suffix=f"sleep-profile-{metric_name}",
            )
        )
    return tuple(candidates)


def legacy_raw_vendor_event_to_metadata(
    event: RawVendorEvent,
    *,
    provider_account_id: str,
    data_mode: DataMode,
    encrypted_payload_reference: str,
    idempotency_version: str,
    payload_size_bytes: int,
    retention_deadline: datetime,
    signature_profile: str | None = None,
    signature_verification: SignatureVerificationState = (
        SignatureVerificationState.UNKNOWN
    ),
) -> RawIngressRecordMetadata:
    payload_hash = _legacy_raw_event_sha256(event)
    return RawIngressRecordMetadata(
        raw_ingress_record_id=event.raw_event_id,
        data_mode=data_mode,
        provider_id=event.vendor,
        provider_account_id=provider_account_id,
        event_type=event.event_type,
        message_id=event.message_id,
        request_signed_at=_explicitly_zoned_timestamp(
            event.raw_payload.get("timestamp")
        )[0],
        measurement_at=None,
        event_occurred_at=_legacy_event_time_from_data(event),
        received_at=event.received_at,
        signature_profile=signature_profile,
        signature_verification=signature_verification,
        idempotency_identity=event.message_id or f"payload-sha256:{payload_hash}",
        idempotency_version=idempotency_version,
        pre_normalization_payload_sha256=payload_hash,
        encrypted_payload_reference=encrypted_payload_reference,
        content_type="application/json",
        payload_size_bytes=payload_size_bytes,
        retention_deadline=retention_deadline,
    )


def bind_adapter_candidate(
    candidate: AdapterObservationCandidate,
    binding: DeviceBinding,
    *,
    observation_id: str | None = None,
) -> SleepObservation:
    """Pure, explicit candidate-to-observation conversion.

    This is not a binding service: callers must supply the exact already
    resolved binding.  No current-binding lookup or implicit backdating occurs.
    """

    if binding.status == DeviceBindingStatus.REVOKED:
        raise ValueError("revoked binding cannot create observations")
    if candidate.data_mode != binding.data_mode:
        raise ValueError("candidate and binding data_mode must match")
    if candidate.provider_id != binding.provider_id:
        raise ValueError("candidate and binding provider_id must match")
    if candidate.provider_account_id != binding.provider_account_id:
        raise ValueError("candidate and binding provider_account_id must match")
    if not _provider_identity_matches(
        candidate.provider_device,
        binding.provider_device,
    ):
        raise ValueError("candidate does not match the supplied provider device")

    attribution_time = candidate.measurement_at or candidate.event_occurred_at
    if attribution_time is None:
        raise ValueError(
            "candidate needs measurement_at or event_occurred_at for binding"
        )
    if attribution_time < binding.effective_from:
        raise ValueError("candidate time precedes binding effective_from")
    if (
        binding.effective_until is not None
        and attribution_time >= binding.effective_until
    ):
        raise ValueError("candidate time is outside binding effective interval")

    return SleepObservation(
        observation_id=observation_id
        or (
            f"observation:{candidate.candidate_id}:"
            f"binding-v{binding.binding_version}"
        ),
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


def radar_schema_to_candidates(
    record: RadarVitalSnapshot
    | RadarSleepStageSegment
    | RadarSleepReport
    | RadarAlertEvent,
    *,
    provider_account_id: str,
    adapter_id: str,
    adapter_version: str,
    data_mode: DataMode,
) -> tuple[AdapterObservationCandidate, ...]:
    """Dispatch known legacy Radar formats and reject every unknown format."""

    if isinstance(record, RadarVitalSnapshot):
        return radar_vital_snapshot_to_candidates(
            record,
            provider_account_id=provider_account_id,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            data_mode=data_mode,
        )
    if isinstance(record, RadarSleepStageSegment):
        return (
            radar_sleep_stage_to_candidate(
                record,
                provider_account_id=provider_account_id,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                data_mode=data_mode,
            ),
        )
    if isinstance(record, RadarSleepReport):
        return radar_sleep_report_to_candidates(
            record,
            provider_account_id=provider_account_id,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            data_mode=data_mode,
        )
    if isinstance(record, RadarAlertEvent):
        return (
            radar_alert_to_candidate(
                record,
                provider_account_id=provider_account_id,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                data_mode=data_mode,
            ),
        )
    return _reject_unknown_radar_schema(record)


def _reject_unknown_radar_schema(record: object) -> NoReturn:
    raise TypeError(f"unsupported legacy Radar schema: {type(record).__name__}")


def _provider_identity_matches(
    candidate: ProviderDeviceIdentity,
    binding: ProviderDeviceIdentity,
) -> bool:
    candidate_keys = {
        ("provider_device_id", candidate.provider_device_id),
        ("provider_device_name", candidate.provider_device_name),
        *{(f"native:{key}", value) for key, value in candidate.native_keys.items()},
    }
    binding_keys = {
        ("provider_device_id", binding.provider_device_id),
        ("provider_device_name", binding.provider_device_name),
        *{(f"native:{key}", value) for key, value in binding.native_keys.items()},
    }
    return bool(
        {(key, value) for key, value in candidate_keys if value}
        & {(key, value) for key, value in binding_keys if value}
    )


__all__ = [
    "bind_adapter_candidate",
    "legacy_raw_vendor_event_to_metadata",
    "radar_alert_to_candidate",
    "radar_schema_to_candidates",
    "radar_sleep_report_to_candidates",
    "radar_sleep_stage_to_candidate",
    "radar_vital_snapshot_to_candidates",
]
