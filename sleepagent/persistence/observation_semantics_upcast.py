"""Bounded, resumable legacy Movement classifier for migration 014."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from sleepagent.domain.canonical_observation import CanonicalObservationFactoryV2
from sleepagent.domain.contracts import (
    AdapterObservationCandidate,
    MovementPayload,
    ObservationType,
    SleepObservation,
    SourceKind,
)
from sleepagent.domain.observation_semantics import (
    MovementMetricId,
    MovementPayloadV2,
    PersistedObservationSemanticsV2,
)
from sleepagent.persistence.observation_semantics import (
    native_semantics_record,
    persist_observation_semantics_v2,
)


UTC = timezone.utc
UPCAST_NORMALIZER_VERSION = "legacy-observation-upcast.v1"


@dataclass(frozen=True, slots=True)
class _Scope:
    namespace_id: str
    data_mode: str


@dataclass(frozen=True, slots=True)
class UpcastCounts:
    scanned: int = 0
    already_classified: int = 0
    movement_index: int = 0
    movement_event_count: int = 0
    legacy_ambiguous_movement: int = 0
    duplicate_semantic_identity: int = 0
    inserted: int = 0
    errors: int = 0

    def add(self, **changes: int) -> "UpcastCounts":
        values = asdict(self) | {
            key: getattr(self, key) + value for key, value in changes.items()
        }
        return UpcastCounts(**values)


def classify_legacy_movement(
    observation: SleepObservation,
    candidate: AdapterObservationCandidate,
) -> PersistedObservationSemanticsV2:
    """Classify only facts proved by durable vendor and adapter evidence."""

    if observation.observation_type is not ObservationType.MOVEMENT or not isinstance(
        observation.payload, MovementPayload
    ):
        raise ValueError("legacy semantic classifier accepts Movement only")
    if (
        candidate.provenance.raw_ingress_record_id
        != observation.provenance.raw_ingress_record_id
    ):
        raise ValueError("legacy candidate and observation raw evidence disagree")
    occurred_at = observation.measurement_at or observation.event_occurred_at
    if occurred_at is None:
        return _ambiguous_record(
            observation,
            candidate,
            evidence_codes=("AUTHORITATIVE_TIMESTAMP_MISSING",),
        )
    flags = set(observation.quality.quality_flags)
    limitations = set(observation.quality.limitations)
    adapter_id = observation.provenance.adapter_id
    perceptor_evidence = observation.provenance.provider_id == "perceptor"
    if (
        perceptor_evidence
        and adapter_id in {"yunyun-v2.5.2-push", "perceptor-pull"}
        and observation.source_kind is SourceKind.DEVICE_MEASURED
        and observation.payload.unit == "index"
    ):
        payload = MovementPayloadV2(
            metric_id=MovementMetricId.MOVEMENT_INDEX,
            value=observation.payload.value,
            unit="vendor_index",
            vendor_semantic_code="perceptor.body_shake.index",
        )
        return _classified_record(
            candidate,
            payload,
            evidence_codes=(
                "PERCEPTOR_BODY_SHAKE_DEVICE_MEASUREMENT",
                "LEGACY_UNIT_INDEX",
            ),
        )
    value = observation.payload.value
    if (
        perceptor_evidence
        and adapter_id == "perceptor-pull"
        and observation.source_kind is SourceKind.VENDOR_DERIVED
        and "vendor_report_hour_bucket" in flags
        and "vendor_hourly_movement_count_not_continuous_sample" in limitations
        and float(value).is_integer()
        and observation.measurement_at is not None
    ):
        start = observation.measurement_at
        payload = MovementPayloadV2(
            metric_id=MovementMetricId.MOVEMENT_EVENT_COUNT,
            value=int(value),
            unit="count",
            aggregation_start_at=start,
            aggregation_end_at=start + timedelta(hours=1),
            vendor_semantic_code="perceptor.body_shake.hourly_count",
        )
        return _classified_record(
            candidate,
            payload,
            evidence_codes=(
                "PERCEPTOR_SLEEP_REPORT_HOUR_BUCKET",
                "EXPLICIT_VENDOR_COUNT_FIELD",
            ),
        )
    evidence_codes = ["VENDOR_MEANING_NOT_PROVED"]
    if "vendor_report_series" in flags:
        evidence_codes.append("TIME_LONG_VALUE_SHAPE_AMBIGUOUS")
    return _ambiguous_record(
        observation,
        candidate,
        evidence_codes=tuple(evidence_codes),
    )


def run_upcast(
    connection: Any,
    *,
    batch_size: int = 250,
    max_rows: int = 10_000,
    dry_run: bool = False,
) -> UpcastCounts:
    """Scan at most ``max_rows`` missing sidecars; reruns resume naturally."""

    if batch_size < 1 or batch_size > 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    cursor_after = ("", "", "")
    known_identities = _known_semantic_identities(connection)
    counts = UpcastCounts(already_classified=len(known_identities))
    while counts.scanned < max_rows:
        limit = min(batch_size, max_rows - counts.scanned)
        classified_at = datetime.now(tz=UTC)
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                SELECT observation.observation_id,
                       observation.namespace_id,
                       observation.data_mode,
                       observation.subject_id,
                       observation.observation_json,
                       candidate.candidate_json
                FROM public.sleep_domain_canonical_observations AS observation
                JOIN public.sleep_domain_adapter_candidates AS candidate
                  ON candidate.candidate_id = observation.candidate_id
                 AND candidate.namespace_id = observation.namespace_id
                 AND candidate.data_mode = observation.data_mode
                LEFT JOIN public.sleep_domain_observation_semantics_v2 AS semantic
                  ON semantic.observation_id = observation.observation_id
                 AND semantic.namespace_id = observation.namespace_id
                 AND semantic.data_mode = observation.data_mode
                WHERE observation.observation_type = 'movement'
                  AND semantic.observation_id IS NULL
                  AND (
                    observation.observation_id,
                    observation.namespace_id,
                    observation.data_mode
                  ) > (%s, %s, %s)
                ORDER BY observation.observation_id,
                         observation.namespace_id,
                         observation.data_mode
                LIMIT %s
                """,
                (*cursor_after, limit),
            )
            rows = cursor.fetchall()
            if not rows:
                connection.rollback() if dry_run else connection.commit()
                break
            for row in rows:
                observation_id = str(row[0])
                cursor_after = (observation_id, str(row[1]), str(row[2]))
                counts = counts.add(scanned=1)
                cursor.execute("SAVEPOINT observation_semantics_upcast_row")
                try:
                    observation = SleepObservation.model_validate(_mapping(row[4]))
                    candidate = AdapterObservationCandidate.model_validate(
                        _mapping(row[5])
                    )
                    semantic = classify_legacy_movement(observation, candidate)
                    counts = counts.add(**{semantic.metric_id: 1})
                    semantic_key = (
                        str(row[1]),
                        str(row[2]),
                        semantic.semantic_identity,
                    )
                    if semantic_key in known_identities:
                        counts = counts.add(duplicate_semantic_identity=1)
                    else:
                        if not dry_run:
                            cursor.execute(
                                "SELECT pg_advisory_xact_lock("
                                "hashtextextended(%s, 0))",
                                (
                                    "observation-semantics-upcast:"
                                    f"{semantic.semantic_identity}",
                                ),
                            )
                            cursor.execute(
                                "SELECT observation_id FROM "
                                "public.sleep_domain_observation_semantics_v2 "
                                "WHERE namespace_id = %s AND data_mode = %s "
                                "AND semantic_identity = %s",
                                (
                                    str(row[1]),
                                    str(row[2]),
                                    semantic.semantic_identity,
                                ),
                            )
                            if cursor.fetchone() is not None:
                                counts = counts.add(
                                    duplicate_semantic_identity=1
                                )
                            else:
                                inserted = persist_observation_semantics_v2(
                                    cursor,
                                    _Scope(str(row[1]), str(row[2])),
                                    observation_id=observation_id,
                                    subject_id=str(row[3]),
                                    semantics=semantic,
                                    created_at=classified_at,
                                )
                                counts = counts.add(inserted=int(inserted))
                        known_identities.add(semantic_key)
                except Exception:
                    cursor.execute(
                        "ROLLBACK TO SAVEPOINT observation_semantics_upcast_row"
                    )
                    counts = counts.add(errors=1)
                else:
                    cursor.execute(
                        "RELEASE SAVEPOINT observation_semantics_upcast_row"
                    )
            connection.rollback() if dry_run else connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()
    return counts


def _classified_record(
    candidate: AdapterObservationCandidate,
    payload: MovementPayloadV2,
    *,
    evidence_codes: tuple[str, ...],
) -> PersistedObservationSemanticsV2:
    canonical = CanonicalObservationFactoryV2().build(
        candidate=candidate,
        movement_payload=payload,
        normalizer_version=UPCAST_NORMALIZER_VERSION,
    )
    return native_semantics_record(canonical).model_copy(
        update={
            "upcast_status": "legacy_classified",
            "classification_evidence": {
                "schema_version": "semantic_classification_evidence.v1",
                "classification_source": UPCAST_NORMALIZER_VERSION,
                "evidence_codes": list(evidence_codes),
                "numeric_heuristic_used": False,
            },
        }
    )


def _ambiguous_record(
    observation: SleepObservation,
    candidate: AdapterObservationCandidate,
    *,
    evidence_codes: tuple[str, ...],
) -> PersistedObservationSemanticsV2:
    occurred_at = observation.measurement_at or observation.event_occurred_at
    if occurred_at is None:
        occurred_at = observation.received_at
    payload = MovementPayloadV2(
        metric_id=MovementMetricId.LEGACY_AMBIGUOUS_MOVEMENT,
        value=observation.payload.value,
        unit="legacy_unknown",
        vendor_semantic_code="legacy.movement.unresolved",
    )
    material = {
        "provider_id": candidate.provider_id,
        "provider_account_id": candidate.provider_account_id,
        "provider_device": candidate.provider_device.model_dump(mode="json"),
        "observation_type": "movement",
        "metric_id": MovementMetricId.LEGACY_AMBIGUOUS_MOVEMENT.value,
        "canonical_unit": "legacy_unknown",
        "value": observation.payload.value,
        "occurred_at": occurred_at.astimezone(UTC).isoformat(),
        "aggregation_start_at": None,
        "aggregation_end_at": None,
        "source_kind": observation.source_kind.value,
        "vendor_semantic_code": "legacy.movement.unresolved",
    }
    identity = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return PersistedObservationSemanticsV2(
        observation_type=ObservationType.MOVEMENT,
        metric_id=MovementMetricId.LEGACY_AMBIGUOUS_MOVEMENT.value,
        semantic_payload=payload.model_dump(mode="json"),
        canonical_unit="legacy_unknown",
        occurred_at=occurred_at,
        source_kind=observation.source_kind,
        provenance=observation.provenance,
        vendor_semantic_code="legacy.movement.unresolved",
        ontology_version="observation_semantics.v2",
        normalizer_version=UPCAST_NORMALIZER_VERSION,
        semantic_identity=identity,
        transport_receipt_identity=observation.idempotency_key,
        trusted_for_analytics=False,
        upcast_status="legacy_ambiguous",
        classification_evidence={
            "schema_version": "semantic_classification_evidence.v1",
            "classification_source": UPCAST_NORMALIZER_VERSION,
            "evidence_codes": list(evidence_codes),
            "numeric_heuristic_used": False,
        },
    )


def _known_semantic_identities(
    connection: Any,
) -> set[tuple[str, str, str]]:
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT namespace_id, data_mode, semantic_identity "
            "FROM public.sleep_domain_observation_semantics_v2 "
            "WHERE observation_type = 'movement'"
        )
        return {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in cursor.fetchall()
        }
    finally:
        cursor.close()


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("legacy observation JSON is not an object")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--max-rows", type=int, default=10_000)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    import psycopg

    with psycopg.connect(args.database_url) as connection:
        counts = run_upcast(
            connection,
            batch_size=args.batch_size,
            max_rows=args.max_rows,
            dry_run=args.dry_run,
        )
    print(
        json.dumps(
            asdict(counts) | {"dry_run": args.dry_run},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 1 if counts.errors else 0


if __name__ == "__main__":  # pragma: no cover - exercised as an operator CLI
    raise SystemExit(main())


__all__ = ["UpcastCounts", "classify_legacy_movement", "run_upcast"]
