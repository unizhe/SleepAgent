# 本模块负责可复现模拟数据与回放契约，不参与生产事实判定。
"""Durable replay-journey Worker handler for the first backend slice."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any, Mapping

from sleepagent.persistence.migrations import MIGRATION_MANIFEST_SHA256
from sleepagent.persistence.uow import UnitOfWorkFactory, UowScope
from sleepagent.simulation.generator import CanonicalReplayGenerator
from sleepagent.simulation.replay_ingress import (
    AdaptedReplay,
    AdaptedReplayIngressV2,
    ReplayIngressItem,
    canonical_ingress_items_sha256,
    replay_external_fact_adapter,
)
from sleepagent.simulation.seed_registry import (
    load_replay_seed_registry,
    verify_packaged_seed,
)
from sleepagent.infrastructure.postgres_sleep_slice import (
    IngressResult,
    RawPayloadCipher,
    ReplayIngressHandler,
    ReplayIngressOrder,
    ReplayRawBatch,
)
from sleepagent.workers.retention import PostgresRetentionKeyCoordinator
from sleepagent.workers.runtime import (
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
    WorkResult,
)


UTC = timezone.utc


class ReplayJourneyInvariantError(RuntimeError):
    """A durable journey or packaged artifact violates its locked contract."""


class ReplayJourneyLeaseLost(RuntimeError):
    """The current Worker no longer owns the journey fence."""


class ReplayJourneyTerminalError(RuntimeError):
    """A stable first-slice terminal condition was committed by a child."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_POSTGRES_TERMINAL_CODES = frozenset(
    {
        "episode_not_committed",
        "product_failed",
        "role_projection_incomplete",
        "scenario_contract_invalid",
    }
)


def _postgres_terminal_code(exc: Exception) -> str | None:
    if getattr(exc, "sqlstate", None) != "P0001":
        return None
    diagnostic = getattr(exc, "diag", None)
    code = str(getattr(diagnostic, "message_primary", "")).strip()
    return code if code in _POSTGRES_TERMINAL_CODES else None


@dataclass(frozen=True, slots=True)
class JourneyBatchProgress:
    committed_batch_count: int
    committed_observation_count: int
    predecessor_batch_id: str | None
    predecessor_raw_ingress_record_id: str | None
    predecessor_work_id: str | None


@dataclass(frozen=True, slots=True)
class NormalizationProgress:
    total: int
    succeeded: int
    terminal_failed: int


@dataclass(frozen=True, slots=True)
class EpisodeProgress:
    night_episode_id: str
    night_episode_revision_id: str
    episode_local_date: str
    assignment_basis: str
    membership_count: int
    fast_path_operation_id: str


@dataclass(frozen=True, slots=True)
class FastPathProgress:
    status: str
    fast_path_operation_id: str
    product_operation_id: str | None = None
    quality_assessment_id: str | None = None
    current_risk_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProductProgress:
    status: str
    product_operation_id: str
    analysis_revision_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectionProgress:
    analysis_revision_id: str
    role_projection_ids: dict[str, str]


class PostgresReplayJourneyRepository:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
    ) -> None:
        self.uow_factory = uow_factory

    def bootstrap(
        self,
        scope: UowScope,
        *,
        root_operation_id: str,
        journey_id: str,
    ) -> bool:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT public.sleepagent_bootstrap_demo_journey(%s, %s)",
                    (root_operation_id, journey_id),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            changed = bool(row and row[0] is True)
            if changed:
                uow.commit()
            return changed

    def ensure_manifest(
        self,
        scope: UowScope,
        *,
        journey_id: str,
        seed_id: str,
        adapted: AdaptedReplay,
        manifest_sha256: str,
    ) -> None:
        manifest = adapted.manifest
        manifest_id = f"manifest:{manifest_sha256}"
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "INSERT INTO public.backend_replay_ingress_manifests ("
                    "manifest_id, journey_id, namespace_id, data_mode, "
                    "namespace_generation, run_id, arm_id, subject_id, seed_id, "
                    "schema_version, scenario_sha256, generator_version, "
                    "adapter_version, component_pins_sha256, "
                    "canonical_sequence_sha256, observation_count, "
                    "first_received_at, last_received_at, night_count, "
                    "manifest_json) VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s, %s, %s, %s, %s::jsonb) "
                    "ON CONFLICT (journey_id) DO NOTHING",
                    (
                        manifest_id,
                        journey_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        seed_id,
                        manifest.schema_version,
                        manifest.scenario_sha256,
                        manifest.generator_version,
                        manifest.adapter_version,
                        manifest.component_pins_sha256,
                        manifest.canonical_sequence_sha256,
                        manifest.observation_count,
                        manifest.first_received_at,
                        manifest.last_received_at,
                        manifest.night_count,
                        manifest.model_dump_json(),
                    ),
                )
                cursor.execute(
                    "SELECT manifest_id, canonical_sequence_sha256, "
                    "observation_count FROM public.backend_replay_ingress_manifests "
                    "WHERE journey_id = %s",
                    (journey_id,),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None or (
                str(row[0]) != manifest_id
                or str(row[1]) != manifest.canonical_sequence_sha256
                or int(row[2]) != manifest.observation_count
            ):
                raise ReplayJourneyInvariantError(
                    "durable ingress manifest conflicts with packaged facts"
                )
            uow.commit()

    def batch_progress(
        self,
        scope: UowScope,
        *,
        journey_id: str,
    ) -> JourneyBatchProgress:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT batch_id, batch_sequence, observation_count, "
                    "predecessor_batch_id, receipt_json "
                    "FROM public.backend_replay_ingress_batches "
                    "WHERE journey_id = %s AND status = 'committed' "
                    "ORDER BY batch_sequence",
                    (journey_id,),
                )
                rows = tuple(cursor.fetchall())
            finally:
                cursor.close()
            uow.commit()
        total = 0
        predecessor_batch_id: str | None = None
        predecessor_raw_id: str | None = None
        predecessor_work_id: str | None = None
        for expected_sequence, row in enumerate(rows, start=1):
            if int(row[1]) != expected_sequence:
                raise ReplayJourneyInvariantError("durable batch sequence has a gap")
            if (
                (expected_sequence == 1 and row[3] is not None)
                or (
                    expected_sequence > 1
                    and str(row[3]) != predecessor_batch_id
                )
            ):
                raise ReplayJourneyInvariantError(
                    "durable batch predecessor chain drifted"
                )
            receipt = _json_object(row[4])
            predecessor_batch_id = str(row[0])
            predecessor_raw_id = str(receipt["last_raw_ingress_record_id"])
            predecessor_work_id = str(receipt["last_normalization_work_id"])
            total += int(row[2])
        return JourneyBatchProgress(
            committed_batch_count=len(rows),
            committed_observation_count=total,
            predecessor_batch_id=predecessor_batch_id,
            predecessor_raw_ingress_record_id=predecessor_raw_id,
            predecessor_work_id=predecessor_work_id,
        )

    def stage_future_items(
        self,
        scope: UowScope,
        *,
        journey_id: str,
        manifest_sha256: str,
        items: tuple[ReplayIngressItem, ...],
    ) -> None:
        if not items:
            return
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                for item in items:
                    fact_sha256 = hashlib.sha256(item.canonical_bytes()).hexdigest()
                    cursor.execute(
                        "INSERT INTO public.backend_replay_staged_facts ("
                        "staged_fact_id, journey_id, namespace_id, data_mode, "
                        "namespace_generation, run_id, arm_id, subject_id, "
                        "manifest_sha256, stream_key, stream_sequence, "
                        "predecessor_sequence, release_at, fact_sha256, "
                        "fact_json, status) VALUES ("
                        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                        "%s, %s, %s::jsonb, 'staged') "
                        "ON CONFLICT (journey_id, stream_sequence) DO NOTHING",
                        (
                            f"staged-fact:{fact_sha256}",
                            journey_id,
                            scope.namespace_id,
                            scope.data_mode,
                            scope.namespace_generation,
                            scope.run_id,
                            scope.arm_id,
                            scope.subject_id,
                            manifest_sha256,
                            item.stream_key,
                            item.sequence,
                            item.predecessor_sequence,
                            item.observation.received_at,
                            fact_sha256,
                            item.model_dump_json(),
                        ),
                    )
                cursor.execute(
                    "SELECT count(*), min(stream_sequence), max(stream_sequence), "
                    "count(DISTINCT manifest_sha256), count(DISTINCT stream_key) "
                    "FROM public.backend_replay_staged_facts "
                    "WHERE journey_id = %s",
                    (journey_id,),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None or (
                int(row[0]) != len(items)
                or int(row[1]) != items[0].sequence
                or int(row[2]) != items[-1].sequence
                or int(row[3]) != 1
                or int(row[4]) != 1
            ):
                raise ReplayJourneyInvariantError(
                    "durable future-fact staging conflicts with manifest"
                )
            uow.commit()

    def commit_batch_checkpoint(
        self,
        scope: UowScope,
        *,
        context: WorkContext,
        batch_sequence: int,
        items: tuple[ReplayIngressItem, ...],
        predecessor_batch_id: str | None,
        results: tuple[IngressResult, ...],
        final_batch: bool,
    ) -> None:
        claim = context.claim
        batch_sha256 = canonical_ingress_items_sha256(items)
        batch_id = f"replay-batch:{hashlib.sha256((claim.work_id + ':' + str(batch_sequence) + ':' + batch_sha256).encode('utf-8')).hexdigest()}"
        receipt = {
            "schema_version": "replay_ingress_batch_receipt.v1",
            "batch_id": batch_id,
            "batch_sequence": batch_sequence,
            "observation_count": len(results),
            "first_raw_ingress_record_id": results[0].raw_ingress_record_id,
            "last_raw_ingress_record_id": results[-1].raw_ingress_record_id,
            "first_normalization_work_id": results[0].normalization_work_id,
            "last_normalization_work_id": results[-1].normalization_work_id,
            "canonical_payload_sha256": batch_sha256,
        }
        checkpoint_sha256 = _sha256(receipt)
        next_phase = "waiting_normalization" if final_batch else "staging_input"
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                if not _heartbeat(cursor, context):
                    raise ReplayJourneyLeaseLost("batch checkpoint lost its fence")
                cursor.execute(
                    "SELECT count(*), min(stream_sequence), max(stream_sequence) "
                    "FROM public.sleep_domain_raw_inbox "
                    "WHERE replay_journey_id = %s AND ingress_stream_key = %s "
                    "AND stream_sequence BETWEEN %s AND %s",
                    (
                        claim.work_id,
                        items[0].stream_key,
                        items[0].sequence,
                        items[-1].sequence,
                    ),
                )
                raw_count = cursor.fetchone()
                if raw_count is None or (
                    int(raw_count[0]) != len(items)
                    or int(raw_count[1]) != items[0].sequence
                    or int(raw_count[2]) != items[-1].sequence
                ):
                    raise ReplayJourneyInvariantError(
                        "raw batch is not atomically visible"
                    )
                cursor.execute(
                    "INSERT INTO public.backend_replay_ingress_batches ("
                    "batch_id, journey_id, namespace_id, data_mode, "
                    "namespace_generation, run_id, arm_id, subject_id, "
                    "batch_sequence, predecessor_batch_id, observation_count, "
                    "canonical_payload_sha256, status, receipt_json, committed_at"
                    ") VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "'committed', %s::jsonb, clock_timestamp()) "
                    "ON CONFLICT (journey_id, batch_sequence) DO NOTHING",
                    (
                        batch_id,
                        claim.work_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        batch_sequence,
                        predecessor_batch_id,
                        len(results),
                        batch_sha256,
                        json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                    ),
                )
                cursor.execute(
                    "SELECT batch_id, canonical_payload_sha256, receipt_json "
                    "FROM public.backend_replay_ingress_batches "
                    "WHERE journey_id = %s AND batch_sequence = %s",
                    (claim.work_id, batch_sequence),
                )
                durable = cursor.fetchone()
                if durable is None or (
                    str(durable[0]) != batch_id
                    or str(durable[1]) != batch_sha256
                    or _json_object(durable[2]) != receipt
                ):
                    raise ReplayJourneyInvariantError(
                        "durable batch checkpoint conflicts with committed raw facts"
                    )
                cursor.execute(
                    "INSERT INTO public.backend_demo_journey_checkpoints ("
                    "checkpoint_id, journey_id, namespace_id, data_mode, "
                    "namespace_generation, run_id, arm_id, subject_id, phase, "
                    "semantic_checkpoint_key, checkpoint_sha256, checkpoint_json, "
                    "lease_generation, fencing_token) VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, 'staging_input', %s, %s, "
                    "%s::jsonb, %s, %s) ON CONFLICT ("
                    "journey_id, phase, semantic_checkpoint_key) DO NOTHING",
                    (
                        f"checkpoint:{checkpoint_sha256}",
                        claim.work_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        f"batch:{batch_sequence}",
                        checkpoint_sha256,
                        json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                        claim.lease_generation,
                        claim.fencing_token,
                    ),
                )
                cursor.execute(
                    "INSERT INTO public.backend_demo_journey_events ("
                    "event_id, journey_id, namespace_id, data_mode, "
                    "namespace_generation, run_id, arm_id, subject_id, "
                    "event_type, state, event_json) VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, "
                    "'journey.batch_committed', %s, %s::jsonb) "
                    "ON CONFLICT (event_id) DO NOTHING",
                    (
                        f"event:{checkpoint_sha256}",
                        claim.work_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        next_phase,
                        json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                    ),
                )
                cursor.execute(
                    "UPDATE public.backend_demo_journeys SET phase = %s, "
                    "resume_at = clock_timestamp(), worker_instance = NULL, "
                    "fencing_token = NULL, lease_expires_at = NULL, "
                    "heartbeat_at = NULL, version = version + 1, "
                    "updated_at = clock_timestamp() WHERE journey_id = %s "
                    "AND lease_generation = %s AND fencing_token = %s",
                    (
                        next_phase,
                        claim.work_id,
                        claim.lease_generation,
                        claim.fencing_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ReplayJourneyLeaseLost("batch transition lost its fence")
            finally:
                cursor.close()
            uow.commit()

    def normalization_progress(
        self,
        scope: UowScope,
        *,
        journey_id: str,
    ) -> NormalizationProgress:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT count(*), count(*) FILTER (WHERE status = 'succeeded'), "
                    "count(*) FILTER (WHERE status IN ('quarantined','dead_letter')) "
                    "FROM public.sleep_domain_normalization_work "
                    "WHERE replay_journey_id = %s",
                    (journey_id,),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if row is None:
            raise ReplayJourneyInvariantError("normalization progress unavailable")
        return NormalizationProgress(
            total=int(row[0]),
            succeeded=int(row[1]),
            terminal_failed=int(row[2]),
        )

    def deadline_exceeded(
        self,
        scope: UowScope,
        *,
        journey_id: str,
    ) -> bool:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT deadline_at <= clock_timestamp() "
                    "FROM public.backend_demo_journeys "
                    "WHERE journey_id = %s",
                    (journey_id,),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if row is None:
            raise ReplayJourneyInvariantError("journey deadline is unavailable")
        return bool(row[0])

    def episode_progress(
        self,
        scope: UowScope,
    ) -> EpisodeProgress | None:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT episode.night_episode_id, "
                    "episode.current_revision_id, episode.episode_local_date, "
                    "episode.assignment_basis, episode.date_state, "
                    "episode.date_conflict, (SELECT count(*) FROM "
                    "public.sleep_domain_episode_observation_memberships AS member "
                    "WHERE member.namespace_id = episode.namespace_id "
                    "AND member.data_mode = episode.data_mode "
                    "AND member.night_episode_id = episode.night_episode_id), "
                    "(SELECT COALESCE(array_agg(member.observation_id "
                    "ORDER BY member.observation_id), ARRAY[]::text[]) "
                    "FROM public.sleep_domain_episode_observation_memberships "
                    "AS member WHERE member.namespace_id = episode.namespace_id "
                    "AND member.data_mode = episode.data_mode "
                    "AND member.night_episode_id = episode.night_episode_id) = "
                    "(SELECT COALESCE(array_agg(value ORDER BY value), "
                    "ARRAY[]::text[]) FROM jsonb_array_elements_text("
                    "revision.revision_json -> 'observation_ids') AS ids(value)) "
                    "FROM public.sleep_domain_night_episodes AS episode "
                    "JOIN public.sleep_domain_night_episode_revisions AS revision "
                    "ON revision.night_episode_revision_id = "
                    "episode.current_revision_id "
                    "AND revision.namespace_id = episode.namespace_id "
                    "AND revision.data_mode = episode.data_mode "
                    "WHERE episode.namespace_id = %s AND episode.data_mode = %s "
                    "AND episode.namespace_generation = %s "
                    "AND COALESCE(episode.run_id, '') = COALESCE(%s, '') "
                    "AND COALESCE(episode.arm_id, '') = COALESCE(%s, '') "
                    "AND episode.subject_id = %s AND episode.protocol_version >= 2 "
                    "ORDER BY episode.created_at, episode.night_episode_id",
                    (
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                    ),
                )
                rows = tuple(cursor.fetchall())
                if any(bool(row[5]) or str(row[4]) == "conflict" for row in rows):
                    raise ReplayJourneyTerminalError("episode_date_conflict")
                finalized = tuple(
                    row
                    for row in rows
                    if str(row[4]) == "finalized" and not bool(row[5])
                )
                if not finalized:
                    uow.commit()
                    return None
                if len(finalized) != 1:
                    raise ReplayJourneyInvariantError(
                        "journey produced more than one finalized NightEpisode"
                    )
                row = finalized[0]
                if (
                    row[1] is None
                    or row[2] is None
                    or str(row[3]) != "observed_wake"
                    or int(row[6]) < 1
                    or row[7] is not True
                ):
                    raise ReplayJourneyTerminalError("episode_not_committed")
                cursor.execute(
                    "SELECT operation_id, status FROM "
                    "public.sleep_domain_operations WHERE namespace_id = %s "
                    "AND data_mode = %s AND namespace_generation = %s "
                    "AND COALESCE(run_id, '') = COALESCE(%s, '') "
                    "AND COALESCE(arm_id, '') = COALESCE(%s, '') "
                    "AND subject_id = %s AND operation_type = 'fast_path' "
                    "AND target_resource_key = %s AND protocol_version >= 2 "
                    "ORDER BY created_at, operation_id",
                    (
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        str(row[1]),
                    ),
                )
                operations = tuple(cursor.fetchall())
            finally:
                cursor.close()
            uow.commit()
        if len(operations) != 1:
            raise ReplayJourneyTerminalError("episode_not_committed")
        return EpisodeProgress(
            night_episode_id=str(row[0]),
            night_episode_revision_id=str(row[1]),
            episode_local_date=str(row[2]),
            assignment_basis=str(row[3]),
            membership_count=int(row[6]),
            fast_path_operation_id=str(operations[0][0]),
        )

    def fast_path_progress(
        self,
        scope: UowScope,
        *,
        episode: EpisodeProgress,
    ) -> FastPathProgress:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT status, operation_json, operation_type FROM "
                    "public.sleep_domain_operations WHERE operation_id = %s "
                    "AND namespace_id = %s AND data_mode = %s "
                    "AND namespace_generation = %s AND subject_id = %s "
                    "AND operation_type = 'fast_path' "
                    "AND target_resource_key = %s",
                    (
                        episode.fast_path_operation_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.subject_id,
                        episode.night_episode_revision_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ReplayJourneyInvariantError(
                        "fast-path operation disappeared"
                    )
                status = str(row[0])
                if status != "succeeded":
                    uow.commit()
                    return FastPathProgress(
                        status=status,
                        fast_path_operation_id=episode.fast_path_operation_id,
                    )
                operation = _json_object(row[1])
                result = _json_object(operation.get("result"))
                quality_id = str(result.get("quality_assessment_id", ""))
                risk_id = str(result.get("current_risk_id", ""))
                product_id = str(result.get("product_agent_operation_id") or "")
                report_id = str(result.get("report_operation_id") or "")
                child_id = report_id or product_id
                child_operation_type = (
                    "product.report.run.v1" if report_id else "product_agent"
                )
                if not quality_id or not risk_id:
                    raise ReplayJourneyInvariantError(
                        "fast-path receipt is missing committed assessment references"
                    )
                cursor.execute(
                    "SELECT quality.assessment_json, risk.risk_json "
                    "FROM public.sleep_domain_current_quality AS quality "
                    "JOIN public.sleep_domain_current_risk AS risk "
                    "ON risk.namespace_id = quality.namespace_id "
                    "AND risk.data_mode = quality.data_mode "
                    "AND risk.subject_id = quality.subject_id "
                    "AND risk.night_episode_id = quality.night_episode_id "
                    "WHERE quality.namespace_id = %s AND quality.data_mode = %s "
                    "AND quality.subject_id = %s "
                    "AND quality.night_episode_id = %s "
                    "AND quality.assessment_id = %s "
                    "AND risk.current_risk_id = %s",
                    (
                        scope.namespace_id,
                        scope.data_mode,
                        scope.subject_id,
                        episode.night_episode_id,
                        quality_id,
                        risk_id,
                    ),
                )
                assessment = cursor.fetchone()
                product = None
                if child_id:
                    cursor.execute(
                        "SELECT status, target_resource_key FROM "
                        "public.sleep_domain_operations WHERE operation_id = %s "
                        "AND namespace_id = %s AND data_mode = %s "
                        "AND namespace_generation = %s AND subject_id = %s "
                        "AND operation_type = %s",
                        (
                            child_id,
                            scope.namespace_id,
                            scope.data_mode,
                            scope.namespace_generation,
                            scope.subject_id,
                            child_operation_type,
                        ),
                    )
                    product = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if assessment is None:
            raise ReplayJourneyInvariantError("fast-path assessments are unavailable")
        quality = _json_object(assessment[0])
        risk = _json_object(assessment[1])
        if quality.get("data_sufficiency") != "sufficient":
            raise ReplayJourneyTerminalError("quality_insufficient")
        urgent = (
            risk.get("data_sufficiency") != "sufficient"
            or bool(result.get("urgent"))
            or bool(risk.get("health_escalation_allowed"))
            or risk.get("risk_state") != "no_reviewed_signal"
        )
        if urgent:
            if child_id or result.get("model_invocation_count") != 0:
                raise ReplayJourneyInvariantError(
                    "urgent fast path crossed the zero-model Product boundary"
                )
            raise ReplayJourneyTerminalError("unexpected_urgent_route")
        if not child_id:
            raise ReplayJourneyInvariantError(
                "non-urgent fast path omitted its Product child"
            )
        if product is None or str(product[1]) != episode.night_episode_revision_id:
            raise ReplayJourneyInvariantError("Product child is not exact-source bound")
        return FastPathProgress(
            status="succeeded",
            fast_path_operation_id=episode.fast_path_operation_id,
            product_operation_id=child_id,
            quality_assessment_id=quality_id,
            current_risk_id=risk_id,
        )

    def product_progress(
        self,
        scope: UowScope,
        *,
        episode: EpisodeProgress,
        product_operation_id: str,
    ) -> ProductProgress:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT status, operation_json, operation_type FROM "
                    "public.sleep_domain_operations WHERE operation_id = %s "
                    "AND namespace_id = %s AND data_mode = %s "
                    "AND namespace_generation = %s AND subject_id = %s "
                    "AND operation_type IN ('product_agent', 'product.report.run.v1') "
                    "AND target_resource_key = %s",
                    (
                        product_operation_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.subject_id,
                        episode.night_episode_revision_id,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if row is None:
            raise ReplayJourneyInvariantError("Product operation disappeared")
        status = str(row[0])
        if status != "succeeded":
            return ProductProgress(status=status, product_operation_id=product_operation_id)
        operation = _json_object(row[1])
        operation_type = str(row[2])
        if operation_type == "product.report.run.v1":
            report_result = _json_object(operation.get("report_result"))
            shared_operation_id = str(
                report_result.get("shared_operation_id") or ""
            )
            if not shared_operation_id:
                raise ReplayJourneyInvariantError(
                    "shared-only report omitted its shared operation"
                )
            with self.uow_factory.begin(scope) as uow:
                cursor = uow.connection.cursor()
                try:
                    cursor.execute(
                        "SELECT status, operation_json FROM "
                        "public.sleep_domain_operations WHERE operation_id = %s "
                        "AND namespace_id = %s AND data_mode = %s "
                        "AND namespace_generation = %s AND subject_id = %s "
                        "AND operation_type = 'product.shared_analysis.v1'",
                        (
                            shared_operation_id,
                            scope.namespace_id,
                            scope.data_mode,
                            scope.namespace_generation,
                            scope.subject_id,
                        ),
                    )
                    shared_row = cursor.fetchone()
                finally:
                    cursor.close()
                uow.commit()
            if shared_row is None:
                raise ReplayJourneyInvariantError(
                    "shared-only report lost its shared analysis operation"
                )
            shared_status = str(shared_row[0])
            if shared_status != "succeeded":
                return ProductProgress(
                    status=shared_status,
                    product_operation_id=product_operation_id,
                )
            result = _json_object(_json_object(shared_row[1]).get("result"))
        else:
            result = _json_object(operation.get("result"))
        analysis_revision_id = str(result.get("analysis_revision_id", ""))
        if (
            not analysis_revision_id
            or result.get("night_episode_revision_id")
            != episode.night_episode_revision_id
            or len(result.get("role_view_ids", ())) != 3
        ):
            raise ReplayJourneyTerminalError("product_failed")
        return ProductProgress(
            status=status,
            product_operation_id=product_operation_id,
            analysis_revision_id=analysis_revision_id,
        )

    def projection_progress(
        self,
        scope: UowScope,
        *,
        episode: EpisodeProgress,
        analysis_revision_id: str,
    ) -> ProjectionProgress:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT view.role, view.role_view_id, view.status, "
                    "view.public_today_json, view.public_projection_sha256 "
                    "FROM public.sleep_domain_analysis_role_views AS view "
                    "JOIN public.sleep_domain_analysis_revisions AS analysis "
                    "ON analysis.analysis_revision_id = view.analysis_revision_id "
                    "AND analysis.namespace_id = view.namespace_id "
                    "AND analysis.data_mode = view.data_mode "
                    "AND analysis.subject_id = view.subject_id "
                    "WHERE view.analysis_revision_id = %s "
                    "AND view.namespace_id = %s AND view.data_mode = %s "
                    "AND view.namespace_generation = %s "
                    "AND COALESCE(view.run_id, '') = COALESCE(%s, '') "
                    "AND COALESCE(view.arm_id, '') = COALESCE(%s, '') "
                    "AND view.subject_id = %s "
                    "AND view.night_episode_revision_id = %s "
                    "AND view.authorization_epoch = %s "
                    "AND view.privacy_epoch = %s "
                    "AND view.retrieval_policy_epoch = %s "
                    "AND view.public_schema_version = 'product_sleep_today.v1' "
                    "ORDER BY view.role",
                    (
                        analysis_revision_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        episode.night_episode_revision_id,
                        scope.authorization_epoch,
                        scope.privacy_epoch,
                        scope.retrieval_policy_epoch,
                    ),
                )
                rows = tuple(cursor.fetchall())
            finally:
                cursor.close()
            uow.commit()
        if len(rows) != 3 or {str(row[0]) for row in rows} != {
            "elder",
            "family",
            "doctor",
        }:
            raise ReplayJourneyTerminalError("role_projection_incomplete")
        role_projection_ids: dict[str, str] = {}
        for row in rows:
            public_payload = _json_object(row[3])
            if (
                str(row[2]) != "ready"
                or public_payload.get("role") != str(row[0])
                or public_payload.get("analysis_revision_id")
                != analysis_revision_id
                or _sha256(public_payload) != str(row[4])
            ):
                raise ReplayJourneyTerminalError("role_projection_incomplete")
            role_projection_ids[str(row[0])] = str(row[1])
        return ProjectionProgress(
            analysis_revision_id=analysis_revision_id,
            role_projection_ids=role_projection_ids,
        )

    def advance(
        self,
        scope: UowScope,
        *,
        context: WorkContext,
        from_phase: str,
        to_phase: str,
        checkpoint: str,
        payload: Mapping[str, Any],
    ) -> None:
        checkpoint_payload = {
            "schema_version": "demo_journey_checkpoint.v1",
            "checkpoint": checkpoint,
            **dict(payload),
        }
        checkpoint_sha256 = _sha256(
            {
                "journey_id": context.claim.work_id,
                "from_phase": from_phase,
                "to_phase": to_phase,
                "payload": checkpoint_payload,
            }
        )
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                if not _heartbeat(cursor, context):
                    raise ReplayJourneyLeaseLost("phase transition lost its fence")
                cursor.execute(
                    "INSERT INTO public.backend_demo_journey_checkpoints ("
                    "checkpoint_id, journey_id, namespace_id, data_mode, "
                    "namespace_generation, run_id, arm_id, subject_id, phase, "
                    "semantic_checkpoint_key, checkpoint_sha256, checkpoint_json, "
                    "lease_generation, fencing_token) VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, "
                    "%s, %s) ON CONFLICT (journey_id, phase, "
                    "semantic_checkpoint_key) DO NOTHING",
                    (
                        f"checkpoint:{checkpoint_sha256}",
                        context.claim.work_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        from_phase,
                        checkpoint,
                        checkpoint_sha256,
                        json.dumps(
                            checkpoint_payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        context.claim.lease_generation,
                        context.claim.fencing_token,
                    ),
                )
                cursor.execute(
                    "INSERT INTO public.backend_demo_journey_events ("
                    "event_id, journey_id, namespace_id, data_mode, "
                    "namespace_generation, run_id, arm_id, subject_id, "
                    "event_type, state, event_json) VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
                    "ON CONFLICT (event_id) DO NOTHING",
                    (
                        f"event:{checkpoint_sha256}",
                        context.claim.work_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        f"journey.{checkpoint}",
                        to_phase,
                        json.dumps(
                            checkpoint_payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
                cursor.execute(
                    "UPDATE public.backend_demo_journeys SET phase = %s, "
                    "resume_at = clock_timestamp(), worker_instance = NULL, "
                    "fencing_token = NULL, lease_expires_at = NULL, "
                    "heartbeat_at = NULL, version = version + 1, "
                    "updated_at = clock_timestamp() WHERE journey_id = %s "
                    "AND phase = %s AND lease_generation = %s "
                    "AND fencing_token = %s",
                    (
                        to_phase,
                        context.claim.work_id,
                        from_phase,
                        context.claim.lease_generation,
                        context.claim.fencing_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ReplayJourneyLeaseLost("phase transition lost its fence")
            finally:
                cursor.close()
            uow.commit()

    def succeed(
        self,
        scope: UowScope,
        *,
        context: WorkContext,
        result: Mapping[str, Any],
    ) -> None:
        try:
            with self.uow_factory.begin(scope) as uow:
                cursor = uow.connection.cursor()
                try:
                    cursor.execute(
                        "SELECT public.sleepagent_succeed_demo_journey("
                        "%s, %s, %s, %s::jsonb)",
                        (
                            context.claim.work_id,
                            context.claim.lease_generation,
                            context.claim.fencing_token,
                            json.dumps(
                                result,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    )
                    changed = cursor.fetchone()
                finally:
                    cursor.close()
                if not changed or changed[0] is not True:
                    raise ReplayJourneyLeaseLost("root success lost its fence")
                uow.commit()
        except Exception as exc:
            terminal_code = _postgres_terminal_code(exc)
            if terminal_code is not None:
                raise ReplayJourneyTerminalError(terminal_code) from exc
            raise

    def wait(
        self,
        scope: UowScope,
        *,
        context: WorkContext,
        phase: str,
        delay_seconds: float = 0.5,
    ) -> None:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT clock_timestamp() + make_interval(secs => %s)",
                    (delay_seconds,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ReplayJourneyInvariantError("resume time unavailable")
                cursor.execute(
                    "SELECT public.sleepagent_wait_demo_journey("
                    "%s, %s, %s, %s, %s)",
                    (
                        context.claim.work_id,
                        context.claim.lease_generation,
                        context.claim.fencing_token,
                        phase,
                        row[0],
                    ),
                )
                changed = cursor.fetchone()
            finally:
                cursor.close()
            if not changed or changed[0] is not True:
                raise ReplayJourneyLeaseLost("journey WAIT lost its fence")
            uow.commit()


class ReplayJourneyWorkHandler:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory[Any],
        cipher: RawPayloadCipher,
        retention_keys: PostgresRetentionKeyCoordinator | None = None,
        retention: timedelta = timedelta(days=30),
        repository: Any | None = None,
        ingress: Any | None = None,
        generator: Any | None = None,
        adapter: Any | None = None,
        registry: Any | None = None,
        observation_semantics_version: str = "v1",
    ) -> None:
        self.uow_factory = uow_factory
        self.repository = repository or PostgresReplayJourneyRepository(uow_factory)
        self.ingress = ingress or ReplayIngressHandler(
            uow_factory,
            cipher=cipher,
            retention_keys=retention_keys,
            retention=retention,
        )
        self.generator = generator or CanonicalReplayGenerator()
        self.adapter = adapter
        self.registry = registry or load_replay_seed_registry()
        if observation_semantics_version not in {"v1", "v2"}:
            raise ValueError("unsupported observation semantics version")
        self.observation_semantics_version = observation_semantics_version
        self._adapted: dict[str, AdaptedReplay] = {}

    def __call__(self, context: WorkContext) -> WorkResult:
        try:
            scope = _journey_scope(context)
            phase = str(context.claim.metadata.get("phase", ""))
            payload = context.claim.payload
            if self.repository.deadline_exceeded(
                scope,
                journey_id=context.claim.work_id,
            ):
                raise ReplayJourneyTerminalError("journey_deadline_exceeded")
            if phase == "accepted":
                if not self.repository.bootstrap(
                    scope,
                    root_operation_id=str(payload["root_operation_id"]),
                    journey_id=context.claim.work_id,
                ):
                    raise ReplayJourneyLeaseLost("bootstrap lost its fence")
                return _handler_owned("authority_bootstrapped")
            if phase == "staging_input":
                return self._stage_one_batch(context, scope)
            if phase == "waiting_normalization":
                progress = self.repository.normalization_progress(
                    scope,
                    journey_id=context.claim.work_id,
                )
                expected = len(_initial_items(self._adapted_ingress(payload)))
                if progress.total != expected:
                    raise ReplayJourneyInvariantError(
                        "normalization work count does not match ingress manifest"
                    )
                if progress.terminal_failed:
                    raise ReplayJourneyTerminalError("normalization_failed")
                if progress.succeeded != expected:
                    self.repository.wait(
                        scope,
                        context=context,
                        phase="waiting_normalization",
                    )
                    return _handler_owned("waiting_normalization")
                self.repository.advance(
                    scope,
                    context=context,
                    from_phase="waiting_normalization",
                    to_phase="waiting_episode",
                    checkpoint="normalization_complete",
                    payload={"normalization_work_count": expected},
                )
                return _handler_owned("normalization_complete")
            if phase == "waiting_episode":
                episode = self.repository.episode_progress(scope)
                if episode is None:
                    self.repository.wait(
                        scope,
                        context=context,
                        phase="waiting_episode",
                    )
                    return _handler_owned("waiting_episode")
                self.repository.advance(
                    scope,
                    context=context,
                    from_phase="waiting_episode",
                    to_phase="waiting_fast_path",
                    checkpoint="episode_committed",
                    payload={
                        "night_episode_id": episode.night_episode_id,
                        "night_episode_revision_id": (
                            episode.night_episode_revision_id
                        ),
                        "episode_local_date": episode.episode_local_date,
                        "assignment_basis": episode.assignment_basis,
                        "membership_count": episode.membership_count,
                        "fast_path_operation_id": (
                            episode.fast_path_operation_id
                        ),
                    },
                )
                return _handler_owned("episode_committed")
            episode = self.repository.episode_progress(scope)
            if episode is None:
                raise ReplayJourneyTerminalError("episode_not_committed")
            fast_path = self.repository.fast_path_progress(
                scope,
                episode=episode,
            )
            if phase == "waiting_fast_path":
                if fast_path.status != "succeeded":
                    if fast_path.status in {"pending", "retry", "running"}:
                        self.repository.wait(
                            scope,
                            context=context,
                            phase="waiting_fast_path",
                        )
                        return _handler_owned("waiting_fast_path")
                    raise ReplayJourneyTerminalError("episode_not_committed")
                assert fast_path.product_operation_id is not None
                self.repository.advance(
                    scope,
                    context=context,
                    from_phase="waiting_fast_path",
                    to_phase="waiting_product",
                    checkpoint="fast_path_committed",
                    payload={
                        "fast_path_operation_id": (
                            fast_path.fast_path_operation_id
                        ),
                        "quality_assessment_id": (
                            fast_path.quality_assessment_id
                        ),
                        "current_risk_id": fast_path.current_risk_id,
                        "product_operation_id": (
                            fast_path.product_operation_id
                        ),
                    },
                )
                return _handler_owned("fast_path_committed")
            if fast_path.status != "succeeded" or not fast_path.product_operation_id:
                raise ReplayJourneyTerminalError("product_failed")
            product = self.repository.product_progress(
                scope,
                episode=episode,
                product_operation_id=fast_path.product_operation_id,
            )
            if phase == "waiting_product":
                if product.status != "succeeded":
                    if product.status in {"pending", "retry", "running"}:
                        self.repository.wait(
                            scope,
                            context=context,
                            phase="waiting_product",
                        )
                        return _handler_owned("waiting_product")
                    raise ReplayJourneyTerminalError("product_failed")
                assert product.analysis_revision_id is not None
                self.repository.advance(
                    scope,
                    context=context,
                    from_phase="waiting_product",
                    to_phase="verifying_views",
                    checkpoint="product_committed",
                    payload={
                        "product_operation_id": product.product_operation_id,
                        "analysis_revision_id": product.analysis_revision_id,
                    },
                )
                return _handler_owned("product_committed")
            if phase != "verifying_views":
                raise ReplayJourneyInvariantError("unknown replay journey phase")
            if product.status != "succeeded" or not product.analysis_revision_id:
                raise ReplayJourneyTerminalError("product_failed")
            projections = self.repository.projection_progress(
                scope,
                episode=episode,
                analysis_revision_id=product.analysis_revision_id,
            )
            root_result = {
                "schema_version": "demo_journey_result.v1",
                "subject_ref": scope.subject_id,
                "night_episode_id": episode.night_episode_id,
                "night_episode_revision_id": episode.night_episode_revision_id,
                "fast_path_operation_id": fast_path.fast_path_operation_id,
                "product_operation_id": product.product_operation_id,
                "analysis_revision_id": projections.analysis_revision_id,
                "role_projection_ids": projections.role_projection_ids,
                "manifest_sha256": str(payload["manifest_sha256"]),
                "data_mode": "replay",
                "synthetic_non_release": True,
            }
            self.repository.succeed(
                scope,
                context=context,
                result=root_result,
            )
            return _handler_owned("journey_succeeded", **root_result)
        except ReplayJourneyLeaseLost:
            context.mark_lease_lost()
            return WorkResult(
                disposition=WorkDisposition.OUTCOME_UNKNOWN,
                error_code="replay_journey_lease_lost",
            )
        except ReplayJourneyTerminalError as exc:
            return WorkResult(
                disposition=WorkDisposition.TERMINAL,
                error_code=exc.code,
            )
        except (KeyError, TypeError, ValueError, ReplayJourneyInvariantError):
            return WorkResult(
                disposition=WorkDisposition.TERMINAL,
                error_code="scenario_contract_invalid",
            )

    def _stage_one_batch(
        self,
        context: WorkContext,
        scope: UowScope,
    ) -> WorkResult:
        payload = context.claim.payload
        adapted = self._adapted_ingress(payload)
        manifest_sha256 = _sha256(adapted.manifest.model_dump(mode="json"))
        if manifest_sha256 != str(payload["manifest_sha256"]):
            raise ReplayJourneyInvariantError("journey manifest hash drifted")
        self.repository.ensure_manifest(
            scope,
            journey_id=context.claim.work_id,
            seed_id=str(payload["seed_id"]),
            adapted=adapted,
            manifest_sha256=manifest_sha256,
        )
        if isinstance(adapted, AdaptedReplayIngressV2):
            self.repository.stage_future_items(
                scope,
                journey_id=context.claim.work_id,
                manifest_sha256=manifest_sha256,
                items=adapted.future_items,
            )
        ingress_items = _initial_items(adapted)
        progress = self.repository.batch_progress(
            scope,
            journey_id=context.claim.work_id,
        )
        if progress.committed_observation_count >= len(ingress_items):
            raise ReplayJourneyInvariantError(
                "staging phase remained active after all batches committed"
            )
        batch_size = int(payload["batch_size"])
        start = progress.committed_observation_count
        items = ingress_items[start : start + batch_size]
        if not items:
            raise ReplayJourneyInvariantError("next replay batch is empty")
        results = self.ingress.ingest(
            scope,
            ReplayRawBatch(items=tuple(item.observation for item in items)),
            order=ReplayIngressOrder(
                journey_id=context.claim.work_id,
                manifest_sha256=manifest_sha256,
                stream_key=items[0].stream_key,
                first_sequence=items[0].sequence,
                predecessor_raw_ingress_record_id=(
                    progress.predecessor_raw_ingress_record_id
                ),
                predecessor_work_id=progress.predecessor_work_id,
            ),
        )
        final_batch = start + len(items) == len(ingress_items)
        self.repository.commit_batch_checkpoint(
            scope,
            context=context,
            batch_sequence=progress.committed_batch_count + 1,
            items=items,
            predecessor_batch_id=progress.predecessor_batch_id,
            results=results,
            final_batch=final_batch,
        )
        return _handler_owned(
            "ingress_staged",
            batch_sequence=progress.committed_batch_count + 1,
            observation_count=len(items),
            final_batch=final_batch,
        )

    def _adapted_ingress(
        self,
        payload: Mapping[str, Any],
    ) -> AdaptedReplay:
        seed_id = str(payload["seed_id"])
        cached = self._adapted.get(seed_id)
        if cached is not None:
            return cached
        seed = next(
            (value for value in self.registry.seeds if value.seed_id == seed_id),
            None,
        )
        if seed is None:
            raise ReplayJourneyInvariantError("journey seed is not in server registry")
        scenario = verify_packaged_seed(seed)
        generated = self.generator.generate(scenario)
        adapter = self.adapter or replay_external_fact_adapter(
            seed.adapter_version,
            observation_semantics_version=self.observation_semantics_version,
        )
        adapted = adapter.adapt(scenario, generated)
        if (
            adapted.manifest.scenario_sha256 != str(payload["scenario_sha256"])
            or adapted.manifest.generator_version
            != str(payload["generator_version"])
            or adapted.manifest.adapter_version != str(payload["adapter_version"])
            or str(payload["schema_manifest_sha256"])
            != MIGRATION_MANIFEST_SHA256
        ):
            raise ReplayJourneyInvariantError("journey component pins drifted")
        self._adapted[seed_id] = adapted
        return adapted


def _initial_items(adapted: AdaptedReplay) -> tuple[ReplayIngressItem, ...]:
    if isinstance(adapted, AdaptedReplayIngressV2):
        return adapted.initial_items
    return adapted.items


def _journey_scope(context: WorkContext) -> UowScope:
    claim = context.claim
    if (
        claim.queue != "replay_journey"
        or claim.operation_id is None
        or claim.metadata.get("work_kind") != "journey"
        or claim.subject_id is None
    ):
        raise ReplayJourneyInvariantError("invalid replay journey claim")
    scope = context.store.uow_scope_for_claim(claim)
    expected = {
        "namespace_id": claim.namespace_id,
        "namespace_generation": claim.namespace_generation,
        "data_mode": claim.data_mode,
        "run_id": claim.run_id,
        "arm_id": claim.arm_id,
        "subject_id": claim.subject_id,
        "worker_instance": claim.worker_instance,
    }
    if scope.process_role != "worker" or any(
        getattr(scope, key) != value for key, value in expected.items()
    ):
        raise ReplayJourneyInvariantError("journey claim scope drifted")
    snapshot = claim.authorization_snapshot
    expected_snapshot: Mapping[str, object] = {
        "schema_version": "workload_authorization_snapshot.v1",
        "workload_principal_id": scope.service_principal_id,
        "namespace_id": scope.namespace_id,
        "namespace_generation": scope.namespace_generation,
        "data_mode": scope.data_mode,
        "run_id": scope.run_id,
        "arm_id": scope.arm_id,
        "subject_id": scope.subject_id,
        "purpose": scope.purpose,
        "allowed_handler": "replay_journey",
        "authorization_epoch": scope.authorization_epoch,
        "privacy_epoch": scope.privacy_epoch,
        "retrieval_policy_epoch": scope.retrieval_policy_epoch,
    }
    if set(snapshot) != set(expected_snapshot) or any(
        type(snapshot[name]) is not type(expected)
        or snapshot[name] != expected
        for name, expected in expected_snapshot.items()
    ):
        raise ReplayJourneyInvariantError("journey authorization snapshot drifted")
    return scope


def _heartbeat(cursor: Any, context: WorkContext) -> bool:
    claim = context.claim
    cursor.execute(
        "SELECT public.sleepagent_heartbeat_demo_journey(%s, %s, %s, %s)",
        (
            claim.work_id,
            claim.lease_generation,
            claim.fencing_token,
            int(claim.metadata["lease_seconds"]),
        ),
    )
    row = cursor.fetchone()
    return bool(row and row[0] is True)


def _handler_owned(
    checkpoint: str,
    **values: Any,
) -> WorkResult:
    return WorkResult(
        disposition=WorkDisposition.SUCCEEDED,
        result={"checkpoint": checkpoint, **values},
        finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _json_object(value: object) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, Mapping):
        raise ReplayJourneyInvariantError("durable journey JSON is not an object")
    return {str(key): item for key, item in parsed.items()}


__all__ = [
    "PostgresReplayJourneyRepository",
    "ReplayJourneyInvariantError",
    "ReplayJourneyLeaseLost",
    "ReplayJourneyWorkHandler",
]
