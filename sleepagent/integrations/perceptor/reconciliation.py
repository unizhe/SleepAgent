"""Provider-neutral fact reconciliation for Perceptor Push and Pull.

Candidate and raw identities remain source-specific.  This module activates the
existing semantic fact/acquisition/conflict ledger after DeviceBinding has
created a :class:`SleepObservation`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sleepagent.domain.contracts import (
    AdapterObservationCandidate,
    MissingIntervalPayload,
    ObservationType,
    SleepObservation,
)
from sleepagent.domain.reconciliation import (
    AcquisitionChannel,
    acquisition_channel_for_observation,
    semantic_fact_slot_key,
    semantic_value_sha256,
    with_reconciliation_conflict,
)
from sleepagent.persistence.uow import UowScope


VITAL_MEASUREMENT_SURFACE = "vital_measurement.v1"
REALTIME_SNAPSHOT_SURFACE = "realtime_snapshot.v1"
CURRENT_STATE_SURFACE = "current_state.v1"
DEVICE_CONNECTIVITY_SURFACE = "device_connectivity.v1"
VENDOR_ALERT_SURFACE = "vendor_alert.v1"
SLEEP_REPORT_SERIES_SURFACE = "vendor_sleep_report_series.v1"
SLEEP_REPORT_STAGE_SURFACE = "vendor_sleep_report_stage.v1"
SLEEP_REPORT_SUMMARY_SURFACE = "vendor_sleep_report_summary.v1"
SLEEP_REPORT_EVENT_SURFACE = "vendor_sleep_report_event.v1"


class PerceptorReconciliationError(RuntimeError):
    """Persisted semantic evidence contradicts the deterministic contract."""


def namespace_generation_scoped_candidate(
    candidate: AdapterObservationCandidate,
    *,
    namespace_id: str,
    namespace_generation: int,
) -> AdapterObservationCandidate:
    """Scope global candidate/source identities to one namespace generation.

    The base Push/Pull parsers intentionally know nothing about persistence
    generations.  Durable normalization applies this boundary before binding so
    an identical vendor payload can be accepted again after a generation reset
    without colliding with an RLS-hidden candidate from the prior generation.
    """

    expected_prefix = f"{candidate.data_mode.value}:"
    if (
        not namespace_id.startswith(expected_prefix)
        or namespace_id == expected_prefix
    ):
        raise PerceptorReconciliationError(
            "candidate namespace does not match its data mode"
        )
    if (
        not isinstance(namespace_generation, int)
        or isinstance(namespace_generation, bool)
        or namespace_generation < 1
    ):
        raise PerceptorReconciliationError(
            "candidate namespace generation must be a positive integer"
        )
    generation_marker = _stable_id(
        "namespace-generation",
        namespace_id,
        candidate.data_mode.value,
        namespace_generation,
    )
    marker_suffix = f":{generation_marker}"
    candidate_is_scoped = candidate.candidate_id.startswith(
        "perceptor:generation-candidate:"
    )
    source_is_scoped = ":perceptor:namespace-generation:" in candidate.source_key
    idempotency_is_scoped = (
        ":perceptor:namespace-generation:" in candidate.idempotency_key
    )
    already_scoped = (
        candidate_is_scoped
        and candidate.source_key.endswith(marker_suffix)
        and candidate.idempotency_key.endswith(marker_suffix)
    )
    if already_scoped:
        return candidate
    if candidate_is_scoped or source_is_scoped or idempotency_is_scoped:
        raise PerceptorReconciliationError(
            "candidate carries a mismatched or partial generation identity"
        )
    return candidate.model_copy(
        update={
            "candidate_id": _stable_id(
                "generation-candidate",
                namespace_id,
                candidate.data_mode.value,
                namespace_generation,
                candidate.candidate_id,
            ),
            "source_key": f"{candidate.source_key}{marker_suffix}",
            "idempotency_key": f"{candidate.idempotency_key}{marker_suffix}",
        }
    )


@dataclass(frozen=True, slots=True)
class PerceptorReconciliationResult:
    canonical_observation_id: str
    canonical_created: bool
    acquisition_created: bool
    push_pull_overlap: bool
    conflict_created_count: int
    duplicate: bool


class PerceptorObservationReconciler:
    """Reconcile one already-bound candidate under the caller's transaction."""

    def reconcile(
        self,
        cursor: Any,
        scope: UowScope,
        *,
        raw_ingress_record_id: str,
        candidate: Any,
        observation: SleepObservation,
        semantic_surface: str,
        committed_at: datetime,
    ) -> PerceptorReconciliationResult:
        if observation.provenance.raw_ingress_record_id != raw_ingress_record_id:
            raise PerceptorReconciliationError(
                "embedded provenance does not reference the durable raw row"
            )
        if candidate.provenance.raw_ingress_record_id != raw_ingress_record_id:
            raise PerceptorReconciliationError(
                "candidate provenance does not reference the durable raw row"
            )
        self._insert_or_validate_candidate(
            cursor,
            scope,
            raw_ingress_record_id=raw_ingress_record_id,
            candidate=candidate,
            committed_at=committed_at,
        )
        slot = semantic_fact_slot_key(
            observation,
            namespace_id=scope.namespace_id,
            semantic_surface=semantic_surface,
            namespace_generation=scope.namespace_generation,
        )
        value_sha256 = semantic_value_sha256(observation)
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (
                f"{scope.namespace_id}\x1f{scope.data_mode}\x1f"
                f"{scope.namespace_generation}\x1f{slot}",
            ),
        )
        self._bootstrap_existing_observations(
            cursor,
            scope,
            target=observation,
            target_slot=slot,
            committed_at=committed_at,
        )
        cursor.execute(
            """
            SELECT value_sha256, observation_id, candidate_id
            FROM public.sleep_domain_observation_fact_values
            WHERE namespace_id = %s AND data_mode = %s AND fact_slot_key = %s
            ORDER BY value_sha256, observation_id
            """,
            (scope.namespace_id, scope.data_mode, slot),
        )
        facts = tuple(cursor.fetchall())
        exact = next((row for row in facts if str(row[0]) == value_sha256), None)
        channel = acquisition_channel_for_observation(observation)
        if exact is not None:
            retained_observation_id = str(exact[1])
            cursor.execute(
                """
                SELECT acquisition_channel
                FROM public.sleep_domain_observation_acquisitions
                WHERE namespace_id = %s AND data_mode = %s
                  AND fact_slot_key = %s AND value_sha256 = %s
                """,
                (scope.namespace_id, scope.data_mode, slot, value_sha256),
            )
            prior_channels = {str(row[0]) for row in cursor.fetchall()}
            acquisition_created = self._insert_acquisition(
                cursor,
                scope,
                raw_ingress_record_id=raw_ingress_record_id,
                candidate_id=candidate.candidate_id,
                observation_id=retained_observation_id,
                fact_slot_key=slot,
                value_sha256=value_sha256,
                channel=channel,
                acquired_at=committed_at,
            )
            opposite = (
                AcquisitionChannel.PULL
                if channel == AcquisitionChannel.PUSH
                else AcquisitionChannel.PUSH
            )
            overlap = opposite.value in prior_channels
            return PerceptorReconciliationResult(
                canonical_observation_id=retained_observation_id,
                canonical_created=False,
                acquisition_created=acquisition_created,
                push_pull_overlap=overlap,
                conflict_created_count=0,
                duplicate=not acquisition_created or not overlap,
            )

        conflicting_facts = facts
        stored_observation = (
            with_reconciliation_conflict(observation)
            if conflicting_facts
            else observation
        )
        canonical_created = self._insert_or_validate_canonical(
            cursor,
            scope,
            raw_ingress_record_id=raw_ingress_record_id,
            candidate_id=candidate.candidate_id,
            observation=stored_observation,
            semantic_surface=semantic_surface,
            expected_value_sha256=value_sha256,
            committed_at=committed_at,
        )
        self._insert_fact_value(
            cursor,
            scope,
            fact_slot_key=slot,
            value_sha256=value_sha256,
            observation_id=stored_observation.observation_id,
            candidate_id=candidate.candidate_id,
            created_at=committed_at,
        )
        acquisition_created = self._insert_acquisition(
            cursor,
            scope,
            raw_ingress_record_id=raw_ingress_record_id,
            candidate_id=candidate.candidate_id,
            observation_id=stored_observation.observation_id,
            fact_slot_key=slot,
            value_sha256=value_sha256,
            channel=channel,
            acquired_at=committed_at,
        )
        conflict_count = 0
        for first_value, first_observation_id, _first_candidate_id in conflicting_facts:
            if self._insert_conflict(
                cursor,
                scope,
                fact_slot_key=slot,
                first_value_sha256=str(first_value),
                first_observation_id=str(first_observation_id),
                second_value_sha256=value_sha256,
                second_observation_id=stored_observation.observation_id,
                detected_at=committed_at,
            ):
                conflict_count += 1
        return PerceptorReconciliationResult(
            canonical_observation_id=stored_observation.observation_id,
            canonical_created=canonical_created,
            acquisition_created=acquisition_created,
            push_pull_overlap=False,
            conflict_created_count=conflict_count,
            duplicate=not canonical_created and not acquisition_created,
        )

    @staticmethod
    def _insert_or_validate_candidate(
        cursor: Any,
        scope: UowScope,
        *,
        raw_ingress_record_id: str,
        candidate: Any,
        committed_at: datetime,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_adapter_candidates (
              candidate_id, namespace_id, data_mode, raw_ingress_record_id,
              provider_account_id, source_key, idempotency_key,
              observation_type, candidate_json, received_at, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (candidate_id) DO NOTHING
            """,
            (
                candidate.candidate_id,
                scope.namespace_id,
                scope.data_mode,
                raw_ingress_record_id,
                candidate.provider_account_id,
                candidate.source_key,
                candidate.idempotency_key,
                candidate.observation_type.value,
                candidate.model_dump_json(),
                candidate.received_at,
                committed_at,
            ),
        )
        if cursor.rowcount == 1:
            return
        cursor.execute(
            """
            SELECT raw_ingress_record_id, candidate_json
            FROM public.sleep_domain_adapter_candidates
            WHERE candidate_id = %s AND namespace_id = %s AND data_mode = %s
            """,
            (candidate.candidate_id, scope.namespace_id, scope.data_mode),
        )
        row = cursor.fetchone()
        if row is None or str(row[0]) != raw_ingress_record_id:
            raise PerceptorReconciliationError("candidate identity collision")
        persisted = _json_mapping(row[1])
        if persisted != candidate.model_dump(mode="json"):
            raise PerceptorReconciliationError("candidate content collision")

    def _bootstrap_existing_observations(
        self,
        cursor: Any,
        scope: UowScope,
        *,
        target: SleepObservation,
        target_slot: str,
        committed_at: datetime,
    ) -> None:
        observed_at = target.measurement_at or target.event_occurred_at
        assert observed_at is not None
        cursor.execute(
            """
            SELECT observation.observation_id, observation.candidate_id,
                   observation.raw_ingress_record_id,
                   observation.observation_json, observation.received_at
            FROM public.sleep_domain_canonical_observations AS observation
            JOIN public.sleep_domain_raw_inbox AS raw
              ON raw.raw_ingress_record_id = observation.raw_ingress_record_id
             AND raw.namespace_id = observation.namespace_id
             AND raw.data_mode = observation.data_mode
             AND raw.subject_id = observation.subject_id
            WHERE observation.namespace_id = %s AND observation.data_mode = %s
              AND observation.subject_id = %s AND observation.device_id = %s
              AND (observation.measurement_at = %s OR observation.event_occurred_at = %s)
              AND raw.scope_protocol_version >= 2
              AND raw.namespace_generation = %s
              AND raw.run_id IS NOT DISTINCT FROM %s
              AND raw.arm_id IS NOT DISTINCT FROM %s
            ORDER BY observation.observation_id
            """,
            (
                scope.namespace_id,
                scope.data_mode,
                target.subject_id,
                target.device_id,
                observed_at,
                observed_at,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
            ),
        )
        for row in tuple(cursor.fetchall()):
            existing = SleepObservation.model_validate(_json_mapping(row[3]))
            try:
                surface = semantic_surface_for_observation(existing)
                existing_slot = semantic_fact_slot_key(
                    existing,
                    namespace_id=scope.namespace_id,
                    semantic_surface=surface,
                    namespace_generation=scope.namespace_generation,
                )
            except ValueError:
                continue
            if existing_slot != target_slot:
                continue
            value_sha256 = semantic_value_sha256(existing)
            self._insert_fact_value(
                cursor,
                scope,
                fact_slot_key=existing_slot,
                value_sha256=value_sha256,
                observation_id=str(row[0]),
                candidate_id=str(row[1]),
                created_at=committed_at,
            )
            self._insert_acquisition(
                cursor,
                scope,
                raw_ingress_record_id=str(row[2]),
                candidate_id=str(row[1]),
                observation_id=str(row[0]),
                fact_slot_key=existing_slot,
                value_sha256=value_sha256,
                channel=acquisition_channel_for_observation(existing),
                acquired_at=row[4] or committed_at,
            )

    @staticmethod
    def _insert_or_validate_canonical(
        cursor: Any,
        scope: UowScope,
        *,
        raw_ingress_record_id: str,
        candidate_id: str,
        observation: SleepObservation,
        semantic_surface: str,
        expected_value_sha256: str,
        committed_at: datetime,
    ) -> bool:
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_canonical_observations (
              observation_id, namespace_id, data_mode, candidate_id,
              raw_ingress_record_id, subject_id, device_id, device_binding_id,
              binding_version, observation_type, source_key, idempotency_key,
              observation_json, measurement_at, event_occurred_at,
              received_at, created_at
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s::jsonb, %s, %s, %s, %s
            )
            ON CONFLICT (observation_id) DO NOTHING
            """,
            (
                observation.observation_id,
                scope.namespace_id,
                scope.data_mode,
                candidate_id,
                raw_ingress_record_id,
                observation.subject_id,
                observation.device_id,
                observation.device_binding_id,
                observation.binding_version,
                observation.observation_type.value,
                observation.source_key,
                observation.idempotency_key,
                observation.model_dump_json(),
                observation.measurement_at,
                observation.event_occurred_at,
                observation.received_at,
                committed_at,
            ),
        )
        if cursor.rowcount == 1:
            return True
        cursor.execute(
            """
            SELECT observation_json
            FROM public.sleep_domain_canonical_observations
            WHERE observation_id = %s AND namespace_id = %s AND data_mode = %s
            """,
            (observation.observation_id, scope.namespace_id, scope.data_mode),
        )
        row = cursor.fetchone()
        if row is None:
            raise PerceptorReconciliationError("canonical identity collision")
        persisted = SleepObservation.model_validate(_json_mapping(row[0]))
        if (
            semantic_fact_slot_key(
                persisted,
                namespace_id=scope.namespace_id,
                semantic_surface=semantic_surface,
                namespace_generation=scope.namespace_generation,
            )
            != semantic_fact_slot_key(
                observation,
                namespace_id=scope.namespace_id,
                semantic_surface=semantic_surface,
                namespace_generation=scope.namespace_generation,
            )
            or semantic_value_sha256(persisted) != expected_value_sha256
        ):
            raise PerceptorReconciliationError("canonical content collision")
        return False

    @staticmethod
    def _insert_fact_value(
        cursor: Any,
        scope: UowScope,
        *,
        fact_slot_key: str,
        value_sha256: str,
        observation_id: str,
        candidate_id: str,
        created_at: datetime,
    ) -> None:
        fact_value_id = _stable_id(
            "fact",
            scope.namespace_id,
            scope.data_mode,
            scope.namespace_generation,
            fact_slot_key,
            value_sha256,
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_observation_fact_values (
              fact_value_id, namespace_id, data_mode, fact_slot_key,
              value_sha256, observation_id, candidate_id, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (namespace_id, data_mode, fact_slot_key, value_sha256)
            DO NOTHING
            """,
            (
                fact_value_id,
                scope.namespace_id,
                scope.data_mode,
                fact_slot_key,
                value_sha256,
                observation_id,
                candidate_id,
                created_at,
            ),
        )
        if cursor.rowcount == 1:
            return
        cursor.execute(
            """
            SELECT observation_id
            FROM public.sleep_domain_observation_fact_values
            WHERE namespace_id = %s AND data_mode = %s
              AND fact_slot_key = %s AND value_sha256 = %s
            """,
            (scope.namespace_id, scope.data_mode, fact_slot_key, value_sha256),
        )
        row = cursor.fetchone()
        if row is None or str(row[0]) != observation_id:
            raise PerceptorReconciliationError("semantic fact collision")

    @staticmethod
    def _insert_acquisition(
        cursor: Any,
        scope: UowScope,
        *,
        raw_ingress_record_id: str,
        candidate_id: str,
        observation_id: str,
        fact_slot_key: str,
        value_sha256: str,
        channel: AcquisitionChannel,
        acquired_at: datetime,
    ) -> bool:
        acquisition_id = _stable_id(
            "acquisition",
            scope.namespace_id,
            scope.data_mode,
            scope.namespace_generation,
            raw_ingress_record_id,
            candidate_id,
            fact_slot_key,
            value_sha256,
            channel.value,
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_observation_acquisitions (
              acquisition_id, namespace_id, data_mode, fact_slot_key,
              value_sha256, observation_id, candidate_id,
              raw_ingress_record_id, acquisition_channel, acquired_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (
              namespace_id, data_mode, raw_ingress_record_id, candidate_id,
              fact_slot_key, value_sha256
            ) DO NOTHING
            """,
            (
                acquisition_id,
                scope.namespace_id,
                scope.data_mode,
                fact_slot_key,
                value_sha256,
                observation_id,
                candidate_id,
                raw_ingress_record_id,
                channel.value,
                acquired_at,
            ),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _insert_conflict(
        cursor: Any,
        scope: UowScope,
        *,
        fact_slot_key: str,
        first_value_sha256: str,
        first_observation_id: str,
        second_value_sha256: str,
        second_observation_id: str,
        detected_at: datetime,
    ) -> bool:
        if first_value_sha256 < second_value_sha256:
            low_value, low_observation = first_value_sha256, first_observation_id
            high_value, high_observation = second_value_sha256, second_observation_id
        else:
            low_value, low_observation = second_value_sha256, second_observation_id
            high_value, high_observation = first_value_sha256, first_observation_id
        conflict_id = _stable_id(
            "conflict",
            scope.namespace_id,
            scope.data_mode,
            scope.namespace_generation,
            fact_slot_key,
            low_value,
            high_value,
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_observation_conflicts (
              conflict_id, namespace_id, data_mode, fact_slot_key,
              first_value_sha256, second_value_sha256,
              first_observation_id, second_observation_id, detected_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (
              namespace_id, data_mode, fact_slot_key,
              first_value_sha256, second_value_sha256
            ) DO NOTHING
            """,
            (
                conflict_id,
                scope.namespace_id,
                scope.data_mode,
                fact_slot_key,
                low_value,
                high_value,
                low_observation,
                high_observation,
                detected_at,
            ),
        )
        return cursor.rowcount == 1


def semantic_surface_for_push(observation: SleepObservation) -> str:
    observation_type = _effective_observation_type(observation)
    if observation_type in {
        ObservationType.HEART_RATE,
        ObservationType.RESPIRATORY_RATE,
        ObservationType.MOVEMENT,
        ObservationType.BED_PRESENCE,
    }:
        return VITAL_MEASUREMENT_SURFACE
    if observation_type == ObservationType.DEVICE_CONNECTIVITY:
        return DEVICE_CONNECTIVITY_SURFACE
    if observation_type == ObservationType.VENDOR_ALERT:
        return VENDOR_ALERT_SURFACE
    raise ValueError("Push observation has no governed reconciliation surface")


def semantic_surface_for_pull(
    endpoint: str,
    observation: SleepObservation,
) -> str:
    if endpoint == "/vitalSigns/getHistoryData":
        return VITAL_MEASUREMENT_SURFACE
    if endpoint == "/vitalSigns/getRealTimes":
        return REALTIME_SNAPSHOT_SURFACE
    if endpoint == "/vitalSigns/getCurrent":
        return CURRENT_STATE_SURFACE
    if endpoint == "/vitalSigns/getSleepReport":
        observation_type = _effective_observation_type(observation)
        if observation_type in {
            ObservationType.HEART_RATE,
            ObservationType.RESPIRATORY_RATE,
            ObservationType.MOVEMENT,
        }:
            return SLEEP_REPORT_SERIES_SURFACE
        if observation_type == ObservationType.SLEEP_STAGE_INTERVAL:
            return SLEEP_REPORT_STAGE_SURFACE
        if observation_type == ObservationType.VENDOR_SLEEP_PROFILE_METRIC:
            return SLEEP_REPORT_SUMMARY_SURFACE
        if observation_type == ObservationType.BED_EXIT:
            return SLEEP_REPORT_EVENT_SURFACE
    raise ValueError("Pull observation has no governed reconciliation surface")


def semantic_surface_for_observation(observation: SleepObservation) -> str:
    channel = acquisition_channel_for_observation(observation)
    if channel == AcquisitionChannel.PUSH:
        return semantic_surface_for_push(observation)
    source_key = observation.source_key
    for endpoint in (
        "/vitalSigns/getHistoryData",
        "/vitalSigns/getRealTimes",
        "/vitalSigns/getCurrent",
        "/vitalSigns/getSleepReport",
    ):
        endpoint_name = endpoint.rsplit("/", 1)[-1]
        if f":{endpoint_name}:" in source_key:
            return semantic_surface_for_pull(endpoint, observation)
    raise ValueError("Pull observation source does not identify its endpoint")


def _effective_observation_type(observation: SleepObservation) -> ObservationType:
    if isinstance(observation.payload, MissingIntervalPayload):
        return observation.payload.target_observation_type
    return observation.observation_type


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return f"perceptor:{prefix}:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _json_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise PerceptorReconciliationError("persisted JSON is not an object")
    return dict(value)


__all__ = [
    "CURRENT_STATE_SURFACE",
    "DEVICE_CONNECTIVITY_SURFACE",
    "PerceptorObservationReconciler",
    "PerceptorReconciliationError",
    "PerceptorReconciliationResult",
    "REALTIME_SNAPSHOT_SURFACE",
    "SLEEP_REPORT_EVENT_SURFACE",
    "SLEEP_REPORT_SERIES_SURFACE",
    "SLEEP_REPORT_STAGE_SURFACE",
    "SLEEP_REPORT_SUMMARY_SURFACE",
    "VENDOR_ALERT_SURFACE",
    "VITAL_MEASUREMENT_SURFACE",
    "namespace_generation_scoped_candidate",
    "semantic_surface_for_observation",
    "semantic_surface_for_pull",
    "semantic_surface_for_push",
]
