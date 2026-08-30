"""One authoritative Observation Semantics V2 normalization boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Literal, TypeAlias, cast

from pydantic import Field, ValidationError

from sleepagent.domain.contracts import (
    AdapterObservationCandidate,
    ObservationPayload,
    ObservationProvenance,
    ObservationType,
    SleepDomainContract,
    SourceKind,
)
from sleepagent.domain.observation_semantics import (
    OBSERVATION_SEMANTICS_VERSION,
    CanonicalObservationRejected,
    MovementPayloadV2,
    SemanticRejectionCategory,
    validate_movement_semantics_v2,
)
from sleepagent.domain.ontology import METRICS, ONTOLOGY_VERSION, validate_observation_ontology


UTC = timezone.utc
CANONICAL_FACTORY_VERSION = "canonical_observation_factory.v2"
CanonicalPayloadV2: TypeAlias = MovementPayloadV2 | ObservationPayload


class CanonicalObservationV2(SleepDomainContract):
    schema_version: Literal["canonical_observation.v2"] = "canonical_observation.v2"
    observation_type: ObservationType
    metric_id: str
    canonical_unit: str | None = None
    payload: CanonicalPayloadV2
    occurred_at: datetime
    aggregation_start_at: datetime | None = None
    aggregation_end_at: datetime | None = None
    source_kind: SourceKind
    provenance: ObservationProvenance
    vendor_semantic_code: str | None = None
    semantics_version: Literal["observation_semantics.v2"] = (
        OBSERVATION_SEMANTICS_VERSION
    )
    ontology_version: str
    normalizer_version: str = Field(min_length=1)
    semantic_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    transport_receipt_identity: str = Field(min_length=1)
    trusted_for_analytics: bool
    compatibility_candidate: AdapterObservationCandidate = Field(exclude=True)


class CanonicalObservationFactoryV2:
    """Validate and normalize every explicit-V2 transport candidate once."""

    version = CANONICAL_FACTORY_VERSION

    def build(
        self,
        *,
        candidate: AdapterObservationCandidate | Mapping[str, Any],
        movement_payload: MovementPayloadV2 | Mapping[str, Any] | None = None,
        normalizer_version: str,
    ) -> CanonicalObservationV2:
        typed_candidate = self._candidate(candidate)
        occurred_at = typed_candidate.measurement_at or typed_candidate.event_occurred_at
        if occurred_at is None or not _is_aware(occurred_at):
            raise CanonicalObservationRejected(
                SemanticRejectionCategory.INVALID_TIMESTAMP,
                "observation requires one timezone-aware authoritative timestamp",
            )
        occurred_at = occurred_at.astimezone(UTC)
        normalized_candidate = typed_candidate.model_copy(
            update={
                "request_signed_at": _utc_or_none(typed_candidate.request_signed_at),
                "measurement_at": _utc_or_none(typed_candidate.measurement_at),
                "event_occurred_at": _utc_or_none(typed_candidate.event_occurred_at),
                "received_at": _utc_or_reject(typed_candidate.received_at),
            }
        )

        if typed_candidate.observation_type is ObservationType.MOVEMENT:
            semantic_payload = self._movement_payload(movement_payload)
            semantic_payload = semantic_payload.model_copy(
                update={
                    "aggregation_start_at": _utc_or_none(
                        semantic_payload.aggregation_start_at
                    ),
                    "aggregation_end_at": _utc_or_none(
                        semantic_payload.aggregation_end_at
                    ),
                }
            )
            movement_definition = validate_movement_semantics_v2(
                semantic_payload,
                source_kind=typed_candidate.source_kind,
            )
            metric_id = semantic_payload.metric_id.value
            canonical_unit = semantic_payload.unit
            aggregation_start = semantic_payload.aggregation_start_at
            aggregation_end = semantic_payload.aggregation_end_at
            vendor_semantic_code = semantic_payload.vendor_semantic_code
            trusted = movement_definition.trusted_for_analytics
            ontology_version = OBSERVATION_SEMANTICS_VERSION
            payload: CanonicalPayloadV2 = semantic_payload
        else:
            self._validate_existing_semantics(typed_candidate)
            existing_definition = METRICS.get(typed_candidate.observation_type)
            metric_id = (
                existing_definition.metric_name
                if existing_definition is not None
                else typed_candidate.observation_type.value
            )
            canonical_unit = (
                existing_definition.canonical_unit
                if existing_definition is not None
                else None
            )
            aggregation_start = None
            aggregation_end = None
            vendor_semantic_code = None
            trusted = True
            ontology_version = ONTOLOGY_VERSION
            payload = typed_candidate.payload

        semantic_identity = _semantic_identity(
            candidate=normalized_candidate,
            metric_id=metric_id,
            canonical_unit=canonical_unit,
            payload=payload,
            occurred_at=occurred_at,
            aggregation_start_at=aggregation_start,
            aggregation_end_at=aggregation_end,
            vendor_semantic_code=vendor_semantic_code,
        )
        return CanonicalObservationV2(
            observation_type=typed_candidate.observation_type,
            metric_id=metric_id,
            canonical_unit=canonical_unit,
            payload=payload,
            occurred_at=occurred_at,
            aggregation_start_at=aggregation_start,
            aggregation_end_at=aggregation_end,
            source_kind=typed_candidate.source_kind,
            provenance=typed_candidate.provenance,
            vendor_semantic_code=vendor_semantic_code,
            ontology_version=ontology_version,
            normalizer_version=normalizer_version,
            semantic_identity=semantic_identity,
            transport_receipt_identity=typed_candidate.idempotency_key,
            trusted_for_analytics=trusted,
            compatibility_candidate=normalized_candidate,
        )

    @staticmethod
    def _candidate(
        candidate: AdapterObservationCandidate | Mapping[str, Any],
    ) -> AdapterObservationCandidate:
        if isinstance(candidate, AdapterObservationCandidate):
            return candidate
        provider_id = candidate.get("provider_id")
        account_id = candidate.get("provider_account_id")
        provenance = candidate.get("provenance")
        if isinstance(provenance, Mapping) and (
            provenance.get("provider_id") != provider_id
            or provenance.get("provider_account_id") != account_id
        ):
            raise CanonicalObservationRejected(
                SemanticRejectionCategory.INVALID_SOURCE_PROVENANCE,
                "candidate and provenance provider identities must match",
            )
        for field in (
            "request_signed_at",
            "measurement_at",
            "event_occurred_at",
            "received_at",
        ):
            value = candidate.get(field)
            if isinstance(value, datetime) and not _is_aware(value):
                raise CanonicalObservationRejected(
                    SemanticRejectionCategory.INVALID_TIMESTAMP,
                    f"{field} must be timezone-aware",
                )
        try:
            return AdapterObservationCandidate.model_validate(candidate)
        except ValidationError as exc:
            raise CanonicalObservationRejected(
                SemanticRejectionCategory.INVALID_SCHEMA,
                "adapter candidate does not match its versioned schema",
            ) from exc

    @staticmethod
    def _movement_payload(
        payload: MovementPayloadV2 | Mapping[str, Any] | None,
    ) -> MovementPayloadV2:
        if payload is None:
            raise CanonicalObservationRejected(
                SemanticRejectionCategory.UNSUPPORTED_VENDOR_SEMANTICS,
                "movement requires an explicit V2 vendor-to-metric mapping",
            )
        if isinstance(payload, MovementPayloadV2):
            return payload
        try:
            return cast(MovementPayloadV2, MovementPayloadV2.model_validate(payload))
        except ValidationError as exc:
            raise CanonicalObservationRejected(
                SemanticRejectionCategory.INVALID_SCHEMA,
                "movement payload does not match movement_payload.v2",
            ) from exc

    @staticmethod
    def _validate_existing_semantics(candidate: AdapterObservationCandidate) -> None:
        try:
            validate_observation_ontology(
                observation_type=candidate.observation_type,
                payload=candidate.payload,
                source_kind=candidate.source_kind,
            )
        except ValueError as exc:
            detail = str(exc)
            category = (
                SemanticRejectionCategory.INVALID_SOURCE_PROVENANCE
                if "source" in detail
                else SemanticRejectionCategory.INVALID_UNIT
            )
            raise CanonicalObservationRejected(category, detail) from exc


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _utc_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _utc_or_reject(value)


def _utc_or_reject(value: datetime) -> datetime:
    if not _is_aware(value):
        raise CanonicalObservationRejected(
            SemanticRejectionCategory.INVALID_TIMESTAMP,
            "canonical timestamps must be timezone-aware",
        )
    return value.astimezone(UTC)


def _semantic_identity(
    *,
    candidate: AdapterObservationCandidate,
    metric_id: str,
    canonical_unit: str | None,
    payload: CanonicalPayloadV2,
    occurred_at: datetime,
    aggregation_start_at: datetime | None,
    aggregation_end_at: datetime | None,
    vendor_semantic_code: str | None,
) -> str:
    material = {
        "provider_id": candidate.provider_id,
        "provider_account_id": candidate.provider_account_id,
        "provider_device": candidate.provider_device.model_dump(mode="json"),
        "observation_type": candidate.observation_type.value,
        "metric_id": metric_id,
        "canonical_unit": canonical_unit,
        "value": getattr(payload, "value", None),
        "occurred_at": occurred_at.isoformat(),
        "aggregation_start_at": (
            None if aggregation_start_at is None else aggregation_start_at.isoformat()
        ),
        "aggregation_end_at": (
            None if aggregation_end_at is None else aggregation_end_at.isoformat()
        ),
        "source_kind": candidate.source_kind.value,
        "vendor_semantic_code": vendor_semantic_code,
    }
    encoded = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "CANONICAL_FACTORY_VERSION",
    "CanonicalObservationFactoryV2",
    "CanonicalObservationV2",
]
