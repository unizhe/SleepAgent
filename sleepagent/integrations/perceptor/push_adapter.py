"""Pure Perceptor push normalization Adapter with no transport side effects."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Sequence

from sleepagent.product_device import (
    build_raw_vendor_event,
    build_vital_snapshot_from_raw_event,
    normalize_vendor_timestamp,
)
from sleepagent.sleep_domain.contracts import (
    AdapterDescriptor,
    AdapterObservationCandidate,
    AlgorithmVersionValue,
    AlertLifecycleState,
    AlertSeverity,
    AvailabilityState,
    CalibrationValue,
    ConfidenceValue,
    DeviceConnectivityPayload,
    DeviceConnectivityState,
    MissingState,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    ProviderDeviceIdentity,
    SourceKind,
    TimezoneStatus,
    VendorAlertPayload,
)
from sleepagent.sleep_domain.radar_compat import radar_vital_snapshot_to_candidates
from sleepagent.sleep_domain.registry import AdapterInputEnvelope


class PerceptorPushAdapter:
    """Allowlisted deterministic parser; it performs no network or external write."""

    def __init__(self, descriptor: AdapterDescriptor) -> None:
        self._descriptor = descriptor

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def normalize(
        self,
        adapter_input: AdapterInputEnvelope,
    ) -> Sequence[AdapterObservationCandidate]:
        payload = _strict_json_object(adapter_input.raw_payload)
        from sleepagent.integrations.perceptor.pull_adapter import (
            is_perceptor_pull_envelope,
            normalize_perceptor_pull,
        )

        if is_perceptor_pull_envelope(payload):
            return normalize_perceptor_pull(
                adapter_input,
                self.descriptor,
                payload,
            )
        raw_record = adapter_input.raw_record
        event = build_raw_vendor_event(
            payload,
            vendor=raw_record.provider_id,
            received_at=raw_record.received_at,
            raw_event_id=raw_record.raw_ingress_record_id,
        )
        if event.event_type == "VitalSignsDataEvent":
            snapshot = build_vital_snapshot_from_raw_event(event)
            legacy = radar_vital_snapshot_to_candidates(
                snapshot,
                provider_account_id=raw_record.provider_account_id,
                adapter_id=self.descriptor.adapter_id,
                adapter_version=self.descriptor.adapter_version,
                data_mode=raw_record.data_mode,
            )
            return tuple(
                _pin_raw_provenance(candidate, raw_record, index)
                for index, candidate in enumerate(legacy)
            )
        if event.event_type in {"ConnectedEvent", "DisconnectedEvent"}:
            occurred_at, timezone_status, source_text = _safe_source_time(
                event.data_payload
            )
            candidate = _base_candidate(
                raw_record=raw_record,
                descriptor=self.descriptor,
                provider_device=_provider_device(payload, event.data_payload),
                observation_type=ObservationType.DEVICE_CONNECTIVITY,
                payload=DeviceConnectivityPayload(
                    state=(
                        DeviceConnectivityState.ONLINE
                        if event.event_type == "ConnectedEvent"
                        else DeviceConnectivityState.OFFLINE
                    ),
                    vendor_status_code=event.event_type,
                ),
                source_kind=SourceKind.DEVICE_MEASURED,
                event_occurred_at=occurred_at,
                timezone_status=timezone_status,
                source_timestamp_text=source_text,
                suffix="connectivity",
            )
            return (candidate,)
        if event.event_type in {"AlarmEvent", "AlarmStopEvent"}:
            occurred_at, timezone_status, source_text = _safe_source_time(
                event.data_payload
            )
            alert_code = _first_text(
                event.data_payload,
                "alarm_type",
                "alarmType",
                "alarm_code",
                "alarmCode",
                "code",
            ) or event.event_type
            candidate = _base_candidate(
                raw_record=raw_record,
                descriptor=self.descriptor,
                provider_device=_provider_device(payload, event.data_payload),
                observation_type=ObservationType.VENDOR_ALERT,
                payload=VendorAlertPayload(
                    alert_code=alert_code,
                    severity=AlertSeverity.UNKNOWN,
                    lifecycle_state=(
                        AlertLifecycleState.ACTIVE
                        if event.event_type == "AlarmEvent"
                        else AlertLifecycleState.STOPPED
                    ),
                    vendor_alert_instance_id=_first_text(
                        event.data_payload,
                        "alarm_id",
                        "alarmId",
                        "alert_id",
                        "alertId",
                    ),
                    title=_first_text(event.data_payload, "title", "name"),
                    message=_first_text(event.data_payload, "message", "msg"),
                ),
                source_kind=SourceKind.VENDOR_DERIVED,
                event_occurred_at=occurred_at,
                timezone_status=timezone_status,
                source_timestamp_text=source_text,
                suffix=f"alert:{event.event_type}",
            )
            return (candidate,)
        raise ValueError(f"unsupported Perceptor push event: {event.event_type}")


def _strict_json_object(raw_payload: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        parsed = json.loads(raw_payload, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Perceptor push body is not valid UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Perceptor push body must be a JSON object")
    return parsed


def _pin_raw_provenance(
    candidate: AdapterObservationCandidate,
    raw_record: Any,
    ordinal: int,
) -> AdapterObservationCandidate:
    provenance = candidate.provenance.model_copy(
        update={
            "raw_ingress_record_id": raw_record.raw_ingress_record_id,
            "raw_payload_sha256": raw_record.pre_normalization_payload_sha256,
            "source_record_id": raw_record.message_id,
            "source_idempotency_key": raw_record.idempotency_identity,
        }
    )
    suffix = candidate.observation_type.value
    identity = hashlib.sha256(
        (
            f"{raw_record.raw_ingress_record_id}|{suffix}|{ordinal}|"
            f"{candidate.provenance.adapter_version}"
        ).encode("utf-8")
    ).hexdigest()
    return candidate.model_copy(
        update={
            "candidate_id": f"perceptor:{identity}",
            "provenance": provenance,
            "source_key": (
                f"perceptor:{raw_record.raw_ingress_record_id}:{suffix}:{ordinal}"
            ),
            "idempotency_key": (
                f"perceptor:{raw_record.idempotency_identity}:{suffix}:{ordinal}:"
                f"{candidate.provenance.adapter_version}"
            ),
        }
    )


def _base_candidate(
    *,
    raw_record: Any,
    descriptor: AdapterDescriptor,
    provider_device: ProviderDeviceIdentity,
    observation_type: ObservationType,
    payload: Any,
    source_kind: SourceKind,
    event_occurred_at: datetime | None,
    timezone_status: TimezoneStatus,
    source_timestamp_text: str | None,
    suffix: str,
) -> AdapterObservationCandidate:
    identity = hashlib.sha256(
        (
            f"{raw_record.raw_ingress_record_id}|{suffix}|"
            f"{descriptor.adapter_version}"
        ).encode("utf-8")
    ).hexdigest()
    return AdapterObservationCandidate(
        candidate_id=f"perceptor:{identity}",
        data_mode=raw_record.data_mode,
        observation_type=observation_type,
        payload=payload,
        source_kind=source_kind,
        provider_id=raw_record.provider_id,
        provider_account_id=raw_record.provider_account_id,
        provider_device=provider_device,
        request_signed_at=raw_record.request_signed_at,
        measurement_at=None,
        event_occurred_at=event_occurred_at,
        received_at=raw_record.received_at,
        source_timestamp_text=source_timestamp_text,
        timezone_status=timezone_status,
        quality=ObservationQuality(
            missing_state=MissingState.PRESENT,
            confidence=ConfidenceValue(state=AvailabilityState.NOT_PROVIDED),
            algorithm_version=AlgorithmVersionValue(
                state=AvailabilityState.NOT_PROVIDED
            ),
            calibration=CalibrationValue(
                state=AvailabilityState.NOT_PROVIDED
            ),
            processing_steps=("perceptor_push_parse.v1",),
            limitations=(
                "push_compatibility_profile_pending_real_confirmation",
            ),
        ),
        provenance=ObservationProvenance(
            provider_id=raw_record.provider_id,
            provider_account_id=raw_record.provider_account_id,
            adapter_id=descriptor.adapter_id,
            adapter_version=descriptor.adapter_version,
            raw_ingress_record_id=raw_record.raw_ingress_record_id,
            raw_payload_sha256=raw_record.pre_normalization_payload_sha256,
            source_record_id=raw_record.message_id,
            source_idempotency_key=raw_record.idempotency_identity,
            producer_name="perceptor_push_adapter",
        ),
        source_key=f"perceptor:{raw_record.raw_ingress_record_id}:{suffix}",
        idempotency_key=(
            f"perceptor:{raw_record.idempotency_identity}:{suffix}:"
            f"{descriptor.adapter_version}"
        ),
    )


def _provider_device(
    payload: dict[str, Any],
    data: dict[str, Any],
) -> ProviderDeviceIdentity:
    device_id = _first_text(payload, "device_id", "deviceId")
    device_name = _first_text(
        payload,
        "device_name",
        "deviceName",
    ) or _first_text(data, "device_name", "deviceName")
    return ProviderDeviceIdentity(
        provider_device_id=device_id,
        provider_device_name=device_name,
        product_id=_first_text(payload, "product_id", "productId"),
        home_id=_first_text(payload, "home_id", "homeId"),
        project_id=_first_text(payload, "project_id", "projectId"),
    )


def _safe_source_time(
    data: dict[str, Any],
) -> tuple[datetime | None, TimezoneStatus, str | None]:
    value: Any = None
    for key in (
        "DateTime",
        "dateTime",
        "datetime",
        "event_time",
        "eventTime",
        "occurred_at",
    ):
        if key in data:
            value = data[key]
            break
    if value is None or value == "":
        return None, TimezoneStatus.NOT_PROVIDED, None
    source_text = str(value)
    if isinstance(value, str) and not _looks_numeric(value):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None, TimezoneStatus.TIMEZONE_UNKNOWN, source_text
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None, TimezoneStatus.TIMEZONE_UNKNOWN, source_text
    try:
        return normalize_vendor_timestamp(value), TimezoneStatus.KNOWN, source_text
    except (TypeError, ValueError):
        return None, TimezoneStatus.TIMEZONE_UNKNOWN, source_text


def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _looks_numeric(value: str) -> bool:
    stripped = value.strip()
    return stripped.replace(".", "", 1).replace("-", "", 1).isdigit()


__all__ = ["PerceptorPushAdapter"]
