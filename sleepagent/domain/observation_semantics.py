"""Observation Semantics V2 movement contracts and authoritative registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Final, Literal, Mapping

from pydantic import Field, model_validator

from sleepagent.domain.contracts import NonEmptyStr, SleepDomainContract, SourceKind


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
    "SemanticRejectionCategory",
    "movement_metric_definition",
    "validate_movement_semantics_v2",
]
