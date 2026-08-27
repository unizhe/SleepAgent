"""Pure semantic identity helpers for canonical observation reconciliation.

Raw ingress identity is intentionally unsuitable for Push/Pull reconciliation:
two acquisition paths can encode the same fact with different payload bytes.  This
module derives provider-neutral fact-slot and value identities exclusively from an
already-bound :class:`SleepObservation`.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sleepagent.domain.contracts import (
    DomainNamespace,
    MissingIntervalPayload,
    ObservationProvenance,
    SleepObservation,
    SleepStageIntervalPayload,
    UnknownObservationPayload,
    VendorAlertPayload,
    VendorSleepProfileMetricPayload,
)


_SURFACE_PATTERN = re.compile(
    r"^[a-z0-9]+(?:[._:-][a-z0-9]+)*$",
    flags=re.ASCII,
)
_ADAPTER_TOKEN_PATTERN = re.compile(r"[^a-z0-9]+", flags=re.ASCII)
_MAX_SEMANTIC_SURFACE_LENGTH = 128

RECONCILIATION_CONFLICT_QUALITY_FLAG = "push_pull_conflict"
RECONCILIATION_CONFLICT_LIMITATION = (
    "conflicting_values_for_same_semantic_fact_slot"
)


class AcquisitionChannel(str, Enum):
    """The raw transport authority that supplied a canonical observation."""

    PUSH = "PUSH"
    PULL = "PULL"


def semantic_fact_slot_key(
    observation: SleepObservation,
    namespace_id: str,
    semantic_surface: str,
    *,
    namespace_generation: int = 1,
) -> str:
    """Return the deterministic slot occupied by an observation's semantic fact.

    The slot deliberately excludes the observed value and all raw/acquisition
    identity.  Consequently, equal Push and Pull facts share a slot, while unequal
    values for that slot can be detected by comparing ``semantic_value_sha256``.
    Times are normalized to exact UTC instants; no proximity bucketing occurs.
    """

    _validate_namespace(observation, namespace_id)
    _validate_semantic_surface(semantic_surface)
    if (
        not isinstance(namespace_generation, int)
        or isinstance(namespace_generation, bool)
        or namespace_generation < 1
    ):
        raise ValueError("namespace_generation must be a positive integer")
    observed_at = _observed_at(observation)

    material: dict[str, Any] = {
        "schema_version": "semantic_fact_slot.v2",
        "namespace_id": namespace_id,
        "namespace_generation": namespace_generation,
        "data_mode": observation.data_mode.value,
        "semantic_surface": semantic_surface,
        "subject_id": observation.subject_id,
        "device_id": observation.device_id,
        "device_binding_id": observation.device_binding_id,
        "binding_version": observation.binding_version,
        "observed_at_utc": _utc_text(observed_at, "observation time"),
        "observation_type": observation.observation_type.value,
        "effective_observation_type": observation.observation_type.value,
        "subtype": _slot_subtype(observation),
    }
    if isinstance(observation.payload, MissingIntervalPayload):
        material["effective_observation_type"] = (
            observation.payload.target_observation_type.value
        )
    return _sha256(material)


def semantic_value_sha256(observation: SleepObservation) -> str:
    """Hash only the stable canonical semantic payload/value.

    Observation ids, binding/envelope identity, source keys, provenance, quality,
    source channel, and receipt/request times are not value semantics and therefore
    cannot change this digest.
    """

    _observed_at(observation)
    material = {
        "schema_version": "semantic_value.v1",
        "observation_type": observation.observation_type.value,
        "payload": observation.payload.model_dump(mode="json"),
    }
    return _sha256(material)


def acquisition_channel_from_provenance(
    provenance: ObservationProvenance,
) -> AcquisitionChannel:
    """Infer a Push or Pull authority from a canonical adapter identifier.

    Adapter ids must contain exactly one delimited ``push``/``pull`` token.  An
    unknown or ambiguous id fails closed instead of inventing provenance.
    """

    adapter_id = provenance.adapter_id.casefold()
    tokens = {
        token for token in _ADAPTER_TOKEN_PATTERN.split(adapter_id) if token
    }
    channels = {
        channel
        for token, channel in (
            ("push", AcquisitionChannel.PUSH),
            ("pull", AcquisitionChannel.PULL),
        )
        if token in tokens
    }
    if len(channels) != 1:
        raise ValueError(
            "provenance adapter_id must identify exactly one acquisition channel"
        )
    return channels.pop()


def acquisition_channel_for_observation(
    observation: SleepObservation,
) -> AcquisitionChannel:
    """Infer the acquisition channel carried by an observation's provenance."""

    return acquisition_channel_from_provenance(observation.provenance)


def with_reconciliation_conflict(
    observation: SleepObservation,
) -> SleepObservation:
    """Return an immutable copy marked as having a semantic Push/Pull conflict.

    Applying the helper repeatedly is idempotent and never mutates the source
    observation or its frozen quality contract.
    """

    quality = observation.quality
    conflict_quality = quality.model_copy(
        update={
            "quality_flags": _append_once(
                quality.quality_flags,
                RECONCILIATION_CONFLICT_QUALITY_FLAG,
            ),
            "limitations": _append_once(
                quality.limitations,
                RECONCILIATION_CONFLICT_LIMITATION,
            ),
        }
    )
    return observation.model_copy(update={"quality": conflict_quality})


def _validate_namespace(
    observation: SleepObservation,
    namespace_id: str,
) -> None:
    if not isinstance(namespace_id, str):
        raise ValueError("namespace_id must be a string")
    if namespace_id != namespace_id.strip() or any(
        character.isspace() for character in namespace_id
    ):
        raise ValueError("namespace_id must not contain whitespace")
    DomainNamespace(namespace_id=namespace_id, data_mode=observation.data_mode)


def _validate_semantic_surface(semantic_surface: str) -> None:
    if not isinstance(semantic_surface, str):
        raise ValueError("semantic_surface must be a string")
    if (
        len(semantic_surface) > _MAX_SEMANTIC_SURFACE_LENGTH
        or _SURFACE_PATTERN.fullmatch(semantic_surface) is None
    ):
        raise ValueError(
            "semantic_surface must be a canonical lowercase identifier"
        )


def _observed_at(observation: SleepObservation) -> datetime:
    observed_at = observation.measurement_at or observation.event_occurred_at
    if observed_at is None:
        raise ValueError(
            "semantic reconciliation requires measurement_at or event_occurred_at"
        )
    _require_aware(observed_at, "observation time")
    return observed_at


def _slot_subtype(observation: SleepObservation) -> dict[str, Any] | None:
    payload = observation.payload
    if isinstance(payload, MissingIntervalPayload):
        subtype: dict[str, Any] = {
            "missing_target": payload.target_observation_type.value,
        }
        if payload.interval_start_at is not None:
            subtype["interval_start_at_utc"] = _utc_text(
                payload.interval_start_at,
                "missing interval start",
            )
        if payload.interval_end_at is not None:
            subtype["interval_end_at_utc"] = _utc_text(
                payload.interval_end_at,
                "missing interval end",
            )
        return subtype
    if isinstance(payload, SleepStageIntervalPayload):
        if payload.start_at is None or payload.end_at is None:
            raise ValueError(
                "sleep-stage semantic slot requires exact interval times"
            )
        return {
            "interval_start_at_utc": _utc_text(
                payload.start_at,
                "sleep-stage interval start",
            ),
            "interval_end_at_utc": _utc_text(
                payload.end_at,
                "sleep-stage interval end",
            ),
        }
    if isinstance(payload, VendorSleepProfileMetricPayload):
        return {"metric_name": payload.metric_name}
    if isinstance(payload, VendorAlertPayload):
        return {
            "alert_code": payload.alert_code,
            "alert_instance_id": payload.vendor_alert_instance_id,
        }
    if isinstance(payload, UnknownObservationPayload):
        return {"source_type": payload.source_type}
    return None


def _utc_text(value: datetime | None, label: str) -> str:
    if value is None:
        raise ValueError(f"{label} is required")
    _require_aware(value, label)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _append_once(values: tuple[str, ...], value: str) -> tuple[str, ...]:
    if value in values:
        return values
    return (*values, value)


__all__ = [
    "AcquisitionChannel",
    "RECONCILIATION_CONFLICT_LIMITATION",
    "RECONCILIATION_CONFLICT_QUALITY_FLAG",
    "acquisition_channel_for_observation",
    "acquisition_channel_from_provenance",
    "semantic_fact_slot_key",
    "semantic_value_sha256",
    "with_reconciliation_conflict",
]
