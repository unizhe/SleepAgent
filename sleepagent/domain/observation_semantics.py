"""Observation Semantics V2 movement contracts and authoritative registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from statistics import median
from typing import Any, Final, Iterable, Literal, Mapping

from pydantic import Field, model_validator

from sleepagent.domain.contracts import (
    NonEmptyStr,
    ObservationProvenance,
    ObservationType,
    SleepDomainContract,
    SourceKind,
)


OBSERVATION_SEMANTICS_VERSION: Final[Literal["observation_semantics.v2"]] = (
    "observation_semantics.v2"
)


class MovementMetricId(str, Enum):
    MOVEMENT_INDEX = "movement_index"
    MOVEMENT_EVENT_COUNT = "movement_event_count"
    LEGACY_AMBIGUOUS_MOVEMENT = "legacy_ambiguous_movement"


class SemanticRejectionCategory(str, Enum):
    INVALID_SCHEMA = "invalid_schema"
    UNSUPPORTED_METRIC = "unsupported_metric"
    INVALID_UNIT = "invalid_unit"
    INVALID_VALUE = "invalid_value"
    MISSING_AGGREGATION_WINDOW = "missing_aggregation_window"
    INVALID_AGGREGATION_WINDOW = "invalid_aggregation_window"
    INVALID_SOURCE_PROVENANCE = "invalid_source_provenance"
    INVALID_TIMESTAMP = "invalid_timestamp"
    UNSUPPORTED_VENDOR_SEMANTICS = "unsupported_vendor_semantics"


class CanonicalObservationRejected(ValueError):
    """One stable domain rejection with a machine-readable semantic category."""

    def __init__(self, category: SemanticRejectionCategory, detail: str) -> None:
        self.category = category
        self.detail = detail
        super().__init__(f"{category.value}: {detail}")


class MovementPayloadV2(SleepDomainContract):
    schema_version: Literal["movement_payload.v2"] = "movement_payload.v2"
    metric_id: MovementMetricId
    value: int | float = Field(ge=0)
    unit: NonEmptyStr
    aggregation_start_at: datetime | None = None
    aggregation_end_at: datetime | None = None
    vendor_semantic_code: str | None = None
    semantics_version: Literal["observation_semantics.v2"] = (
        OBSERVATION_SEMANTICS_VERSION
    )

    @model_validator(mode="after")
    def reject_boolean_value(self) -> "MovementPayloadV2":
        if isinstance(self.value, bool):
            raise ValueError("movement value must be numeric, not boolean")
        return self


class PersistedObservationSemanticsV2(SleepDomainContract):
    """Storage-neutral V2 semantic authority for one compatibility row."""

    schema_version: Literal["canonical_observation.v2"] = (
        "canonical_observation.v2"
    )
    observation_type: ObservationType
    metric_id: NonEmptyStr
    semantic_payload: dict[str, Any]
    canonical_unit: str | None = None
    occurred_at: datetime
    aggregation_start_at: datetime | None = None
    aggregation_end_at: datetime | None = None
    source_kind: SourceKind
    provenance: ObservationProvenance
    vendor_semantic_code: str | None = None
    semantics_version: Literal["observation_semantics.v2"] = (
        OBSERVATION_SEMANTICS_VERSION
    )
    ontology_version: NonEmptyStr
    normalizer_version: NonEmptyStr
    semantic_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    transport_receipt_identity: NonEmptyStr
    trusted_for_analytics: bool
    upcast_status: Literal[
        "native_v2", "legacy_classified", "legacy_ambiguous"
    ]
    classification_evidence: dict[str, Any]

    @model_validator(mode="after")
    def enforce_movement_storage_contract(
        self,
    ) -> "PersistedObservationSemanticsV2":
        if (self.aggregation_start_at is None) != (
            self.aggregation_end_at is None
        ):
            raise ValueError("aggregation window requires both boundaries")
        if (
            self.aggregation_start_at is not None
            and self.aggregation_end_at is not None
            and self.aggregation_end_at <= self.aggregation_start_at
        ):
            raise ValueError("aggregation window must be positive")
        if self.observation_type is not ObservationType.MOVEMENT:
            if self.metric_id in {item.value for item in MovementMetricId}:
                raise ValueError("movement metric requires movement observation")
            if (
                not self.trusted_for_analytics
                or self.upcast_status == "legacy_ambiguous"
            ):
                raise ValueError("non-movement V2 semantics must remain trusted")
            return self
        expected = {
            MovementMetricId.MOVEMENT_INDEX.value: (
                "vendor_index",
                SourceKind.DEVICE_MEASURED,
                True,
            ),
            MovementMetricId.MOVEMENT_EVENT_COUNT.value: (
                "count",
                SourceKind.VENDOR_DERIVED,
                True,
            ),
            MovementMetricId.LEGACY_AMBIGUOUS_MOVEMENT.value: (
                "legacy_unknown",
                None,
                False,
            ),
        }.get(self.metric_id)
        if expected is None:
            raise ValueError("movement metric is not registered")
        unit, source_kind, trusted = expected
        if self.canonical_unit != unit or self.trusted_for_analytics is not trusted:
            raise ValueError("movement storage authority contradicts metric")
        if source_kind is not None and self.source_kind is not source_kind:
            raise ValueError("movement source authority contradicts metric")
        if self.metric_id == MovementMetricId.MOVEMENT_EVENT_COUNT.value and (
            self.aggregation_start_at is None
            or self.aggregation_end_at is None
        ):
            raise ValueError("movement event count requires a window")
        if self.metric_id == MovementMetricId.MOVEMENT_INDEX.value and (
            self.aggregation_start_at is not None
            or self.aggregation_end_at is not None
        ):
            raise ValueError("movement index cannot carry a count window")
        if self.metric_id == MovementMetricId.LEGACY_AMBIGUOUS_MOVEMENT.value:
            if self.upcast_status != "legacy_ambiguous":
                raise ValueError("ambiguous movement must remain an explicit upcast")
            if self.source_kind not in {
                SourceKind.DEVICE_MEASURED,
                SourceKind.VENDOR_DERIVED,
            }:
                raise ValueError("ambiguous movement has unsupported provenance")
        return self


def aggregate_movement_semantics_v2(
    facts: Iterable[Mapping[str, Any]],
    *,
    expected_start_at: datetime | None = None,
    expected_end_at: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate compatible V2 movement facts without semantic coercion."""

    indexes: list[float] = []
    counts: list[tuple[datetime, datetime, int]] = []
    ambiguous_count = 0
    unclassified_count = 0
    invalid_count = 0
    for fact in facts:
        metric_id = fact.get("metric_id")
        trusted = fact.get("trusted_for_analytics") is True
        value = fact.get("value")
        if metric_id == MovementMetricId.MOVEMENT_INDEX.value:
            if (
                trusted
                and fact.get("unit") == "vendor_index"
                and fact.get("source_kind") == SourceKind.DEVICE_MEASURED.value
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                indexes.append(float(value))
            else:
                invalid_count += 1
        elif metric_id == MovementMetricId.MOVEMENT_EVENT_COUNT.value:
            start = fact.get("aggregation_start_at")
            end = fact.get("aggregation_end_at")
            if (
                trusted
                and fact.get("unit") == "count"
                and fact.get("source_kind") == SourceKind.VENDOR_DERIVED.value
                and isinstance(value, int)
                and not isinstance(value, bool)
                and isinstance(start, datetime)
                and isinstance(end, datetime)
                and end > start
            ):
                counts.append((start, end, value))
            else:
                invalid_count += 1
        elif metric_id == MovementMetricId.LEGACY_AMBIGUOUS_MOVEMENT.value:
            ambiguous_count += 1
        else:
            unclassified_count += 1

    by_window: dict[tuple[datetime, datetime], list[int]] = {}
    for start, end, value in counts:
        by_window.setdefault((start, end), []).append(value)
    duplicate_observation_count = sum(
        len(values) - 1
        for values in by_window.values()
        if len(values) > 1 and len(set(values)) == 1
    )
    conflicting_window_observation_count = sum(
        len(values)
        for values in by_window.values()
        if len(set(values)) > 1
    )
    unique = [
        (start, end, values[0])
        for (start, end), values in by_window.items()
        if len(set(values)) == 1
    ]
    overlapping: set[int] = set()
    for left in range(len(unique)):
        for right in range(left + 1, len(unique)):
            if (
                unique[left][0] < unique[right][1]
                and unique[right][0] < unique[left][1]
            ):
                overlapping.update((left, right))
    disjoint = [item for index, item in enumerate(unique) if index not in overlapping]
    disjoint.sort(key=lambda item: (item[0], item[1]))
    hourly = [
        item
        for item in disjoint
        if (item[1] - item[0]).total_seconds() == 3600
    ]
    coverage_seconds = sum((end - start).total_seconds() for start, end, _ in disjoint)
    has_gap = any(
        right[0] > left[1] for left, right in zip(disjoint, disjoint[1:])
    )
    outside_expected = False
    if expected_start_at is not None and expected_end_at is not None:
        outside_expected = (
            not disjoint
            or disjoint[0][0] > expected_start_at
            or disjoint[-1][1] < expected_end_at
        )
    partial = bool(
        conflicting_window_observation_count
        or overlapping
        or has_gap
        or outside_expected
        or invalid_count
    )
    reason_codes: list[str] = []
    if ambiguous_count or unclassified_count:
        reason_codes.append("AMBIGUOUS_MOVEMENT_EXCLUDED")
    if duplicate_observation_count:
        reason_codes.append("DUPLICATE_COUNT_OBSERVATION_DEDUPLICATED")
    if conflicting_window_observation_count:
        reason_codes.append("CONFLICTING_COUNT_WINDOW_EXCLUDED")
    if overlapping:
        reason_codes.append("OVERLAPPING_COUNT_WINDOWS_EXCLUDED")
    if len(hourly) != len(disjoint):
        reason_codes.append("NON_HOURLY_COUNT_EXCLUDED_FROM_HOURLY_MAX")
    if partial:
        reason_codes.append("PARTIAL_COUNT_COVERAGE")
    if invalid_count:
        reason_codes.append("INVALID_MOVEMENT_SEMANTICS_EXCLUDED")
    return {
        "schema_version": "movement_analytics.v2",
        "movement_index": {
            "unit": "vendor_index",
            "sample_count": len(indexes),
            "mean": None if not indexes else sum(indexes) / len(indexes),
            "median": None if not indexes else float(median(indexes)),
        },
        "movement_event_count": {
            "unit": "count",
            "total": None if not disjoint else sum(item[2] for item in disjoint),
            "hourly_max": None if not hourly else max(item[2] for item in hourly),
            "hourly_window_count": len(hourly),
            "non_hourly_window_count": len(disjoint) - len(hourly),
            "summed_window_count": len(disjoint),
            "overlapping_window_count": len(overlapping),
            "duplicate_observation_count": duplicate_observation_count,
            "conflicting_window_observation_count": (
                conflicting_window_observation_count
            ),
            "coverage_minutes": round(coverage_seconds / 60, 1),
            "partial_coverage": partial,
        },
        "excluded": {
            "legacy_ambiguous_count": ambiguous_count,
            "unclassified_v1_count": unclassified_count,
            "invalid_semantics_count": invalid_count,
        },
        "threshold_status": "SEMANTIC_THRESHOLD_UNRESOLVED",
        "reason_codes": reason_codes,
    }


@dataclass(frozen=True, slots=True)
class MovementMetricDefinition:
    metric_id: MovementMetricId
    allowed_units: frozenset[str]
    allowed_source_kinds: frozenset[SourceKind]
    aggregation_window_required: bool
    payload_schema: str
    semantics_version: str
    trusted_for_analytics: bool


MOVEMENT_METRICS: Mapping[MovementMetricId, MovementMetricDefinition] = {
    MovementMetricId.MOVEMENT_INDEX: MovementMetricDefinition(
        metric_id=MovementMetricId.MOVEMENT_INDEX,
        allowed_units=frozenset({"vendor_index"}),
        allowed_source_kinds=frozenset({SourceKind.DEVICE_MEASURED}),
        aggregation_window_required=False,
        payload_schema="movement_payload.v2",
        semantics_version=OBSERVATION_SEMANTICS_VERSION,
        trusted_for_analytics=True,
    ),
    MovementMetricId.MOVEMENT_EVENT_COUNT: MovementMetricDefinition(
        metric_id=MovementMetricId.MOVEMENT_EVENT_COUNT,
        allowed_units=frozenset({"count"}),
        allowed_source_kinds=frozenset({SourceKind.VENDOR_DERIVED}),
        aggregation_window_required=True,
        payload_schema="movement_payload.v2",
        semantics_version=OBSERVATION_SEMANTICS_VERSION,
        trusted_for_analytics=True,
    ),
    MovementMetricId.LEGACY_AMBIGUOUS_MOVEMENT: MovementMetricDefinition(
        metric_id=MovementMetricId.LEGACY_AMBIGUOUS_MOVEMENT,
        allowed_units=frozenset({"legacy_unknown"}),
        allowed_source_kinds=frozenset(
            {SourceKind.DEVICE_MEASURED, SourceKind.VENDOR_DERIVED}
        ),
        aggregation_window_required=False,
        payload_schema="movement_payload.v2",
        semantics_version=OBSERVATION_SEMANTICS_VERSION,
        trusted_for_analytics=False,
    ),
}


def validate_movement_semantics_v2(
    payload: MovementPayloadV2,
    *,
    source_kind: SourceKind,
) -> MovementMetricDefinition:
    definition = MOVEMENT_METRICS.get(payload.metric_id)
    if definition is None:  # pragma: no cover - enum construction is fail-closed
        raise CanonicalObservationRejected(
            SemanticRejectionCategory.UNSUPPORTED_METRIC,
            "movement metric is not registered",
        )
    if not definition.trusted_for_analytics:
        raise CanonicalObservationRejected(
            SemanticRejectionCategory.UNSUPPORTED_VENDOR_SEMANTICS,
            "legacy ambiguous movement is audit-only and cannot enter V2 ingestion",
        )
    if payload.unit not in definition.allowed_units:
        raise CanonicalObservationRejected(
            SemanticRejectionCategory.INVALID_UNIT,
            f"{payload.metric_id.value} does not allow unit {payload.unit!r}",
        )
    if source_kind not in definition.allowed_source_kinds:
        raise CanonicalObservationRejected(
            SemanticRejectionCategory.INVALID_SOURCE_PROVENANCE,
            f"{payload.metric_id.value} does not allow source {source_kind.value!r}",
        )
    start = payload.aggregation_start_at
    end = payload.aggregation_end_at
    if definition.aggregation_window_required and (start is None or end is None):
        raise CanonicalObservationRejected(
            SemanticRejectionCategory.MISSING_AGGREGATION_WINDOW,
            f"{payload.metric_id.value} requires both aggregation boundaries",
        )
    if (start is None) != (end is None):
        raise CanonicalObservationRejected(
            SemanticRejectionCategory.INVALID_AGGREGATION_WINDOW,
            "aggregation window requires both boundaries",
        )
    if start is not None and end is not None and end <= start:
        raise CanonicalObservationRejected(
            SemanticRejectionCategory.INVALID_AGGREGATION_WINDOW,
            "aggregation end must be after aggregation start",
        )
    if payload.metric_id is MovementMetricId.MOVEMENT_EVENT_COUNT and not (
        isinstance(payload.value, int) and not isinstance(payload.value, bool)
    ):
        raise CanonicalObservationRejected(
            SemanticRejectionCategory.INVALID_VALUE,
            "movement_event_count requires a non-negative integer value",
        )
    return definition


def movement_metric_definition(
    metric_id: MovementMetricId,
) -> MovementMetricDefinition:
    return MOVEMENT_METRICS[metric_id]


__all__ = [
    "CanonicalObservationRejected",
    "MOVEMENT_METRICS",
    "MovementMetricDefinition",
    "MovementMetricId",
    "MovementPayloadV2",
    "OBSERVATION_SEMANTICS_VERSION",
    "PersistedObservationSemanticsV2",
    "SemanticRejectionCategory",
    "aggregate_movement_semantics_v2",
    "movement_metric_definition",
    "validate_movement_semantics_v2",
]
