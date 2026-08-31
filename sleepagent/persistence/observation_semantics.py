"""PostgreSQL adapter for the storage-neutral Observation Semantics V2 model."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Protocol

from sleepagent.domain.canonical_observation import (
    CanonicalObservationV2,
    legacy_missing_interval_semantic_identity_v2,
)
from sleepagent.domain.observation_semantics import PersistedObservationSemanticsV2


class SemanticPersistenceScope(Protocol):
    namespace_id: str
    data_mode: Any


class ObservationSemanticsPersistenceError(RuntimeError):
    """A durable semantic identity conflicts with already committed truth."""


def native_semantics_record(
    canonical: CanonicalObservationV2,
) -> PersistedObservationSemanticsV2:
    return PersistedObservationSemanticsV2(
        observation_type=canonical.observation_type,
        metric_id=canonical.metric_id,
        semantic_payload=canonical.payload.model_dump(mode="json"),
        canonical_unit=canonical.canonical_unit,
        occurred_at=canonical.occurred_at,
        aggregation_start_at=canonical.aggregation_start_at,
        aggregation_end_at=canonical.aggregation_end_at,
        source_kind=canonical.source_kind,
        provenance=canonical.provenance,
        vendor_semantic_code=canonical.vendor_semantic_code,
        ontology_version=canonical.ontology_version,
        normalizer_version=canonical.normalizer_version,
        semantic_identity=canonical.semantic_identity,
        transport_receipt_identity=canonical.transport_receipt_identity,
        trusted_for_analytics=canonical.trusted_for_analytics,
        upcast_status="native_v2",
        classification_evidence={
            "schema_version": "semantic_classification_evidence.v1",
            "classification_source": "canonical_observation_factory.v2",
            "evidence_codes": ["EXPLICIT_V2_TRANSPORT_MAPPING"],
        },
    )


def persist_observation_semantics_v2(
    cursor: Any,
    scope: SemanticPersistenceScope,
    *,
    observation_id: str,
    subject_id: str,
    semantics: PersistedObservationSemanticsV2 | CanonicalObservationV2,
    created_at: datetime,
) -> bool:
    """Insert one immutable semantic row or validate an exact retry."""

    record = (
        native_semantics_record(semantics)
        if isinstance(semantics, CanonicalObservationV2)
        else semantics
    )
    values = _record_values(record)
    cursor.execute(
        """
        INSERT INTO public.sleep_domain_observation_semantics_v2 (
          observation_id, namespace_id, data_mode, subject_id,
          schema_version, observation_type, metric_id,
          semantic_payload_json, canonical_unit, occurred_at,
          aggregation_start_at, aggregation_end_at, source_kind,
          provenance_json, vendor_semantic_code, semantics_version,
          ontology_version, normalizer_version, semantic_identity,
          transport_receipt_identity, trusted_for_analytics, upcast_status,
          classification_evidence, created_at
        ) VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s,
          %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
        )
        ON CONFLICT (observation_id, namespace_id, data_mode) DO NOTHING
        """,
        (
            observation_id,
            scope.namespace_id,
            scope.data_mode,
            subject_id,
            *values,
            created_at,
        ),
    )
    if cursor.rowcount == 1:
        return True
    cursor.execute(
        """
        SELECT schema_version, observation_type, metric_id,
               semantic_payload_json, canonical_unit, occurred_at,
               aggregation_start_at, aggregation_end_at, source_kind,
               provenance_json, vendor_semantic_code, semantics_version,
               ontology_version, normalizer_version, semantic_identity,
               transport_receipt_identity, trusted_for_analytics,
               upcast_status, classification_evidence
        FROM public.sleep_domain_observation_semantics_v2
        WHERE observation_id = %s AND namespace_id = %s AND data_mode = %s
          AND subject_id = %s
        """,
        (observation_id, scope.namespace_id, scope.data_mode, subject_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise ObservationSemanticsPersistenceError(
            "observation semantic identity or content collision"
        )
    persisted_values = _database_values(row)
    candidate_values = _comparable_values(record)
    if _semantic_core(persisted_values) == _semantic_core(candidate_values):
        return False
    legacy_identity = (
        legacy_missing_interval_semantic_identity_v2(semantics)
        if isinstance(semantics, CanonicalObservationV2)
        else None
    )
    if (
        legacy_identity is None
        or persisted_values[14] != legacy_identity
        or _semantic_content_core(persisted_values)
        != _semantic_content_core(candidate_values)
    ):
        raise ObservationSemanticsPersistenceError(
            "observation semantic identity or content collision"
        )
    return False


def _record_values(record: PersistedObservationSemanticsV2) -> tuple[Any, ...]:
    value = record.model_dump(mode="json")
    return (
        value["schema_version"],
        value["observation_type"],
        value["metric_id"],
        _json(value["semantic_payload"]),
        value["canonical_unit"],
        record.occurred_at,
        record.aggregation_start_at,
        record.aggregation_end_at,
        value["source_kind"],
        _json(value["provenance"]),
        value["vendor_semantic_code"],
        value["semantics_version"],
        value["ontology_version"],
        value["normalizer_version"],
        value["semantic_identity"],
        value["transport_receipt_identity"],
        value["trusted_for_analytics"],
        value["upcast_status"],
        _json(value["classification_evidence"]),
    )


def _comparable_values(
    record: PersistedObservationSemanticsV2,
) -> tuple[Any, ...]:
    value = record.model_dump(mode="json")
    return (
        value["schema_version"],
        value["observation_type"],
        value["metric_id"],
        value["semantic_payload"],
        value["canonical_unit"],
        record.occurred_at,
        record.aggregation_start_at,
        record.aggregation_end_at,
        value["source_kind"],
        value["provenance"],
        value["vendor_semantic_code"],
        value["semantics_version"],
        value["ontology_version"],
        value["normalizer_version"],
        value["semantic_identity"],
        value["transport_receipt_identity"],
        value["trusted_for_analytics"],
        value["upcast_status"],
        value["classification_evidence"],
    )


def _database_values(row: tuple[Any, ...]) -> tuple[Any, ...]:
    mutable = list(row)
    for index in (3, 9, 18):
        mutable[index] = _mapping(mutable[index])
    return tuple(mutable)


def _semantic_core(values: tuple[Any, ...]) -> tuple[Any, ...]:
    # Provenance/normalizer/transport fields identify independent acquisitions.
    # The first committed acquisition remains immutable while Push/Pull retries
    # may corroborate the same semantic identity through the acquisition ledger.
    return tuple(
        value
        for index, value in enumerate(values)
        if index not in {9, 13, 15, 17, 18}
    )


def _semantic_content_core(values: tuple[Any, ...]) -> tuple[Any, ...]:
    """Compare immutable meaning while accepting the one proved legacy hash."""

    return tuple(
        value
        for index, value in enumerate(values)
        if index not in {9, 13, 14, 15, 17, 18}
    )


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ObservationSemanticsPersistenceError("persisted semantic JSON is invalid")


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


__all__ = [
    "ObservationSemanticsPersistenceError",
    "native_semantics_record",
    "persist_observation_semantics_v2",
]
