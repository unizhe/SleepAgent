# 本模块负责睡眠领域规则与数据语义，不依赖 HTTP 或进程装配。
"""Versioned canonical metric, unit, and human/source vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sleepagent.domain.contracts import ObservationType, SourceKind


ONTOLOGY_VERSION = "sleep_observation_ontology.v1"


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    metric_name: str
    canonical_unit: str
    allowed_source_kinds: frozenset[SourceKind]


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
    ObservationType.VENDOR_SLEEP_PROFILE_METRIC: MetricDefinition(
        "vendor_time_in_bed_minutes",
        "minutes",
        frozenset({SourceKind.VENDOR_DERIVED}),
    ),
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
) -> None:
    expected_source = (
        SourceKind.VENDOR_DERIVED
        if observation_type
        in {
            ObservationType.SLEEP_STAGE_INTERVAL,
            ObservationType.VENDOR_SLEEP_PROFILE_METRIC,
            ObservationType.VENDOR_ALERT,
        }
        else SourceKind.DEVICE_MEASURED
    )
    if source_kind != expected_source:
        raise ValueError(
            f"{observation_type.value} requires {expected_source.value} source"
        )
    definition = METRICS.get(observation_type)
    if definition is None:
        return
    if source_kind not in definition.allowed_source_kinds:
        raise ValueError("metric source is outside the canonical ontology")
    unit = getattr(payload, "unit", None)
    metric_name = getattr(payload, "metric_name", definition.metric_name)
    if metric_name != definition.metric_name or unit != definition.canonical_unit:
        raise ValueError(
            f"unsupported {observation_type.value} metric/unit pair"
        )


__all__ = [
    "METRICS",
    "ONTOLOGY_VERSION",
    "MetricDefinition",
    "SOURCE_ALIASES",
    "canonical_source_kind",
    "validate_observation_ontology",
]
