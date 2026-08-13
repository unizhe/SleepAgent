# 本模块负责持久化队列的一项 Worker 职责，并以租约和围栏保护提交。
"""read-model durable ScenarioClock command handler."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Mapping

from sleepagent.config import BackendKeyProvider
from sleepagent.config import SleepBackendSettings
from sleepagent.persistence.uow import UowScope
from sleepagent.persistence.migrations import SCENARIO_CLOCK_AUTHORITY_FUNCTION
from sleepagent.workers.retention import (
    LocalTestRetentionKeyEnvelope,
    PostgresRetentionKeyCoordinator,
)
from sleepagent.domain.episodes import UUID7Generator
from sleepagent.domain.postgres_slice import (
    IngressResult,
    RawPayloadCipher,
    ReplayIngressHandler,
    ReplayIngressOrder,
    ReplayRawBatch,
)
from sleepagent.simulation.replay_ingress import ReplayIngressItem
from sleepagent.workers.runtime import (
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
    WorkResult,
    worker_uow_factory,
)


DEMO_ADVANCE_QUEUE = "demo_advance"
RECONCILIATION_QUEUE = "reconciliation"
EPISODE_DATE_RECONCILIATION_OPERATION = "episode_date_reconciliation"


class DemoAdvanceError(RuntimeError):
    pass


class DemoAdvanceLeaseLost(DemoAdvanceError):
    pass


class DateReconciliationError(RuntimeError):
    pass


class DateReconciliationLeaseLost(DateReconciliationError):
    pass


class EpisodeDateReconciliationHandler:
    """Reject a conflicting candidate while preserving its canonical owner."""

    def __init__(self, *, id_generator: UUID7Generator | None = None) -> None:
        self.id_generator = id_generator or UUID7Generator()

    def __call__(self, context: WorkContext) -> WorkResult:
        try:
            scope = _reconciliation_scope(context)
            payload = _date_reconciliation_payload(context.claim.payload)
            result = self._commit(context, scope=scope, payload=payload)
            return WorkResult(
                disposition=WorkDisposition.SUCCEEDED,
                result=result,
                finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
            )
        except DateReconciliationLeaseLost:
            context.mark_lease_lost()
            return WorkResult(
                disposition=WorkDisposition.OUTCOME_UNKNOWN,
                error_code=(
                    "episode_date_reconciliation_lease_lost_"
                    "reconciliation_required"
                ),
            )
        except (DateReconciliationError, KeyError, TypeError, ValueError):
            return WorkResult(
                disposition=WorkDisposition.TERMINAL,
                error_code="episode_date_reconciliation_invariant_violation",
            )

    def _commit(
        self,
        context: WorkContext,
        *,
        scope: UowScope,
        payload: Mapping[str, str],
    ) -> dict[str, Any]:
        claim = context.claim
        event_id = self.id_generator()
        audit_id = self.id_generator()
        with worker_uow_factory(context).begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT reconciliation.reconciliation_id, "
                    "reconciliation.candidate_night_episode_id, "
                    "reconciliation.conflicting_night_episode_id, "
                    "reconciliation.candidate_revision_id, "
                    "reconciliation.proposed_episode_local_date, "
                    "reconciliation.status, candidate_revision.date_state, "
                    "candidate_revision.date_conflict, "
                    "candidate_revision.episode_local_date, "
                    "candidate_episode.current_revision_id, "
                    "owner.current_revision_id, owner.date_state, "
                    "owner.date_conflict, owner.episode_local_date, "
                    "operation.policy_sha256 "
                    "FROM public.sleep_domain_operations AS operation "
                    "JOIN public.backend_episode_date_reconciliation "
                    "AS reconciliation ON reconciliation.operation_id = "
                    "operation.operation_id AND reconciliation.namespace_id = "
                    "operation.namespace_id AND reconciliation.data_mode = "
                    "operation.data_mode AND "
                    "reconciliation.namespace_generation = "
                    "operation.namespace_generation AND "
                    "reconciliation.subject_id = operation.subject_id "
                    "JOIN public.sleep_domain_night_episode_revisions "
                    "AS candidate_revision ON "
                    "candidate_revision.night_episode_revision_id = "
                    "reconciliation.candidate_revision_id AND "
                    "candidate_revision.namespace_id = reconciliation.namespace_id "
                    "AND candidate_revision.data_mode = reconciliation.data_mode "
                    "AND candidate_revision.namespace_generation = "
                    "reconciliation.namespace_generation "
                    "JOIN public.sleep_domain_night_episodes AS candidate_episode "
                    "ON candidate_episode.night_episode_id = "
                    "reconciliation.candidate_night_episode_id AND "
                    "candidate_episode.namespace_id = reconciliation.namespace_id "
                    "AND candidate_episode.data_mode = reconciliation.data_mode "
                    "AND candidate_episode.namespace_generation = "
                    "reconciliation.namespace_generation "
                    "JOIN public.sleep_domain_night_episodes AS owner "
                    "ON owner.night_episode_id = "
                    "reconciliation.conflicting_night_episode_id AND "
                    "owner.namespace_id = reconciliation.namespace_id "
                    "AND owner.data_mode = reconciliation.data_mode "
                    "AND owner.namespace_generation = "
                    "reconciliation.namespace_generation "
                    "WHERE operation.operation_id = %s "
                    "AND operation.operation_type = %s "
                    "AND operation.queue_name = %s "
                    "AND operation.target_resource_id = "
                    "reconciliation.reconciliation_id "
                    "AND operation.status = 'running' "
                    "AND operation.cas_version = %s "
                    "AND operation.lease_generation = %s "
                    "AND operation.fencing_token = %s "
                    "AND operation.worker_instance = %s "
                    "AND operation.lease_expires_at > clock_timestamp() "
                    "FOR UPDATE OF operation, reconciliation, "
                    "candidate_episode, owner, candidate_revision",
                    (
                        claim.work_id,
                        EPISODE_DATE_RECONCILIATION_OPERATION,
                        RECONCILIATION_QUEUE,
                        claim.operation_version,
                        claim.lease_generation,
                        claim.fencing_token,
                        claim.worker_instance,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise DateReconciliationLeaseLost(
                        "date reconciliation operation fence was lost"
                    )
                stored = {
                    "reconciliation_id": str(row[0]),
                    "candidate_night_episode_id": str(row[1]),
                    "conflicting_night_episode_id": str(row[2]),
                    "candidate_revision_id": str(row[3]),
                    "proposed_episode_local_date": row[4].isoformat(),
                }
                if any(payload[name] != value for name, value in stored.items()):
                    raise DateReconciliationError(
                        "date reconciliation payload drifted from durable state"
                    )
                if (
                    str(row[5]) != "reconciliation_required"
                    or str(row[6]) != "conflict"
                    or row[7] is not True
                    or row[8] != row[4]
                    or row[9] is None
                    or str(row[9]) == stored["candidate_revision_id"]
                    or row[10] is None
                    or str(row[11]) != "finalized"
                    or row[12] is not False
                    or row[13] != row[4]
                ):
                    raise DateReconciliationError(
                        "date conflict no longer has one preserved canonical owner"
                    )
                cursor.execute(
                    "SELECT count(*), min(night_episode_id) FROM "
                    "public.sleep_domain_night_episodes WHERE namespace_id = %s "
                    "AND data_mode = %s AND namespace_generation = %s "
                    "AND subject_id = %s AND episode_local_date = %s "
                    "AND protocol_version >= 2 AND date_state = 'finalized' "
                    "AND date_conflict = FALSE",
                    (
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.subject_id,
                        row[4],
                    ),
                )
                owner_count = cursor.fetchone()
                if (
                    owner_count is None
                    or int(owner_count[0]) != 1
                    or str(owner_count[1])
                    != stored["conflicting_night_episode_id"]
                ):
                    raise DateReconciliationError(
                        "canonical owner cardinality changed"
                    )
                resolution = {
                    "schema_version": "episode_date_reconciliation_resolution.v1",
                    **stored,
                    "decision": "reject_candidate",
                    "reason_code": "existing_canonical_date_owner_preserved",
                    "canonical_night_episode_revision_id": str(row[10]),
                    "candidate_promoted": False,
                    "product_work_enqueued": False,
                }
                cursor.execute(
                    "UPDATE public.backend_episode_date_reconciliation SET "
                    "status = 'rejected', reason_code = %s, "
                    "resolution_json = %s::jsonb, "
                    "resolved_at = clock_timestamp() "
                    "WHERE reconciliation_id = %s "
                    "AND operation_id = %s "
                    "AND status = 'reconciliation_required'",
                    (
                        "existing_canonical_date_owner_preserved",
                        _json(resolution),
                        stored["reconciliation_id"],
                        claim.work_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise DateReconciliationError(
                        "date reconciliation decision CAS was lost"
                    )
                event = {
                    "schema_version": "committed_event.v2",
                    "event_type": (
                        "NIGHT_EPISODE_DATE_RECONCILIATION_REJECTED"
                    ),
                    "operation_id": claim.work_id,
                    **resolution,
                    "synthetic_non_release": scope.data_mode == "replay",
                }
                cursor.execute(
                    "INSERT INTO public.sleep_domain_domain_outbox ("
                    "event_id, namespace_id, data_mode, event_type, "
                    "aggregate_type, aggregate_id, aggregate_version, "
                    "per_aggregate_sequence, subject_id, operation_id, status, "
                    "available_at, event_json, created_at, protocol_version, "
                    "namespace_generation, run_id, arm_id) VALUES ("
                    "%s, %s, %s, %s, 'EpisodeDateReconciliation', %s, 1, 1, "
                    "%s, %s, 'committed', clock_timestamp(), %s::jsonb, "
                    "clock_timestamp(), 2, %s, %s, %s)",
                    (
                        event_id,
                        scope.namespace_id,
                        scope.data_mode,
                        event["event_type"],
                        stored["reconciliation_id"],
                        scope.subject_id,
                        claim.work_id,
                        _json(event),
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                    ),
                )
                result = {
                    "schema_version": "episode_date_reconciliation_result.v1",
                    **resolution,
                }
                cursor.execute(
                    "UPDATE public.sleep_domain_operations SET "
                    "status = 'succeeded', outcome_class = 'succeeded', "
                    "operation_json = operation_json || "
                    "jsonb_build_object('result', %s::jsonb), "
                    "cas_version = cas_version + 1, "
                    "updated_at = clock_timestamp(), lease_owner = NULL, "
                    "lease_expires_at = NULL, fencing_token = NULL, "
                    "worker_instance = NULL, heartbeat_at = NULL "
                    "WHERE operation_id = %s AND status = 'running' "
                    "AND cas_version = %s AND lease_generation = %s "
                    "AND fencing_token = %s AND worker_instance = %s "
                    "AND lease_expires_at > clock_timestamp() "
                    "RETURNING cas_version",
                    (
                        _json(result),
                        claim.work_id,
                        claim.operation_version,
                        claim.lease_generation,
                        claim.fencing_token,
                        claim.worker_instance,
                    ),
                )
                if cursor.fetchone() is None:
                    raise DateReconciliationLeaseLost(
                        "date reconciliation terminal CAS was lost"
                    )
                cursor.execute(
                    "INSERT INTO public.backend_authorization_audit ("
                    "audit_id, namespace_id, data_mode, subject_id, principal_id, "
                    "actor_id, binding_id, decision, reason_code, policy_sha256, "
                    "authorization_epoch, privacy_epoch, retrieval_policy_epoch, "
                    "audit_json, occurred_at) VALUES ("
                    "%s, %s, %s, %s, %s, NULL, NULL, 'allow', %s, %s, "
                    "%s, %s, %s, %s::jsonb, clock_timestamp())",
                    (
                        audit_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.subject_id,
                        scope.service_principal_id,
                        "episode_date_reconciliation_committed",
                        str(row[14]),
                        scope.authorization_epoch,
                        scope.privacy_epoch,
                        scope.retrieval_policy_epoch,
                        _json(
                            {
                                "operation_id": claim.work_id,
                                "reconciliation_id": stored["reconciliation_id"],
                                "decision": "reject_candidate",
                            }
                        ),
                    ),
                )
            finally:
                cursor.close()
            uow.commit()
        return result


class DemoAdvanceHandler:
    def __init__(
        self,
        *,
        cipher: RawPayloadCipher,
        retention_keys: PostgresRetentionKeyCoordinator | None = None,
        retention: timedelta = timedelta(days=30),
        id_generator: UUID7Generator | None = None,
    ) -> None:
        self.cipher = cipher
        self.retention_keys = retention_keys
        self.retention = retention
        self.id_generator = id_generator or UUID7Generator()

    def __call__(self, context: WorkContext) -> WorkResult:
        try:
            scope = _advance_scope(context)
            payload = context.claim.payload.get("payload")
            if not isinstance(payload, Mapping):
                raise DemoAdvanceError("advance payload is missing")
            seconds = payload.get("seconds")
            if type(seconds) is not int or seconds < 1 or seconds > 604_800:
                raise DemoAdvanceError("advance seconds are invalid")
            result = self._commit(context, scope=scope, seconds=seconds)
            return WorkResult(
                disposition=WorkDisposition.SUCCEEDED,
                result=result,
                finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
            )
        except DemoAdvanceLeaseLost:
            context.mark_lease_lost()
            return WorkResult(
                disposition=WorkDisposition.OUTCOME_UNKNOWN,
                error_code="demo_advance_lease_lost_reconciliation_required",
            )
        except (DemoAdvanceError, KeyError, TypeError, ValueError):
            return WorkResult(
                disposition=WorkDisposition.TERMINAL,
                error_code="demo_advance_contract_invalid",
            )

    def _commit(
        self,
        context: WorkContext,
        *,
        scope: UowScope,
        seconds: int,
    ) -> dict[str, Any]:
        claim = context.claim
        receipt_id = self.id_generator()
        event_id = self.id_generator()
        audit_id = self.id_generator()
        with worker_uow_factory(context).begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    f"SELECT public.{SCENARIO_CLOCK_AUTHORITY_FUNCTION}(%s)",
                    (claim.work_id,),
                )
                authority = cursor.fetchone()
                if authority is None or authority[0] is not True:
                    raise DemoAdvanceError("advance authority is no longer current")
                cursor.execute(
                    "SELECT operation.policy_sha256, "
                    "COALESCE(clock.scenario_now, "
                    "(seed.metadata_json ->> 'scenario_clock_start')::timestamptz), "
                    "COALESCE(clock.clock_version, 0) "
                    "FROM public.sleep_domain_operations AS operation "
                    "JOIN public.backend_demo_journeys AS journey "
                    "ON journey.namespace_id = operation.namespace_id "
                    "AND journey.data_mode = operation.data_mode "
                    "AND journey.namespace_generation = "
                    "operation.namespace_generation "
                    "AND journey.run_id = operation.run_id "
                    "AND journey.arm_id = operation.arm_id "
                    "AND journey.subject_id = operation.subject_id "
                    "AND journey.phase = 'succeeded' "
                    "JOIN public.backend_demo_seed_allowlist AS seed "
                    "ON seed.seed_id = journey.seed_id AND seed.active "
                    "LEFT JOIN public.backend_replay_scenario_clocks AS clock "
                    "ON clock.namespace_id = operation.namespace_id "
                    "AND clock.data_mode = operation.data_mode "
                    "AND clock.namespace_generation = operation.namespace_generation "
                    "AND clock.run_id = operation.run_id "
                    "AND clock.arm_id = operation.arm_id "
                    "WHERE operation.operation_id = %s "
                    "AND operation.status = 'running' "
                    "AND operation.cas_version = %s "
                    "AND operation.lease_generation = %s "
                    "AND operation.fencing_token = %s "
                    "AND operation.worker_instance = %s "
                    "AND operation.lease_expires_at > clock_timestamp() "
                    "FOR UPDATE OF operation",
                    (
                        claim.work_id,
                        claim.operation_version,
                        claim.lease_generation,
                        claim.fencing_token,
                        claim.worker_instance,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise DemoAdvanceLeaseLost("advance operation fence was lost")
                policy_sha256 = str(row[0])
                from_time = row[1]
                from_version = int(row[2])
                cursor.execute(
                    "SELECT %s::timestamptz + make_interval(secs => %s)",
                    (from_time, seconds),
                )
                to_row = cursor.fetchone()
                if to_row is None:
                    raise DemoAdvanceError("database could not advance ScenarioClock")
                to_time = to_row[0]
                cursor.execute("SELECT clock_timestamp()")
                control_row = cursor.fetchone()
                if control_row is None:
                    raise DemoAdvanceError("database ControlClock is unavailable")
                control_time = control_row[0]
                if from_version == 0:
                    cursor.execute(
                        "INSERT INTO public.backend_replay_scenario_clocks ("
                        "namespace_id, data_mode, namespace_generation, run_id, "
                        "arm_id, scenario_now, clock_version, "
                        "last_command_operation_id, command_lease_generation, "
                        "command_fencing_token) VALUES ("
                        "%s, %s, %s, %s, %s, %s, 1, %s, %s, %s)",
                        (
                            scope.namespace_id,
                            scope.data_mode,
                            scope.namespace_generation,
                            scope.run_id,
                            scope.arm_id,
                            to_time,
                            claim.work_id,
                            claim.lease_generation,
                            claim.fencing_token,
                        ),
                    )
                else:
                    cursor.execute(
                        "UPDATE public.backend_replay_scenario_clocks SET "
                        "scenario_now = %s, clock_version = clock_version + 1, "
                        "last_command_operation_id = %s, "
                        "command_lease_generation = %s, "
                        "command_fencing_token = %s, "
                        "updated_at = clock_timestamp() "
                        "WHERE namespace_id = %s AND data_mode = %s "
                        "AND namespace_generation = %s AND run_id = %s "
                        "AND arm_id = %s AND clock_version = %s",
                        (
                            to_time,
                            claim.work_id,
                            claim.lease_generation,
                            claim.fencing_token,
                            scope.namespace_id,
                            scope.data_mode,
                            scope.namespace_generation,
                            scope.run_id,
                            scope.arm_id,
                            from_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise DemoAdvanceError("ScenarioClock CAS was lost")
                cursor.execute(
                    "SELECT staged.staged_fact_id, staged.journey_id, "
                    "staged.manifest_sha256, staged.stream_key, "
                    "staged.stream_sequence, staged.fact_sha256, "
                    "staged.fact_json FROM public.backend_replay_staged_facts "
                    "AS staged WHERE staged.namespace_id = %s "
                    "AND staged.data_mode = %s "
                    "AND staged.namespace_generation = %s "
                    "AND staged.run_id = %s AND staged.arm_id = %s "
                    "AND staged.subject_id = %s AND staged.status = 'staged' "
                    "AND staged.release_at <= %s "
                    "ORDER BY staged.stream_sequence FOR UPDATE",
                    (
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        to_time,
                    ),
                )
                staged_rows = tuple(cursor.fetchall())
                released_count = self._release_staged_facts(
                    uow.connection,
                    cursor,
                    scope=scope,
                    context=context,
                    staged_rows=staged_rows,
                    committed_at=control_time,
                )
                receipt = {
                    "schema_version": "demo_advance_receipt.v1",
                    "operation_id": claim.work_id,
                    "generation": scope.namespace_generation,
                    "from_scenario_time": from_time.isoformat(),
                    "to_scenario_time": to_time.isoformat(),
                    "from_clock_version": from_version,
                    "to_clock_version": from_version + 1,
                    "released_fact_count": released_count,
                }
                cursor.execute(
                    "INSERT INTO public.backend_demo_advance_receipts ("
                    "advance_receipt_id, namespace_id, data_mode, "
                    "namespace_generation, run_id, arm_id, subject_id, "
                    "operation_id, from_scenario_time, to_scenario_time, "
                    "from_clock_version, to_clock_version, released_fact_count, "
                    "receipt_json) VALUES ("
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s::jsonb)",
                    (
                        receipt_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        scope.subject_id,
                        claim.work_id,
                        from_time,
                        to_time,
                        from_version,
                        from_version + 1,
                        released_count,
                        _json(receipt),
                    ),
                )
                event = {
                    "schema_version": "committed_event.v2",
                    "event_type": "DEMO_SCENARIO_CLOCK_ADVANCED",
                    "operation_id": claim.work_id,
                    "generation": scope.namespace_generation,
                    "clock_version": from_version + 1,
                    "data_mode": "replay",
                    "synthetic_non_release": True,
                }
                cursor.execute(
                    "INSERT INTO public.sleep_domain_domain_outbox ("
                    "event_id, namespace_id, data_mode, event_type, "
                    "aggregate_type, aggregate_id, aggregate_version, "
                    "per_aggregate_sequence, subject_id, operation_id, status, "
                    "available_at, event_json, created_at, protocol_version, "
                    "namespace_generation, run_id, arm_id) VALUES ("
                    "%s, %s, %s, 'DEMO_SCENARIO_CLOCK_ADVANCED', "
                    "'ScenarioClock', %s, %s, %s, %s, %s, 'committed', "
                    "clock_timestamp(), %s::jsonb, clock_timestamp(), 2, %s, %s, %s)",
                    (
                        event_id,
                        scope.namespace_id,
                        scope.data_mode,
                        f"{scope.namespace_id}:{scope.namespace_generation}",
                        from_version + 1,
                        from_version + 1,
                        scope.subject_id,
                        claim.work_id,
                        _json(event),
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                    ),
                )
                result = {
                    "schema_version": "demo_advance_result.v1",
                    "scenario_time": to_time.isoformat(),
                    "generation": scope.namespace_generation,
                    "clock_version": from_version + 1,
                    "advance_receipt_id": receipt_id,
                    "released_fact_count": released_count,
                    "data_mode": "replay",
                    "synthetic_non_release": True,
                }
                cursor.execute(
                    "UPDATE public.sleep_domain_operations SET "
                    "status = 'succeeded', outcome_class = 'succeeded', "
                    "operation_json = operation_json || "
                    "jsonb_build_object('result', %s::jsonb), "
                    "cas_version = cas_version + 1, updated_at = clock_timestamp(), "
                    "lease_owner = NULL, lease_expires_at = NULL, "
                    "fencing_token = NULL, worker_instance = NULL, "
                    "heartbeat_at = NULL WHERE operation_id = %s "
                    "AND status = 'running' AND cas_version = %s "
                    "AND lease_generation = %s AND fencing_token = %s "
                    "AND worker_instance = %s "
                    "AND lease_expires_at > clock_timestamp() RETURNING cas_version",
                    (
                        _json(result),
                        claim.work_id,
                        claim.operation_version,
                        claim.lease_generation,
                        claim.fencing_token,
                        claim.worker_instance,
                    ),
                )
                if cursor.fetchone() is None:
                    raise DemoAdvanceLeaseLost("advance terminal CAS was lost")
                cursor.execute(
                    "INSERT INTO public.backend_authorization_audit ("
                    "audit_id, namespace_id, data_mode, subject_id, principal_id, "
                    "actor_id, binding_id, decision, reason_code, policy_sha256, "
                    "authorization_epoch, privacy_epoch, retrieval_policy_epoch, "
                    "audit_json, occurred_at) VALUES ("
                    "%s, %s, %s, %s, %s, NULL, NULL, 'allow', "
                    "'demo_advance_committed', %s, %s, %s, %s, %s::jsonb, "
                    "clock_timestamp())",
                    (
                        audit_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.subject_id,
                        scope.service_principal_id,
                        policy_sha256,
                        scope.authorization_epoch,
                        scope.privacy_epoch,
                        scope.retrieval_policy_epoch,
                        _json(
                            {
                                "operation_id": claim.work_id,
                                "generation": scope.namespace_generation,
                                "clock_version": from_version + 1,
                            }
                        ),
                    ),
                )
            finally:
                cursor.close()
            uow.commit()
        return result

    def _release_staged_facts(
        self,
        connection: Any,
        cursor: Any,
        *,
        scope: UowScope,
        context: WorkContext,
        staged_rows: tuple[Any, ...],
        committed_at: Any,
    ) -> int:
        if not staged_rows:
            return 0
        items = tuple(ReplayIngressItem.model_validate(row[6]) for row in staged_rows)
        if any(
            str(row[3]) != item.stream_key
            or int(row[4]) != item.sequence
            or str(row[5]) != _sha256_bytes(item.canonical_bytes())
            for row, item in zip(staged_rows, items, strict=True)
        ):
            raise DemoAdvanceError("staged fact canonical bytes drifted")
        if len({str(row[1]) for row in staged_rows}) != 1 or len(
            {str(row[2]) for row in staged_rows}
        ) != 1:
            raise DemoAdvanceError("advance cannot cross journey manifests")
        cursor.execute(
            "SELECT raw.raw_ingress_record_id, work.work_id, "
            "raw.stream_sequence FROM public.sleep_domain_raw_inbox AS raw "
            "JOIN public.sleep_domain_normalization_work AS work "
            "ON work.raw_ingress_record_id = raw.raw_ingress_record_id "
            "AND work.namespace_id = raw.namespace_id "
            "AND work.data_mode = raw.data_mode "
            "WHERE raw.replay_journey_id = %s "
            "AND raw.ingress_stream_key = %s ORDER BY raw.stream_sequence DESC "
            "LIMIT 1",
            (str(staged_rows[0][1]), items[0].stream_key),
        )
        predecessor = cursor.fetchone()
        if predecessor is None or int(predecessor[2]) + 1 != items[0].sequence:
            raise DemoAdvanceError("staged release predecessor is unavailable")
        predecessor_raw_id = str(predecessor[0])
        predecessor_work_id = str(predecessor[1])
        ingress = ReplayIngressHandler(
            worker_uow_factory(context),
            cipher=self.cipher,
            retention_keys=self.retention_keys,
            retention=self.retention,
        )
        released_results: list[IngressResult] = []
        for offset in range(0, len(items), 100):
            batch = items[offset : offset + 100]
            results = ingress.ingest_in_uow(
                connection,
                scope,
                ReplayRawBatch(items=tuple(item.observation for item in batch)),
                order=ReplayIngressOrder(
                    journey_id=str(staged_rows[0][1]),
                    manifest_sha256=str(staged_rows[0][2]),
                    stream_key=batch[0].stream_key,
                    first_sequence=batch[0].sequence,
                    predecessor_raw_ingress_record_id=predecessor_raw_id,
                    predecessor_work_id=predecessor_work_id,
                ),
                committed_at=committed_at,
            )
            released_results.extend(results)
            predecessor_raw_id = results[-1].raw_ingress_record_id
            predecessor_work_id = results[-1].normalization_work_id
        for row, result in zip(staged_rows, released_results, strict=True):
            cursor.execute(
                "UPDATE public.backend_replay_staged_facts SET "
                "status = 'released', released_by_operation_id = %s, "
                "release_lease_generation = %s, release_fencing_token = %s, "
                "raw_ingress_record_id = %s, released_at = clock_timestamp() "
                "WHERE staged_fact_id = %s AND status = 'staged'",
                (
                    context.claim.work_id,
                    context.claim.lease_generation,
                    context.claim.fencing_token,
                    result.raw_ingress_record_id,
                    str(row[0]),
                ),
            )
            if cursor.rowcount != 1:
                raise DemoAdvanceError("staged fact release CAS was lost")
        return len(released_results)


def _advance_scope(context: WorkContext) -> UowScope:
    claim = context.claim
    if (
        claim.queue != DEMO_ADVANCE_QUEUE
        or claim.operation_id != claim.work_id
        or claim.subject_id is None
        or claim.metadata.get("operation_type") != "demo_advance"
    ):
        raise DemoAdvanceError("invalid demo advance claim")
    scope = context.store.uow_scope_for_claim(claim)
    expected_scope = {
        "namespace_id": claim.namespace_id,
        "namespace_generation": claim.namespace_generation,
        "data_mode": claim.data_mode,
        "run_id": claim.run_id,
        "arm_id": claim.arm_id,
        "subject_id": claim.subject_id,
        "worker_instance": claim.worker_instance,
    }
    if scope.process_role != "worker" or any(
        getattr(scope, name) != value for name, value in expected_scope.items()
    ):
        raise DemoAdvanceError("demo advance claim scope drifted")
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
        "allowed_handler": DEMO_ADVANCE_QUEUE,
        "authorization_epoch": scope.authorization_epoch,
        "privacy_epoch": scope.privacy_epoch,
        "retrieval_policy_epoch": scope.retrieval_policy_epoch,
    }
    snapshot = claim.authorization_snapshot
    if set(snapshot) != set(expected_snapshot) or any(
        type(snapshot[name]) is not type(value) or snapshot[name] != value
        for name, value in expected_snapshot.items()
    ):
        raise DemoAdvanceError("demo advance authority snapshot drifted")
    return scope


def _reconciliation_scope(context: WorkContext) -> UowScope:
    claim = context.claim
    if (
        claim.queue != RECONCILIATION_QUEUE
        or claim.operation_id != claim.work_id
        or claim.subject_id is None
        or claim.metadata.get("work_kind") != "operation"
        or claim.metadata.get("operation_type")
        != EPISODE_DATE_RECONCILIATION_OPERATION
        or claim.metadata.get("queue_name") != RECONCILIATION_QUEUE
    ):
        raise DateReconciliationError("invalid date reconciliation claim")
    scope = context.store.uow_scope_for_claim(claim)
    expected_scope = {
        "namespace_id": claim.namespace_id,
        "namespace_generation": claim.namespace_generation,
        "data_mode": claim.data_mode,
        "run_id": claim.run_id,
        "arm_id": claim.arm_id,
        "subject_id": claim.subject_id,
        "worker_instance": claim.worker_instance,
    }
    if scope.process_role != "worker" or any(
        getattr(scope, name) != value for name, value in expected_scope.items()
    ):
        raise DateReconciliationError(
            "date reconciliation claim scope drifted"
        )
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
        "allowed_handler": RECONCILIATION_QUEUE,
        "authorization_epoch": scope.authorization_epoch,
        "privacy_epoch": scope.privacy_epoch,
        "retrieval_policy_epoch": scope.retrieval_policy_epoch,
    }
    snapshot = claim.authorization_snapshot
    if set(snapshot) != set(expected_snapshot) or any(
        type(snapshot[name]) is not type(value) or snapshot[name] != value
        for name, value in expected_snapshot.items()
    ):
        raise DateReconciliationError(
            "date reconciliation authority snapshot drifted"
        )
    if claim.payload.get("authorization_snapshot") != snapshot:
        raise DateReconciliationError(
            "date reconciliation embedded authority snapshot drifted"
        )
    return scope


def _date_reconciliation_payload(
    value: Mapping[str, object],
) -> dict[str, str]:
    expected_keys = {
        "schema_version",
        "reconciliation_id",
        "candidate_night_episode_id",
        "candidate_revision_id",
        "conflicting_night_episode_id",
        "proposed_episode_local_date",
        "resolution_policy",
        "authorization_snapshot",
    }
    if set(value) != expected_keys:
        raise DateReconciliationError(
            "date reconciliation operation shape drifted"
        )
    if value.get("schema_version") != "episode_date_reconciliation_operation.v1":
        raise DateReconciliationError(
            "date reconciliation operation version is unsupported"
        )
    if value.get("resolution_policy") != "preserve_existing_canonical_owner.v1":
        raise DateReconciliationError(
            "date reconciliation policy version is unsupported"
        )
    result: dict[str, str] = {}
    for name in (
        "reconciliation_id",
        "candidate_night_episode_id",
        "candidate_revision_id",
        "conflicting_night_episode_id",
        "proposed_episode_local_date",
    ):
        item = value.get(name)
        if not isinstance(item, str) or not item.strip():
            raise DateReconciliationError(
                f"date reconciliation field {name!r} is invalid"
            )
        result[name] = item
    return result


def build_demo_worker_handlers(
    settings: SleepBackendSettings,
) -> dict[str, Any]:
    handlers: dict[str, Any] = {}
    if DEMO_ADVANCE_QUEUE in settings.worker_queues:
        key = BackendKeyProvider(settings.deployment_mode).encryption_key(
            settings.encryption_key_ref
        )
        cipher = RawPayloadCipher(key, key_id=settings.encryption_key_ref)
        retention_keys = PostgresRetentionKeyCoordinator(
            LocalTestRetentionKeyEnvelope(
                key,
                key_id=settings.encryption_key_ref,
            )
        )
        handlers[DEMO_ADVANCE_QUEUE] = DemoAdvanceHandler(
            cipher=cipher,
            retention_keys=retention_keys,
            retention=timedelta(seconds=settings.raw_retention_seconds),
        )
    return handlers


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


__all__ = [
    "DEMO_ADVANCE_QUEUE",
    "EPISODE_DATE_RECONCILIATION_OPERATION",
    "EpisodeDateReconciliationHandler",
    "RECONCILIATION_QUEUE",
    "DemoAdvanceHandler",
    "build_demo_worker_handlers",
]
