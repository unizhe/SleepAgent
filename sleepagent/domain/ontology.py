# 本模块负责睡眠领域规则与数据语义，不依赖 HTTP 或进程装配。
"""Versioned canonical metric, unit, and human/source vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sleepagent.domain.contracts import (
    AvailabilityState,
    ObservationType,
    SourceKind,
    VendorSleepProfileMetricPayload,
)


ONTOLOGY_VERSION = "sleep_observation_ontology.v2"


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    metric_name: str
    canonical_unit: str
    allowed_source_kinds: frozenset[SourceKind]
    payload_unit_required: bool = True
    aggregation_window_required: bool = False
    numeric_minimum: float | None = None
    numeric_maximum: float | None = None
    integer_value_required: bool = False


METRICS: Mapping[ObservationType, MetricDefinition] = {
    ObservationType.HEART_RATE: MetricDefinition(
        "heart_rate", "beats_per_minute", frozenset({SourceKind.DEVICE_MEASURED})
    ),
    ObservationType.RESPIRATORY_RATE: MetricDefinition(
        "respiratory_rate",
        "breaths_per_minute",
        frozenset({SourceKind.DEVICE_MEASURED}),
    ),
    ObservationType.MOVEMENT: MetricDefinition(
        "movement", "index", frozenset({SourceKind.DEVICE_MEASURED})
    ),
    ObservationType.SLEEP_STAGE_INTERVAL: MetricDefinition(
        "sleep_stage",
        "stage_interval",
        frozenset({SourceKind.VENDOR_DERIVED}),
        payload_unit_required=False,
    ),
    ObservationType.BED_EXIT: MetricDefinition(
        "bed_exit_event",
        "event",
        frozenset({SourceKind.VENDOR_DERIVED}),
        payload_unit_required=False,
    ),
    ObservationType.MISSING_INTERVAL: MetricDefinition(
        "missing_interval",
        "interval",
        frozenset({SourceKind.DEVICE_MEASURED, SourceKind.VENDOR_DERIVED}),
        payload_unit_required=False,
    ),
}


VENDOR_SLEEP_PROFILE_METRICS: Mapping[str, MetricDefinition] = {
    "vendor_time_in_bed_minutes": MetricDefinition(
        "vendor_time_in_bed_minutes",
        "minutes",
        frozenset({SourceKind.VENDOR_DERIVED}),
        numeric_minimum=0,
    ),
    "heart_rate_mean": MetricDefinition(
        "heart_rate_mean",
        "beats_per_minute",
        frozenset({SourceKind.VENDOR_DERIVED}),
        aggregation_window_required=True,
        numeric_minimum=1,
        numeric_maximum=300,
        integer_value_required=True,
    ),
    "respiratory_rate_mean": MetricDefinition(
        "respiratory_rate_mean",
        "breaths_per_minute",
        frozenset({SourceKind.VENDOR_DERIVED}),
        aggregation_window_required=True,
        numeric_minimum=1,
        numeric_maximum=150,
        integer_value_required=True,
    ),
    "movement_event_total": MetricDefinition(
        "movement_event_total",
        "count",
        frozenset({SourceKind.VENDOR_DERIVED}),
        aggregation_window_required=True,
        numeric_minimum=0,
        integer_value_required=True,
    ),
    "deep_sleep_ratio": MetricDefinition(
        "deep_sleep_ratio",
        "percent",
        frozenset({SourceKind.VENDOR_DERIVED}),
        aggregation_window_required=True,
        numeric_minimum=0,
        numeric_maximum=100,
    ),
    "sleep_efficiency": MetricDefinition(
        "sleep_efficiency",
        "percent",
        frozenset({SourceKind.VENDOR_DERIVED}),
        aggregation_window_required=True,
        numeric_minimum=0,
        numeric_maximum=100,
    ),
}


# Source authority is distinct from numeric metric/unit authority.  Perceptor
# Push exposes the device's direct OnBed state, while Pull exposes vendor status
# classifications (smbdFlag/probStatus).  Both are legitimate bed-presence
# observations, but their provenance must remain distinguishable downstream.
OBSERVATION_SOURCE_KINDS: Mapping[ObservationType, frozenset[SourceKind]] = {
    ObservationType.BED_PRESENCE: frozenset(
        {SourceKind.DEVICE_MEASURED, SourceKind.VENDOR_DERIVED}
    ),
    ObservationType.SLEEP_STAGE_INTERVAL: frozenset({SourceKind.VENDOR_DERIVED}),
    ObservationType.BED_EXIT: frozenset({SourceKind.VENDOR_DERIVED}),
    ObservationType.MISSING_INTERVAL: frozenset(
        {SourceKind.DEVICE_MEASURED, SourceKind.VENDOR_DERIVED}
    ),
    ObservationType.VENDOR_SLEEP_PROFILE_METRIC: frozenset(
        {SourceKind.VENDOR_DERIVED}
    ),
    ObservationType.VENDOR_ALERT: frozenset({SourceKind.VENDOR_DERIVED}),
}


SOURCE_ALIASES: Mapping[str, SourceKind] = {
    "device": SourceKind.DEVICE_MEASURED,
    "device_measured": SourceKind.DEVICE_MEASURED,
    "objective_sensor": SourceKind.DEVICE_MEASURED,
    "vendor": SourceKind.VENDOR_DERIVED,
    "vendor_derived": SourceKind.VENDOR_DERIVED,
    "elder_self_report": SourceKind.USER_REPORTED,
    "self_report": SourceKind.USER_REPORTED,
    "user_reported": SourceKind.USER_REPORTED,
    "family_observation": SourceKind.EXTERNALLY_REPORTED,
    "family_report": SourceKind.EXTERNALLY_REPORTED,
    "externally_reported": SourceKind.EXTERNALLY_REPORTED,
    "model_derived": SourceKind.FUTURE_MODEL_DERIVED,
    "future_model_derived": SourceKind.FUTURE_MODEL_DERIVED,
}


def canonical_source_kind(value: str | SourceKind) -> SourceKind:
    if isinstance(value, SourceKind):
        return value
    normalized = value.strip().casefold()
    try:
        return SOURCE_ALIASES[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported source vocabulary: {value!r}") from exc


def validate_observation_ontology(
    *,
    observation_type: ObservationType,
    payload: Any,
    source_kind: SourceKind,
) -> MetricDefinition | None:
    allowed_sources = OBSERVATION_SOURCE_KINDS.get(
        observation_type,
        frozenset({SourceKind.DEVICE_MEASURED}),
    )
    if source_kind not in allowed_sources:
        expected = sorted(item.value for item in allowed_sources)
        if len(expected) == 1:
            raise ValueError(
                f"{observation_type.value} requires {expected[0]} source"
            )
        raise ValueError(
            f"{observation_type.value} requires one of [{', '.join(expected)}] sources"
        )
    if isinstance(payload, VendorSleepProfileMetricPayload):
        definition = VENDOR_SLEEP_PROFILE_METRICS.get(payload.metric_name)
        if definition is None:
            raise ValueError("unsupported vendor sleep profile metric")
    else:
        definition = METRICS.get(observation_type)
    if definition is None:
        return None
    if source_kind not in definition.allowed_source_kinds:
        raise ValueError("metric source is outside the canonical ontology")
    unit = getattr(payload, "unit", None)
    metric_name = getattr(payload, "metric_name", definition.metric_name)
    if metric_name != definition.metric_name or (
        definition.payload_unit_required and unit != definition.canonical_unit
    ):
        raise ValueError(
            f"unsupported {observation_type.value} metric/unit pair"
        )
    if isinstance(payload, VendorSleepProfileMetricPayload):
        if payload.value_state is not AvailabilityState.KNOWN:
            raise ValueError("vendor sleep profile metric value is not known")
        value = payload.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("vendor sleep profile metric value must be numeric")
        number = float(value)
        if number != number or number in {float("inf"), float("-inf")}:
            raise ValueError("vendor sleep profile metric value must be finite")
        if definition.integer_value_required and not number.is_integer():
            raise ValueError("vendor sleep profile metric value must be an integer")
        if (
            definition.numeric_minimum is not None
            and number < definition.numeric_minimum
        ):
            raise ValueError("vendor sleep profile metric value is below its minimum")
        if (
            definition.numeric_maximum is not None
            and number > definition.numeric_maximum
        ):
            raise ValueError("vendor sleep profile metric value exceeds its maximum")
        has_window = (
            payload.aggregation_start_at is not None
            and payload.aggregation_end_at is not None
        )
        if definition.aggregation_window_required and not has_window:
            raise ValueError("vendor sleep profile metric requires aggregation window")
    return definition


__all__ = [
    "METRICS",
    "VENDOR_SLEEP_PROFILE_METRICS",
    "OBSERVATION_SOURCE_KINDS",
    "ONTOLOGY_VERSION",
    "MetricDefinition",
    "SOURCE_ALIASES",
    "canonical_source_kind",
    "validate_observation_ontology",
]
