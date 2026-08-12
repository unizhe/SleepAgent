"""Fenced PostgreSQL handlers for Stage-2 public commands and interactions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from sleepagent.backend_settings import (
    DataMode,
    ModelMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.persistence.migrations import MIGRATION_MANIFEST_SHA256
from sleepagent.persistence.uow import UnitOfWorkFactory, UowScope
from sleepagent.sleep_domain.episode_v2 import UUID7Generator
from sleepagent.sleep_domain.worker_adapters import worker_uow_factory
from sleepagent.worker_runtime import (
    InvocationKind,
    LeaseClaim,
    LeaseLostError,
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
    WorkHandler,
    WorkResult,
)


SLEEP_COMMAND_QUEUE = "sleep_command"
PRODUCT_INTERACTION_QUEUE = "product_interaction"
MODEL_COMMANDS = {
    "interaction.start",
    "interaction.ask",
    "interaction.answer",
}
COMMAND_SCOPES = {
    "sleep_api.monitoring.activate.v1": "sleep:monitoring:write",
    "sleep_api.monitoring.deactivate.v1": "sleep:monitoring:write",
    "sleep_api.feedback.elder.v1": "sleep:feedback:self",
    "sleep_api.feedback.family.v1": "sleep:feedback:family",
    "sleep_api.reanalysis.v1": "sleep:reanalysis:write",
    "interaction.start": "product:sleep:interaction:write",
    "interaction.ask": "product:sleep:interaction:write",
    "interaction.answer": "product:sleep:interaction:answer",
    "interaction.confirm": "product:sleep:care:confirm",
    "interaction.decline": "product:sleep:care:confirm",
    "interaction.feedback": "product:sleep:feedback:write",
}


class Stage2WorkerError(RuntimeError):
    pass


class Stage2InvariantError(Stage2WorkerError):
    pass


class Stage2AuthorizationRevoked(Stage2WorkerError):
    pass


class Stage2LeaseLost(Stage2WorkerError):
    pass


class Stage2Conflict(Stage2WorkerError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedStage2Command:
    operation_id: str
    command_type: str
    queue: str
    payload: dict[str, Any]
    target_id: str | None
    authorization_snapshot: dict[str, Any]
    fact_snapshot: dict[str, Any]
    fact_snapshot_sha256: str
    source_state_version: int
    interaction_id: str | None = None
    handle_id: str | None = None


class Stage2CommandProcessor:
    """Prepare outside a write transaction and atomically publish one effect."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        id_generator: Callable[[datetime | None], str] | None = None,
        max_transaction_attempts: int = 3,
    ) -> None:
        if max_transaction_attempts < 1 or max_transaction_attempts > 5:
            raise ValueError("transaction retry bound must be between one and five")
        self.uow_factory = uow_factory
        self.id_generator = id_generator or UUID7Generator()
        self.max_transaction_attempts = max_transaction_attempts

    def prepare(
        self,
        scope: UowScope,
        claim: LeaseClaim,
    ) -> PreparedStage2Command:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                prepared = self._prepare_with_cursor(
                    cursor,
                    scope=scope,
                    claim=claim,
                    lock=False,
                )
            finally:
                cursor.close()
            uow.commit()
        return prepared

    def commit(
        self,
        scope: UowScope,
        claim: LeaseClaim,
        prepared: PreparedStage2Command,
        *,
        model_response: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        effect_ids = {
            name: str(self.id_generator())
            for name in (
                "primary",
                "secondary",
                "revision",
                "handle",
                "event",
                "audit",
                "product_operation",
                "delivery_intent",
            )
        }
        last_error: BaseException | None = None
        for attempt in range(1, self.max_transaction_attempts + 1):
            try:
                with self.uow_factory.begin(scope) as uow:
                    cursor = uow.connection.cursor()
                    try:
                        current = self._prepare_with_cursor(
                            cursor,
                            scope=scope,
                            claim=claim,
                            lock=True,
                        )
                        if (
                            current.fact_snapshot_sha256
                            != prepared.fact_snapshot_sha256
                            or current.source_state_version
                            != prepared.source_state_version
                            or current.command_type != prepared.command_type
                            or current.interaction_id != prepared.interaction_id
                            or current.handle_id != prepared.handle_id
                        ):
                            raise Stage2Conflict(
                                "frozen FactSnapshot or target state drifted"
                            )
                        result = self._commit_effect(
                            cursor,
                            scope=scope,
                            claim=claim,
                            prepared=current,
                            model_response=model_response,
                            effect_ids=effect_ids,
                        )
                        self._finish_operation(
                            cursor,
                            scope=scope,
                            claim=claim,
                            prepared=current,
                            result=result,
                            event_id=effect_ids["event"],
                            audit_id=effect_ids["audit"],
                        )
                    finally:
                        cursor.close()
                    uow.commit()
                return result
            except (Stage2WorkerError, ValueError):
                raise
            except BaseException as exc:
                last_error = exc
                if getattr(exc, "sqlstate", None) not in {"40P01", "40001"}:
                    raise
                if attempt == self.max_transaction_attempts:
                    break
        raise Stage2Conflict("bounded Stage-2 transaction retry exhausted") from last_error

    def _prepare_with_cursor(
        self,
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        lock: bool,
    ) -> PreparedStage2Command:
        lock_clause = " FOR UPDATE" if lock else ""
        cursor.execute(
            "SELECT operation_type, queue_name, operation_json, "
            "authorization_snapshot_json, target_resource_id, policy_sha256 "
            "FROM public.sleep_domain_operations "
            "WHERE operation_id = %s AND namespace_id = %s AND data_mode = %s "
            "AND namespace_generation = %s AND subject_id = %s "
            "AND COALESCE(run_id, '') = COALESCE(%s, '') "
            "AND COALESCE(arm_id, '') = COALESCE(%s, '') "
            "AND status = 'running' AND cas_version = %s "
            "AND lease_generation = %s AND fencing_token = %s "
            "AND worker_instance = %s "
            "AND lease_expires_at > clock_timestamp()" + lock_clause,
            (
                claim.work_id,
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.subject_id,
                scope.run_id,
                scope.arm_id,
                claim.operation_version,
                claim.lease_generation,
                claim.fencing_token,
                claim.worker_instance,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise Stage2LeaseLost("Stage-2 operation fence was rejected")
        command_type = str(row[0])
        queue = str(row[1])
        expected_queue = _queue_for_command(command_type)
        if queue != expected_queue or queue != claim.queue:
            raise Stage2InvariantError("command is routed to the wrong handler")
        required_scope = COMMAND_SCOPES.get(command_type)
        if required_scope is None:
            raise Stage2InvariantError("unknown Stage-2 command type")
        cursor.execute(
            "SELECT public.sleepagent_stage2_authority_allows(%s, %s, %s)",
            (claim.work_id, queue, required_scope),
        )
        authority = cursor.fetchone()
        if authority is None or authority[0] is not True:
            raise Stage2AuthorizationRevoked(
                "queued command authority is no longer current"
            )
        operation_json = _json_object(row[2], "operation_json")
        authorization = _json_object(row[3], "authorization_snapshot_json")
        payload = _json_object(operation_json.get("payload"), "command payload")
        if command_type in {
            "sleep_api.feedback.elder.v1",
            "sleep_api.feedback.family.v1",
            "interaction.feedback",
        }:
            event_at = _required_text(payload.get("event_at"), "feedback event_at")
            cursor.execute(
                "SELECT %s::timestamptz <= clock_timestamp()",
                (event_at,),
            )
            event_time = cursor.fetchone()
            if event_time is None or event_time[0] is not True:
                raise Stage2InvariantError(
                    "feedback event time cannot be in the future"
                )
        target_id = None if row[4] is None else str(row[4])
        interaction_id: str | None = None
        handle_id: str | None = None
        if command_type in {
            "interaction.ask",
            "interaction.answer",
            "interaction.confirm",
            "interaction.decline",
        }:
            interaction_id = _required_text(target_id, "interaction target")
        elif command_type == "interaction.feedback":
            candidate = payload.get("interaction_id")
            interaction_id = None if candidate is None else str(candidate)
        if command_type == "interaction.answer":
            handle_id = _required_text(payload.get("answer_handle"), "answer handle")
        elif command_type in {"interaction.confirm", "interaction.decline"}:
            handle_id = _required_text(
                payload.get("confirmation_handle"),
                "confirmation handle",
            )
        fact_snapshot, state_version = self._fact_snapshot(
            cursor,
            scope=scope,
            command_type=command_type,
            payload=payload,
            target_id=target_id,
            interaction_id=interaction_id,
            handle_id=handle_id,
            authorization=authorization,
            policy_sha256=str(row[5]),
            lock=lock,
        )
        return PreparedStage2Command(
            operation_id=claim.work_id,
            command_type=command_type,
            queue=queue,
            payload=payload,
            target_id=target_id,
            authorization_snapshot=authorization,
            fact_snapshot=fact_snapshot,
            fact_snapshot_sha256=_sha256(fact_snapshot),
            source_state_version=state_version,
            interaction_id=interaction_id,
            handle_id=handle_id,
        )

    def _fact_snapshot(
        self,
        cursor: Any,
        *,
        scope: UowScope,
        command_type: str,
        payload: Mapping[str, Any],
        target_id: str | None,
        interaction_id: str | None,
        handle_id: str | None,
        authorization: Mapping[str, Any],
        policy_sha256: str,
        lock: bool,
    ) -> tuple[dict[str, Any], int]:
        if command_type.startswith("sleep_api.monitoring."):
            cursor.execute(
                "SELECT state, cas_version, active_night_episode_id "
                "FROM public.backend_monitoring_snapshots_v2 "
                "WHERE namespace_id = %s AND data_mode = %s "
                "AND namespace_generation = %s AND subject_id = %s "
                "AND COALESCE(run_id, '') = COALESCE(%s, '') "
                "AND COALESCE(arm_id, '') = COALESCE(%s, '')"
                + (" FOR UPDATE" if lock else ""),
                _scope_params(scope),
            )
            row = cursor.fetchone()
            state = "dormant" if row is None else str(row[0])
            version = 0 if row is None else int(row[1])
            return (
                self._snapshot_envelope(
                    scope,
                    authorization,
                    policy_sha256,
                    {
                        "source_kind": "monitoring_snapshot",
                        "state": state,
                        "active_night_episode_id": (
                            None if row is None or row[2] is None else str(row[2])
                        ),
                        "state_version": version,
                    },
                ),
                version,
            )
        if interaction_id is not None:
            interaction = self._interaction_source(
                cursor,
                scope=scope,
                interaction_id=interaction_id,
                lock=lock,
            )
            if str(interaction[2]) != str(authorization.get("actor_id")):
                raise Stage2AuthorizationRevoked(
                    "interaction belongs to another actor"
                )
            fact_snapshot = _json_object(interaction[8], "FactSnapshot")
            if _sha256(fact_snapshot) != str(interaction[7]):
                raise Stage2InvariantError("persisted FactSnapshot integrity failed")
            if handle_id is not None:
                self._validate_handle_source(
                    cursor,
                    scope=scope,
                    interaction=interaction,
                    handle_id=handle_id,
                    expected_kind=(
                        "answer" if command_type == "interaction.answer"
                        else "confirmation"
                    ),
                    authorization=authorization,
                    policy_sha256=policy_sha256,
                    lock=lock,
                )
            return fact_snapshot, int(interaction[6])
        episode_revision_id: str | None = None
        episode_id: str | None = None
        if command_type == "interaction.start":
            candidate = payload.get("episode_revision_id")
            episode_revision_id = None if candidate is None else str(candidate)
        elif command_type == "interaction.feedback":
            candidate = payload.get("episode_revision_id")
            episode_revision_id = None if candidate is None else str(candidate)
        else:
            episode_id = target_id
        source = self._episode_source(
            cursor,
            scope=scope,
            episode_id=episode_id,
            episode_revision_id=episode_revision_id,
            lock=lock,
        )
        snapshot = self._snapshot_envelope(
            scope,
            authorization,
            policy_sha256,
            {
                "source_kind": "night_episode_revision",
                "night_episode_id": str(source[0]),
                "night_episode_revision_id": str(source[1]),
                "revision_number": int(source[2]),
                "revision": _json_object(source[3], "revision_json"),
                "quality_assessment_id": str(source[4]),
                "quality": _json_object(source[5], "assessment_json"),
                "current_risk_id": str(source[6]),
                "risk": _json_object(source[7], "risk_json"),
            },
        )
        return snapshot, int(source[2])

    @staticmethod
    def _snapshot_envelope(
        scope: UowScope,
        authorization: Mapping[str, Any],
        policy_sha256: str,
        source: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema_version": "product_fact_snapshot.pg.v2",
            "namespace_id": scope.namespace_id,
            "namespace_generation": scope.namespace_generation,
            "data_mode": scope.data_mode,
            "run_id": scope.run_id,
            "arm_id": scope.arm_id,
            "subject_id": scope.subject_id,
            "source": dict(source),
            "source_scope": {
                "binding_id": authorization.get("binding_id"),
                "role": authorization.get("role"),
                "effective_scopes": authorization.get("effective_scopes", []),
            },
            "authorization_epoch": scope.authorization_epoch,
            "privacy_epoch": scope.privacy_epoch,
            "retrieval_policy_epoch": scope.retrieval_policy_epoch,
            "policy_sha256": policy_sha256,
            "registry_sha256": _sha256(
                {"registry": "stage2-command-registry.v1"}
            ),
            "model_sha256": _sha256(
                {"model": "deterministic-stage2-interaction.v1"}
            ),
            "schema_manifest_sha256": MIGRATION_MANIFEST_SHA256,
        }

    @staticmethod
    def _interaction_source(
        cursor: Any,
        *,
        scope: UowScope,
        interaction_id: str,
        lock: bool,
    ) -> Any:
        cursor.execute(
            "SELECT interaction.interaction_id, interaction.night_episode_id, "
            "interaction.actor_id, interaction.binding_id, interaction.role, "
            "interaction.state, interaction.current_revision, "
            "interaction.fact_snapshot_sha256, revision.fact_snapshot_json, "
            "interaction.current_state_sha256 "
            "FROM public.backend_product_interactions AS interaction "
            "JOIN public.backend_product_interaction_revisions AS revision "
            "ON revision.interaction_id = interaction.interaction_id "
            "AND revision.namespace_id = interaction.namespace_id "
            "AND revision.data_mode = interaction.data_mode "
            "AND revision.subject_id = interaction.subject_id "
            "AND revision.revision_number = interaction.current_revision "
            "WHERE interaction.interaction_id = %s "
            "AND interaction.namespace_id = %s AND interaction.data_mode = %s "
            "AND interaction.namespace_generation = %s "
            "AND interaction.subject_id = %s "
            "AND COALESCE(interaction.run_id, '') = COALESCE(%s, '') "
            "AND COALESCE(interaction.arm_id, '') = COALESCE(%s, '')"
            + (" FOR UPDATE OF interaction" if lock else ""),
            (interaction_id, *_scope_params(scope)),
        )
        row = cursor.fetchone()
        if row is None:
            raise Stage2Conflict("interaction is missing or outside this generation")
        return row

    @staticmethod
    def _validate_handle_source(
        cursor: Any,
        *,
        scope: UowScope,
        interaction: Any,
        handle_id: str,
        expected_kind: str,
        authorization: Mapping[str, Any],
        policy_sha256: str,
        lock: bool,
    ) -> None:
        cursor.execute(
            "SELECT handle_kind, target_resource_id, target_state_version, "
            "target_sha256, fact_snapshot_sha256, authorization_epoch, "
            "privacy_epoch, retrieval_policy_epoch, policy_sha256, status, "
            "cas_version, expires_at, actor_id, role "
            "FROM public.backend_pending_handles WHERE handle_id = %s "
            "AND namespace_id = %s AND data_mode = %s "
            "AND namespace_generation = %s AND subject_id = %s "
            "AND COALESCE(run_id, '') = COALESCE(%s, '') "
            "AND COALESCE(arm_id, '') = COALESCE(%s, '') "
            "AND expires_at > clock_timestamp()"
            + (" FOR UPDATE" if lock else ""),
            (handle_id, *_scope_params(scope)),
        )
        row = cursor.fetchone()
        expected = (
            expected_kind,
            str(interaction[0]),
            int(interaction[6]),
            str(interaction[9]),
            str(interaction[7]),
            scope.authorization_epoch,
            scope.privacy_epoch,
            scope.retrieval_policy_epoch,
            policy_sha256,
            "pending",
            str(authorization.get("actor_id")),
            str(authorization.get("role")),
        )
        if row is None or (
            str(row[0]),
            str(row[1]),
            int(row[2]),
            str(row[3]),
            str(row[4]),
            int(row[5]),
            int(row[6]),
            int(row[7]),
            str(row[8]),
            str(row[9]),
            str(row[12]),
            str(row[13]),
        ) != expected:
            raise Stage2Conflict("opaque handle is stale or outside its authority")

    @staticmethod
    def _episode_source(
        cursor: Any,
        *,
        scope: UowScope,
        episode_id: str | None,
        episode_revision_id: str | None,
        lock: bool,
    ) -> Any:
        cursor.execute(
            "SELECT episode.night_episode_id, episode.current_revision_id, "
            "revision.revision_number, revision.revision_json, "
            "quality.assessment_id, quality.assessment_json, "
            "risk.current_risk_id, risk.risk_json "
            "FROM public.sleep_domain_night_episodes AS episode "
            "JOIN public.sleep_domain_night_episode_revisions AS revision "
            "ON revision.night_episode_revision_id = episode.current_revision_id "
            "AND revision.namespace_id = episode.namespace_id "
            "AND revision.data_mode = episode.data_mode "
            "JOIN public.sleep_domain_current_quality AS quality "
            "ON quality.namespace_id = episode.namespace_id "
            "AND quality.data_mode = episode.data_mode "
            "AND quality.subject_id = episode.subject_id "
            "AND quality.night_episode_id = episode.night_episode_id "
            "AND quality.assessment_json #>> "
            "'{source_scope,night_episode_revision_id}' = episode.current_revision_id "
            "JOIN public.sleep_domain_current_risk AS risk "
            "ON risk.namespace_id = episode.namespace_id "
            "AND risk.data_mode = episode.data_mode "
            "AND risk.subject_id = episode.subject_id "
            "AND risk.night_episode_id = episode.night_episode_id "
            "AND risk.risk_json #>> "
            "'{source_scope,night_episode_revision_id}' = episode.current_revision_id "
            "WHERE episode.namespace_id = %s AND episode.data_mode = %s "
            "AND episode.namespace_generation = %s AND episode.subject_id = %s "
            "AND COALESCE(episode.run_id, '') = COALESCE(%s, '') "
            "AND COALESCE(episode.arm_id, '') = COALESCE(%s, '') "
            "AND (%s::text IS NULL OR episode.night_episode_id = %s) "
            "AND (%s::text IS NULL OR episode.current_revision_id = %s) "
            "AND episode.protocol_version >= 2 "
            "AND episode.date_state = 'finalized' "
            "AND episode.date_conflict = FALSE "
            "ORDER BY episode.episode_local_date DESC, episode.updated_at DESC "
            "LIMIT 1" + (" FOR UPDATE OF episode" if lock else ""),
            (
                *_scope_params(scope),
                episode_id,
                episode_id,
                episode_revision_id,
                episode_revision_id,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise Stage2Conflict(
                "no exact committed Episode/quality/risk source is available"
            )
        return row

    def _commit_effect(
        self,
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        prepared: PreparedStage2Command,
        model_response: Mapping[str, Any] | None,
        effect_ids: Mapping[str, str],
    ) -> dict[str, Any]:
        command = prepared.command_type
        if command in {
            "sleep_api.monitoring.activate.v1",
            "sleep_api.monitoring.deactivate.v1",
        }:
            return self._commit_monitoring(
                cursor,
                scope=scope,
                claim=claim,
                prepared=prepared,
                receipt_id=effect_ids["primary"],
                snapshot_id=effect_ids["secondary"],
            )
        if command in {
            "sleep_api.feedback.elder.v1",
            "sleep_api.feedback.family.v1",
            "sleep_api.reanalysis.v1",
        }:
            return self._commit_sleep_fact_or_reanalysis(
                cursor,
                scope=scope,
                claim=claim,
                prepared=prepared,
                fact_id=effect_ids["primary"],
                link_id=effect_ids["secondary"],
                product_operation_id=effect_ids["product_operation"],
            )
        return self._commit_interaction(
            cursor,
            scope=scope,
            claim=claim,
            prepared=prepared,
            model_response=model_response,
            effect_ids=effect_ids,
        )

    def _commit_monitoring(
        self,
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        prepared: PreparedStage2Command,
        receipt_id: str,
        snapshot_id: str,
    ) -> dict[str, Any]:
        activate = (
            prepared.command_type == "sleep_api.monitoring.activate.v1"
        )
        current = prepared.fact_snapshot["source"]
        from_state = str(current["state"])
        expected_from = "dormant" if activate else "active"
        to_state = "active" if activate else "dormant"
        if from_state != expected_from:
            raise Stage2Conflict("monitoring transition is not valid from current state")
        from_version = int(current["state_version"])
        episode_id: str | None = None
        anchor: str | None = None
        if activate:
            source = self._episode_source(
                cursor,
                scope=scope,
                episode_id=None,
                episode_revision_id=None,
                lock=True,
            )
            episode_id = str(source[0])
            anchor = f"activation:{claim.work_id}"
        if from_version == 0:
            cursor.execute(
                "INSERT INTO public.backend_monitoring_snapshots_v2 ("
                "monitoring_snapshot_id, namespace_id, data_mode, "
                "namespace_generation, run_id, arm_id, subject_id, state, "
                "active_night_episode_id, active_episode_anchor_key, "
                "cas_version, snapshot_json) VALUES ("
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s::jsonb)",
                (
                    snapshot_id,
                    scope.namespace_id,
                    scope.data_mode,
                    scope.namespace_generation,
                    scope.run_id,
                    scope.arm_id,
                    scope.subject_id,
                    to_state,
                    episode_id,
                    anchor,
                    _json(
                        {
                            "schema_version": "monitoring_snapshot.v2",
                            "state": to_state,
                            "operation_id": claim.work_id,
                        }
                    ),
                ),
            )
            monitoring_id = snapshot_id
        else:
            cursor.execute(
                "UPDATE public.backend_monitoring_snapshots_v2 SET "
                "state = %s, active_night_episode_id = %s, "
                "active_episode_anchor_key = %s, cas_version = cas_version + 1, "
                "snapshot_json = %s::jsonb, updated_at = clock_timestamp() "
                "WHERE namespace_id = %s AND data_mode = %s "
                "AND namespace_generation = %s AND subject_id = %s "
                "AND COALESCE(run_id, '') = COALESCE(%s, '') "
                "AND COALESCE(arm_id, '') = COALESCE(%s, '') "
                "AND state = %s AND cas_version = %s "
                "RETURNING monitoring_snapshot_id",
                (
                    to_state,
                    episode_id,
                    anchor,
                    _json(
                        {
                            "schema_version": "monitoring_snapshot.v2",
                            "state": to_state,
                            "operation_id": claim.work_id,
                        }
                    ),
                    *_scope_params(scope),
                    from_state,
                    from_version,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise Stage2Conflict("monitoring snapshot CAS lost")
            monitoring_id = str(row[0])
        receipt = {
            "schema_version": "monitoring_transition_receipt.v1",
            "operation_id": claim.work_id,
            "monitoring_snapshot_id": monitoring_id,
            "from_state": from_state,
            "to_state": to_state,
            "from_cas_version": from_version,
            "to_cas_version": from_version + 1,
        }
        cursor.execute(
            "INSERT INTO public.backend_monitoring_transition_receipts ("
            "transition_receipt_id, namespace_id, data_mode, "
            "namespace_generation, run_id, arm_id, subject_id, operation_id, "
            "monitoring_snapshot_id, from_state, to_state, from_cas_version, "
            "to_cas_version, receipt_json) VALUES ("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            (
                receipt_id,
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                scope.subject_id,
                claim.work_id,
                monitoring_id,
                from_state,
                to_state,
                from_version,
                from_version + 1,
                _json(receipt),
            ),
        )
        return {
            "schema_version": "sleep_command_result.v1",
            "result_resource_id": monitoring_id,
            "monitoring_state": to_state,
            "transition_receipt_id": receipt_id,
        }

    def _commit_sleep_fact_or_reanalysis(
        self,
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        prepared: PreparedStage2Command,
        fact_id: str,
        link_id: str,
        product_operation_id: str,
    ) -> dict[str, Any]:
        source = prepared.fact_snapshot["source"]
        episode_id = _required_text(source.get("night_episode_id"), "episode")
        revision_id = _required_text(
            source.get("night_episode_revision_id"), "episode revision"
        )
        fact_id_value: str | None = None
        trigger = "explicit_reanalysis"
        if prepared.command_type.startswith("sleep_api.feedback."):
            canonical_source = (
                "elder_self_report"
                if prepared.command_type == "sleep_api.feedback.elder.v1"
                else "family_observation"
            )
            fact = {
                "schema_version": "human_sleep_fact.v1",
                "canonical_source": canonical_source,
                "night_episode_id": episode_id,
                "night_episode_revision_id": revision_id,
                "event_at": prepared.payload.get("event_at"),
                "source_text": prepared.payload.get("source_text"),
                "structured_answer": prepared.payload.get("structured_answer"),
            }
            cursor.execute(
                "INSERT INTO public.backend_human_facts ("
                "human_fact_id, namespace_id, data_mode, namespace_generation, "
                "run_id, arm_id, subject_id, night_episode_id, "
                "night_episode_revision_id, operation_id, actor_id, binding_id, "
                "canonical_source, event_at, fact_sha256, fact_json) VALUES ("
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s::timestamptz, %s, %s::jsonb)",
                (
                    fact_id,
                    scope.namespace_id,
                    scope.data_mode,
                    scope.namespace_generation,
                    scope.run_id,
                    scope.arm_id,
                    scope.subject_id,
                    episode_id,
                    revision_id,
                    claim.work_id,
                    prepared.authorization_snapshot["actor_id"],
                    prepared.authorization_snapshot["binding_id"],
                    canonical_source,
                    prepared.payload.get("event_at"),
                    _sha256(fact),
                    _json(fact),
                ),
            )
            fact_id_value = fact_id
            trigger = "human_feedback"
        self._insert_product_child(
            cursor,
            scope=scope,
            claim=claim,
            prepared=prepared,
            product_operation_id=product_operation_id,
            link_id=link_id,
            trigger=trigger,
        )
        return {
            "schema_version": "sleep_command_result.v1",
            "result_resource_id": fact_id_value or revision_id,
            "human_fact_id": fact_id_value,
            "product_operation_id": product_operation_id,
            "night_episode_revision_id": revision_id,
        }

    def _insert_product_child(
        self,
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        prepared: PreparedStage2Command,
        product_operation_id: str,
        link_id: str,
        trigger: str,
    ) -> None:
        source = prepared.fact_snapshot["source"]
        episode_id = _required_text(source.get("night_episode_id"), "episode")
        revision_id = _required_text(
            source.get("night_episode_revision_id"), "episode revision"
        )
        quality_id = _required_text(
            source.get("quality_assessment_id"), "quality assessment"
        )
        risk_id = _required_text(source.get("current_risk_id"), "current risk")
        policy_sha256 = _required_text(
            prepared.fact_snapshot.get("policy_sha256"), "policy SHA-256"
        )
        semantic_key = _sha256(
            {
                "stage": "product_agent",
                "source_operation_id": claim.work_id,
                "night_episode_revision_id": revision_id,
                "trigger": trigger,
            }
        )
        workload = {
            "schema_version": "workload_authorization_snapshot.v1",
            "workload_principal_id": scope.service_principal_id,
            "namespace_id": scope.namespace_id,
            "namespace_generation": scope.namespace_generation,
            "data_mode": scope.data_mode,
            "run_id": scope.run_id,
            "arm_id": scope.arm_id,
            "subject_id": scope.subject_id,
            "purpose": scope.purpose,
            "allowed_handler": "product_agent",
            "authorization_epoch": scope.authorization_epoch,
            "privacy_epoch": scope.privacy_epoch,
            "retrieval_policy_epoch": scope.retrieval_policy_epoch,
        }
        operation_json = {
            "schema_version": "backend_operation.v2",
            "trigger": trigger,
            "source_operation_id": claim.work_id,
            "night_episode_id": episode_id,
            "night_episode_revision_id": revision_id,
            "quality_assessment_id": quality_id,
            "current_risk_id": risk_id,
            "human_fact_sha256": (
                None
                if trigger == "explicit_reanalysis"
                else _sha256(prepared.payload)
            ),
            "authorization_snapshot": workload,
        }
        cursor.execute(
            "INSERT INTO public.sleep_domain_operations ("
            "operation_id, namespace_id, data_mode, operation_type, subject_id, "
            "service_principal_id, actor_id, target_resource_id, "
            "target_resource_key, idempotency_key, request_sha256, status, "
            "attempt_count, cas_version, operation_json, created_at, updated_at, "
            "protocol_version, namespace_generation, run_id, arm_id, id_scheme, "
            "origin_kind, semantic_key, queue_name, priority, available_at, "
            "max_attempts, workload_authorization_snapshot_json, policy_sha256) "
            "VALUES (%s, %s, %s, 'product_agent', %s, %s, NULL, %s, %s, %s, "
            "%s, 'pending', 0, 0, %s::jsonb, clock_timestamp(), "
            "clock_timestamp(), 2, %s, %s, %s, 'uuidv7', 'system', %s, "
            "'product_agent', 0, clock_timestamp(), 5, %s::jsonb, %s)",
            (
                product_operation_id,
                scope.namespace_id,
                scope.data_mode,
                scope.subject_id,
                scope.service_principal_id,
                episode_id,
                revision_id,
                semantic_key,
                semantic_key,
                _json(operation_json),
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                semantic_key,
                _json(workload),
                policy_sha256,
            ),
        )
        cursor.execute(
            "INSERT INTO public.backend_reanalysis_links ("
            "reanalysis_link_id, namespace_id, data_mode, namespace_generation, "
            "run_id, arm_id, subject_id, source_operation_id, night_episode_id, "
            "night_episode_revision_id, product_operation_id, trigger_kind) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                link_id,
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                scope.subject_id,
                claim.work_id,
                episode_id,
                revision_id,
                product_operation_id,
                trigger,
            ),
        )

    def _commit_interaction(
        self,
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        prepared: PreparedStage2Command,
        model_response: Mapping[str, Any] | None,
        effect_ids: Mapping[str, str],
    ) -> dict[str, Any]:
        command = prepared.command_type
        if command in MODEL_COMMANDS and model_response is None:
            raise Stage2InvariantError("model command has no journaled response")
        if command == "interaction.start":
            return self._start_interaction(
                cursor,
                scope=scope,
                claim=claim,
                prepared=prepared,
                model_response=model_response or {},
                interaction_id=effect_ids["primary"],
                revision_id=effect_ids["revision"],
                handle_id=effect_ids["handle"],
            )
        if command == "interaction.feedback":
            return self._interaction_feedback(
                cursor,
                scope=scope,
                claim=claim,
                prepared=prepared,
                fact_id=effect_ids["primary"],
                revision_id=effect_ids["revision"],
                product_operation_id=effect_ids["product_operation"],
                link_id=effect_ids["secondary"],
            )
        interaction = self._interaction_source(
            cursor,
            scope=scope,
            interaction_id=_required_text(
                prepared.interaction_id, "interaction target"
            ),
            lock=True,
        )
        if command == "interaction.ask":
            return self._advance_interaction(
                cursor,
                scope=scope,
                claim=claim,
                prepared=prepared,
                interaction=interaction,
                state="waiting_user",
                model_response=model_response or {},
                revision_id=effect_ids["revision"],
                handle_id=effect_ids["handle"],
                handle_kind="answer",
            )
        if command == "interaction.answer":
            self._consume_handle(
                cursor,
                scope=scope,
                claim=claim,
                prepared=prepared,
                interaction=interaction,
            )
            return self._advance_interaction(
                cursor,
                scope=scope,
                claim=claim,
                prepared=prepared,
                interaction=interaction,
                state="awaiting_confirmation",
                model_response=model_response or {},
                revision_id=effect_ids["revision"],
                handle_id=effect_ids["handle"],
                handle_kind="confirmation",
            )
        if command in {"interaction.confirm", "interaction.decline"}:
            self._consume_handle(
                cursor,
                scope=scope,
                claim=claim,
                prepared=prepared,
                interaction=interaction,
            )
            return self._commit_decision(
                cursor,
                scope=scope,
                claim=claim,
                prepared=prepared,
                interaction=interaction,
                confirmed=command == "interaction.confirm",
                decision_id=effect_ids["primary"],
                care_action_id=effect_ids["secondary"],
                revision_id=effect_ids["revision"],
                delivery_intent_id=effect_ids["delivery_intent"],
                source_event_id=effect_ids["event"],
            )
        raise Stage2InvariantError("unsupported Product interaction command")

    def _start_interaction(
        self,
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        prepared: PreparedStage2Command,
        model_response: Mapping[str, Any],
        interaction_id: str,
        revision_id: str,
        handle_id: str,
    ) -> dict[str, Any]:
        source = prepared.fact_snapshot["source"]
        state = "waiting_user"
        revision = {
            "schema_version": "product_interaction_revision.v1",
            "command_type": prepared.command_type,
            "state": state,
            "intent": _required_text(prepared.payload.get("intent"), "intent"),
            "model_response": dict(model_response),
        }
        state_sha256 = _sha256(revision)
        common = (
            scope.namespace_id,
            scope.data_mode,
            scope.namespace_generation,
            scope.run_id,
            scope.arm_id,
            scope.subject_id,
        )
        cursor.execute(
            "INSERT INTO public.backend_product_interactions ("
            "interaction_id, namespace_id, data_mode, namespace_generation, "
            "run_id, arm_id, subject_id, actor_id, binding_id, role, "
            "night_episode_id, night_episode_revision_id, state, "
            "current_revision, cas_version, fact_snapshot_sha256, "
            "current_state_sha256, interaction_json) VALUES ("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "1, 1, %s, %s, %s::jsonb)",
            (
                interaction_id,
                *common,
                prepared.authorization_snapshot["actor_id"],
                prepared.authorization_snapshot["binding_id"],
                prepared.authorization_snapshot["role"],
                source["night_episode_id"],
                source["night_episode_revision_id"],
                state,
                prepared.fact_snapshot_sha256,
                state_sha256,
                _json(
                    {
                        "schema_version": "product_interaction.v1",
                        "interaction_id": interaction_id,
                        "state": state,
                    }
                ),
            ),
        )
        self._insert_interaction_revision(
            cursor,
            scope=scope,
            claim=claim,
            interaction_id=interaction_id,
            revision_id=revision_id,
            revision_number=1,
            state=state,
            fact_snapshot=prepared.fact_snapshot,
            revision=revision,
        )
        self._insert_handle(
            cursor,
            scope=scope,
            prepared=prepared,
            interaction_id=interaction_id,
            handle_id=handle_id,
            handle_kind="answer",
            state_version=1,
            state_sha256=state_sha256,
            prompt=str(model_response.get("prompt", "请补充昨晚的睡眠感受。")),
        )
        return {
            "schema_version": "product_interaction_result.v1",
            "result_ref": interaction_id,
            "interaction_id": interaction_id,
            "interaction_revision": 1,
            "interaction_state": state,
            "public_state": "waiting_for_input",
            "answer_handle": handle_id,
        }

    def _advance_interaction(
        self,
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        prepared: PreparedStage2Command,
        interaction: Any,
        state: str,
        model_response: Mapping[str, Any],
        revision_id: str,
        handle_id: str,
        handle_kind: str,
    ) -> dict[str, Any]:
        if str(interaction[5]) in {"confirmed", "declined", "completed", "failed"}:
            raise Stage2Conflict("terminal interaction cannot be advanced")
        next_revision = int(interaction[6]) + 1
        revision = {
            "schema_version": "product_interaction_revision.v1",
            "command_type": prepared.command_type,
            "state": state,
            "payload": prepared.payload,
            "model_response": dict(model_response),
            "parent_revision": int(interaction[6]),
        }
        state_sha256 = _sha256(revision)
        cursor.execute(
            "UPDATE public.backend_pending_handles SET status = 'revoked', "
            "cas_version = cas_version + 1 "
            "WHERE namespace_id = %s AND data_mode = %s "
            "AND namespace_generation = %s AND subject_id = %s "
            "AND COALESCE(run_id, '') = COALESCE(%s, '') "
            "AND COALESCE(arm_id, '') = COALESCE(%s, '') "
            "AND target_resource_id = %s AND status = 'pending'",
            (*_scope_params(scope), str(interaction[0])),
        )
        cursor.execute(
            "UPDATE public.backend_product_interactions SET state = %s, "
            "current_revision = %s, cas_version = cas_version + 1, "
            "current_state_sha256 = %s, interaction_json = %s::jsonb, "
            "updated_at = clock_timestamp() WHERE interaction_id = %s "
            "AND namespace_id = %s AND data_mode = %s AND subject_id = %s "
            "AND current_revision = %s AND cas_version = %s RETURNING cas_version",
            (
                state,
                next_revision,
                state_sha256,
                _json(
                    {
                        "schema_version": "product_interaction.v1",
                        "interaction_id": str(interaction[0]),
                        "state": state,
                    }
                ),
                str(interaction[0]),
                scope.namespace_id,
                scope.data_mode,
                scope.subject_id,
                int(interaction[6]),
                int(interaction[6]),
            ),
        )
        if cursor.fetchone() is None:
            raise Stage2Conflict("interaction CAS lost")
        self._insert_interaction_revision(
            cursor,
            scope=scope,
            claim=claim,
            interaction_id=str(interaction[0]),
            revision_id=revision_id,
            revision_number=next_revision,
            state=state,
            fact_snapshot=prepared.fact_snapshot,
            revision=revision,
        )
        self._insert_handle(
            cursor,
            scope=scope,
            prepared=prepared,
            interaction_id=str(interaction[0]),
            handle_id=handle_id,
            handle_kind=handle_kind,
            state_version=next_revision,
            state_sha256=state_sha256,
            prompt=str(
                model_response.get(
                    "prompt",
                    (
                        "请确认是否创建这项照护行动。"
                        if handle_kind == "confirmation"
                        else "请补充所需信息。"
                    ),
                )
            ),
        )
        result = {
            "schema_version": "product_interaction_result.v1",
            "result_ref": str(interaction[0]),
            "interaction_id": str(interaction[0]),
            "interaction_revision": next_revision,
            "interaction_state": state,
            "public_state": "waiting_for_input",
        }
        result[f"{handle_kind}_handle"] = handle_id
        return result

    @staticmethod
    def _consume_handle(
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        prepared: PreparedStage2Command,
        interaction: Any,
    ) -> None:
        del scope
        cursor.execute(
            "UPDATE public.backend_pending_handles SET status = 'consumed', "
            "consumed_at = clock_timestamp(), "
            "consumed_by_command_receipt_id = ("
            "SELECT command_receipt_id FROM public.backend_command_receipts "
            "WHERE operation_id = %s), cas_version = cas_version + 1 "
            "WHERE handle_id = %s AND status = 'pending' "
            "AND target_resource_id = %s AND target_state_version = %s "
            "AND target_sha256 = %s AND fact_snapshot_sha256 = %s "
            "AND expires_at > clock_timestamp() RETURNING handle_id",
            (
                claim.work_id,
                prepared.handle_id,
                str(interaction[0]),
                int(interaction[6]),
                str(interaction[9]),
                prepared.fact_snapshot_sha256,
            ),
        )
        if cursor.fetchone() is None:
            raise Stage2Conflict("single-use handle consumption lost its CAS")

    def _commit_decision(
        self,
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        prepared: PreparedStage2Command,
        interaction: Any,
        confirmed: bool,
        decision_id: str,
        care_action_id: str,
        revision_id: str,
        delivery_intent_id: str,
        source_event_id: str,
    ) -> dict[str, Any]:
        if str(interaction[5]) != "awaiting_confirmation":
            raise Stage2Conflict("interaction is not awaiting confirmation")
        state = "confirmed" if confirmed else "declined"
        next_revision = int(interaction[6]) + 1
        decision = {
            "schema_version": "human_decision.v2",
            "choice": "confirm" if confirmed else "decline",
            "interaction_id": str(interaction[0]),
            "target_state_version": int(interaction[6]),
            "target_sha256": str(interaction[9]),
            "reason_code": prepared.payload.get("reason_code"),
        }
        revision = {
            "schema_version": "product_interaction_revision.v1",
            "command_type": prepared.command_type,
            "state": state,
            "decision_sha256": _sha256(decision),
            "parent_revision": int(interaction[6]),
        }
        state_sha256 = _sha256(revision)
        cursor.execute(
            "UPDATE public.backend_product_interactions SET state = %s, "
            "current_revision = %s, cas_version = cas_version + 1, "
            "current_state_sha256 = %s, interaction_json = %s::jsonb, "
            "updated_at = clock_timestamp() WHERE interaction_id = %s "
            "AND current_revision = %s AND cas_version = %s RETURNING cas_version",
            (
                state,
                next_revision,
                state_sha256,
                _json(
                    {
                        "schema_version": "product_interaction.v1",
                        "interaction_id": str(interaction[0]),
                        "state": state,
                    }
                ),
                str(interaction[0]),
                int(interaction[6]),
                int(interaction[6]),
            ),
        )
        if cursor.fetchone() is None:
            raise Stage2Conflict("interaction decision CAS lost")
        self._insert_interaction_revision(
            cursor,
            scope=scope,
            claim=claim,
            interaction_id=str(interaction[0]),
            revision_id=revision_id,
            revision_number=next_revision,
            state=state,
            fact_snapshot=prepared.fact_snapshot,
            revision=revision,
        )
        cursor.execute(
            "INSERT INTO public.backend_human_decisions_v2 ("
            "human_decision_id, namespace_id, data_mode, namespace_generation, "
            "run_id, arm_id, subject_id, interaction_id, operation_id, actor_id, "
            "binding_id, choice, target_state_version, target_sha256, "
            "authorization_epoch, privacy_epoch, retrieval_policy_epoch, "
            "decision_json) VALUES ("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s::jsonb)",
            (
                decision_id,
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                scope.subject_id,
                str(interaction[0]),
                claim.work_id,
                prepared.authorization_snapshot["actor_id"],
                prepared.authorization_snapshot["binding_id"],
                decision["choice"],
                int(interaction[6]),
                str(interaction[9]),
                scope.authorization_epoch,
                scope.privacy_epoch,
                scope.retrieval_policy_epoch,
                _json(decision),
            ),
        )
        result = {
            "schema_version": "product_interaction_result.v1",
            "result_ref": str(interaction[0]),
            "interaction_id": str(interaction[0]),
            "interaction_revision": next_revision,
            "interaction_state": state,
            "public_state": "succeeded",
            "human_decision_id": decision_id,
            "care_action_id": None,
            "delivery_intent_id": None,
        }
        if confirmed:
            action = {
                "schema_version": "care_action.v2",
                "care_action_id": care_action_id,
                "interaction_id": str(interaction[0]),
                "action_kind": "sleep_hygiene_followup",
                "state": "confirmed_pending_delivery",
            }
            cursor.execute(
                "INSERT INTO public.backend_care_actions_v2 ("
                "care_action_id, namespace_id, data_mode, namespace_generation, "
                "run_id, arm_id, subject_id, interaction_id, human_decision_id, "
                "state, action_sha256, action_json, confirmed_at) VALUES ("
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "'confirmed_pending_delivery', %s, %s::jsonb, clock_timestamp())",
                (
                    care_action_id,
                    scope.namespace_id,
                    scope.data_mode,
                    scope.namespace_generation,
                    scope.run_id,
                    scope.arm_id,
                    scope.subject_id,
                    str(interaction[0]),
                    decision_id,
                    _sha256(action),
                    _json(action),
                ),
            )
            self._insert_care_followup(
                cursor,
                scope=scope,
                claim=claim,
                prepared=prepared,
                night_episode_id=_required_text(
                    interaction[1], "interaction episode"
                ),
                care_action_id=care_action_id,
                source_event_id=source_event_id,
            )
            result["care_action_id"] = care_action_id
            result["delivery_intent_id"] = delivery_intent_id
        return result

    @staticmethod
    def _insert_care_followup(
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        prepared: PreparedStage2Command,
        night_episode_id: str,
        care_action_id: str,
        source_event_id: str,
    ) -> None:
        cursor.execute(
            "SELECT state FROM public.sleep_domain_care_followups "
            "WHERE namespace_id = %s AND data_mode = %s "
            "AND namespace_generation = %s "
            "AND COALESCE(run_id, '') = COALESCE(%s, '') "
            "AND COALESCE(arm_id, '') = COALESCE(%s, '') "
            "AND night_episode_id = %s FOR UPDATE",
            (
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                night_episode_id,
            ),
        )
        if cursor.fetchone() is not None:
            raise Stage2Conflict(
                "confirmed care action requires a new CareFollowup"
            )
        cursor.execute("SELECT clock_timestamp()")
        occurred_row = cursor.fetchone()
        if occurred_row is None:
            raise Stage2InvariantError("database did not return ControlClock time")
        occurred_at = occurred_row[0]
        actor_id = _required_text(
            prepared.authorization_snapshot.get("actor_id"), "actor"
        )
        authorization_id = _required_text(
            prepared.authorization_snapshot.get("binding_id"), "binding"
        )
        reason_code = "confirmed_care_action"
        receipt_id = "care-followup-receipt:" + _sha256(
            {
                "operation_id": claim.work_id,
                "night_episode_id": night_episode_id,
                "care_action_id": care_action_id,
            }
        )
        care_event_id = "care-followup-event:" + _sha256(
            {
                "source_event_id": source_event_id,
                "receipt_id": receipt_id,
            }
        )
        snapshot = {
            "schema_version": "care_followup_snapshot.v1",
            "data_mode": scope.data_mode,
            "subject_id": scope.subject_id,
            "night_episode_id": night_episode_id,
            "state": "pending_feedback",
            "state_entered_at": occurred_at.isoformat(),
            "last_transition_id": receipt_id,
            "cas_version": 1,
            "updated_at": occurred_at.isoformat(),
        }
        receipt = {
            "schema_version": "care_followup_transition_receipt.v1",
            "receipt_id": receipt_id,
            "command_id": claim.work_id,
            "data_mode": scope.data_mode,
            "subject_id": scope.subject_id,
            "night_episode_id": night_episode_id,
            "from_state": "none",
            "to_state": "pending_feedback",
            "actor_id": actor_id,
            "authorization_id": authorization_id,
            "reason_code": reason_code,
            "occurred_at": occurred_at.isoformat(),
        }
        cursor.execute(
            "INSERT INTO public.sleep_domain_care_followups ("
            "namespace_id, data_mode, namespace_generation, run_id, arm_id, "
            "subject_id, night_episode_id, state, cas_version, snapshot_json, "
            "created_at, updated_at) VALUES ("
            "%s, %s, %s, %s, %s, %s, %s, 'pending_feedback', 1, %s::jsonb, "
            "%s, %s)",
            (
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                scope.subject_id,
                night_episode_id,
                _json(snapshot),
                occurred_at,
                occurred_at,
            ),
        )
        cursor.execute(
            "INSERT INTO public.sleep_domain_care_followup_transition_receipts ("
            "receipt_id, namespace_id, data_mode, namespace_generation, run_id, "
            "arm_id, subject_id, night_episode_id, command_id, from_state, "
            "to_state, actor_id, authorization_id, receipt_json, occurred_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'none', "
            "'pending_feedback', %s, %s, %s::jsonb, %s)",
            (
                receipt_id,
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                scope.subject_id,
                night_episode_id,
                claim.work_id,
                actor_id,
                authorization_id,
                _json(receipt),
                occurred_at,
            ),
        )
        cursor.execute(
            "SELECT nextval(pg_get_serial_sequence("
            "'public.sleep_domain_domain_outbox', 'delivery_offset'))"
        )
        delivery_row = cursor.fetchone()
        if delivery_row is None:
            raise Stage2InvariantError(
                "database did not return CareFollowup delivery offset"
            )
        delivery_offset = int(delivery_row[0])
        event = {
            "schema_version": "domain_event.v1",
            "event_id": care_event_id,
            "event_type": "CARE_FOLLOWUP_PENDING",
            "event_version": "1",
            "data_mode": scope.data_mode,
            "aggregate_type": "CareFollowup",
            "aggregate_id": night_episode_id,
            "aggregate_version": 1,
            "per_aggregate_sequence": 1,
            "delivery_offset": delivery_offset,
            "subject_id": scope.subject_id,
            "night_episode_id": night_episode_id,
            "night_episode_revision_id": None,
            "operation_id": claim.work_id,
            "event_occurred_at": occurred_at.isoformat(),
            "persisted_at": occurred_at.isoformat(),
            "correlation_id": claim.work_id,
            "causation_id": claim.work_id,
            "attributes": {
                "from_state": "none",
                "to_state": "pending_feedback",
                "actor_id": actor_id,
                "authorization_id": authorization_id,
                "reason_code": reason_code,
            },
        }
        cursor.execute(
            "INSERT INTO public.sleep_domain_domain_outbox ("
            "delivery_offset, event_id, namespace_id, data_mode, event_type, "
            "aggregate_type, aggregate_id, aggregate_version, "
            "per_aggregate_sequence, subject_id, operation_id, status, "
            "attempt_count, available_at, event_json, created_at, "
            "protocol_version, namespace_generation, run_id, arm_id) VALUES ("
            "%s, %s, %s, %s, 'CARE_FOLLOWUP_PENDING', 'CareFollowup', %s, 1, "
            "1, %s, %s, 'pending', 0, %s, %s::jsonb, %s, 2, %s, %s, %s)",
            (
                delivery_offset,
                care_event_id,
                scope.namespace_id,
                scope.data_mode,
                night_episode_id,
                scope.subject_id,
                claim.work_id,
                occurred_at,
                _json(event),
                occurred_at,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
            ),
        )

    def _interaction_feedback(
        self,
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        prepared: PreparedStage2Command,
        fact_id: str,
        revision_id: str,
        product_operation_id: str,
        link_id: str,
    ) -> dict[str, Any]:
        role = str(prepared.authorization_snapshot.get("role"))
        if role not in {"elder", "family"}:
            raise Stage2AuthorizationRevoked(
                "this role cannot create a canonical human feedback fact"
            )
        source = prepared.fact_snapshot["source"]
        canonical_source = (
            "elder_self_report" if role == "elder" else "family_observation"
        )
        fact = {
            "schema_version": "human_sleep_fact.v1",
            "canonical_source": canonical_source,
            "night_episode_id": source["night_episode_id"],
            "night_episode_revision_id": source["night_episode_revision_id"],
            "event_at": prepared.payload["event_at"],
            "feedback": prepared.payload["feedback"],
        }
        cursor.execute(
            "INSERT INTO public.backend_human_facts ("
            "human_fact_id, namespace_id, data_mode, namespace_generation, "
            "run_id, arm_id, subject_id, night_episode_id, "
            "night_episode_revision_id, operation_id, actor_id, binding_id, "
            "canonical_source, event_at, fact_sha256, fact_json) VALUES ("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s::timestamptz, %s, %s::jsonb)",
            (
                fact_id,
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                scope.subject_id,
                source["night_episode_id"],
                source["night_episode_revision_id"],
                claim.work_id,
                prepared.authorization_snapshot["actor_id"],
                prepared.authorization_snapshot["binding_id"],
                canonical_source,
                prepared.payload["event_at"],
                _sha256(fact),
                _json(fact),
            ),
        )
        self._insert_product_child(
            cursor,
            scope=scope,
            claim=claim,
            prepared=prepared,
            product_operation_id=product_operation_id,
            link_id=link_id,
            trigger="human_feedback",
        )
        if prepared.interaction_id is not None:
            interaction = self._interaction_source(
                cursor,
                scope=scope,
                interaction_id=prepared.interaction_id,
                lock=True,
            )
            if str(interaction[5]) not in {"confirmed", "declined", "completed"}:
                next_revision = int(interaction[6]) + 1
                revision = {
                    "schema_version": "product_interaction_revision.v1",
                    "command_type": prepared.command_type,
                    "state": "completed",
                    "human_fact_id": fact_id,
                    "parent_revision": int(interaction[6]),
                }
                state_sha256 = _sha256(revision)
                cursor.execute(
                    "UPDATE public.backend_product_interactions SET "
                    "state = 'completed', current_revision = %s, "
                    "cas_version = cas_version + 1, current_state_sha256 = %s, "
                    "updated_at = clock_timestamp() WHERE interaction_id = %s "
                    "AND current_revision = %s AND cas_version = %s",
                    (
                        next_revision,
                        state_sha256,
                        prepared.interaction_id,
                        int(interaction[6]),
                        int(interaction[6]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise Stage2Conflict("feedback interaction CAS lost")
                self._insert_interaction_revision(
                    cursor,
                    scope=scope,
                    claim=claim,
                    interaction_id=prepared.interaction_id,
                    revision_id=revision_id,
                    revision_number=next_revision,
                    state="completed",
                    fact_snapshot=prepared.fact_snapshot,
                    revision=revision,
                )
        return {
            "schema_version": "product_interaction_result.v1",
            "result_ref": prepared.interaction_id or fact_id,
            "interaction_id": prepared.interaction_id,
            "interaction_state": (
                "completed" if prepared.interaction_id is not None else None
            ),
            "public_state": "succeeded",
            "human_fact_id": fact_id,
            "product_operation_id": product_operation_id,
        }

    @staticmethod
    def _insert_interaction_revision(
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        interaction_id: str,
        revision_id: str,
        revision_number: int,
        state: str,
        fact_snapshot: Mapping[str, Any],
        revision: Mapping[str, Any],
    ) -> None:
        cursor.execute(
            "INSERT INTO public.backend_product_interaction_revisions ("
            "interaction_revision_id, interaction_id, namespace_id, data_mode, "
            "namespace_generation, run_id, arm_id, subject_id, operation_id, "
            "revision_number, state, fact_snapshot_sha256, fact_snapshot_json, "
            "revision_sha256, revision_json) VALUES ("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, "
            "%s, %s::jsonb)",
            (
                revision_id,
                interaction_id,
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                scope.subject_id,
                claim.work_id,
                revision_number,
                state,
                _sha256(fact_snapshot),
                _json(fact_snapshot),
                _sha256(revision),
                _json(revision),
            ),
        )

    @staticmethod
    def _insert_handle(
        cursor: Any,
        *,
        scope: UowScope,
        prepared: PreparedStage2Command,
        interaction_id: str,
        handle_id: str,
        handle_kind: str,
        state_version: int,
        state_sha256: str,
        prompt: str,
    ) -> None:
        handle = {
            "schema_version": "opaque_pending_handle.v1",
            "interaction_id": interaction_id,
            "handle_kind": handle_kind,
            "target_state_version": state_version,
            "target_sha256": state_sha256,
            "fact_snapshot_sha256": prepared.fact_snapshot_sha256,
            "prompt": prompt,
        }
        cursor.execute(
            "INSERT INTO public.backend_pending_handles ("
            "handle_id, handle_kind, namespace_id, data_mode, "
            "namespace_generation, run_id, arm_id, subject_id, actor_id, role, "
            "target_resource_type, target_resource_id, target_state_version, "
            "target_sha256, fact_snapshot_sha256, care_profile_state_version, "
            "authorization_epoch, privacy_epoch, retrieval_policy_epoch, "
            "policy_sha256, status, expires_at, handle_json) VALUES ("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "'product_interaction', %s, %s, %s, %s, 1, %s, %s, %s, %s, "
            "'pending', clock_timestamp() + interval '24 hours', %s::jsonb)",
            (
                handle_id,
                handle_kind,
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                scope.subject_id,
                prepared.authorization_snapshot["actor_id"],
                prepared.authorization_snapshot["role"],
                interaction_id,
                state_version,
                state_sha256,
                prepared.fact_snapshot_sha256,
                scope.authorization_epoch,
                scope.privacy_epoch,
                scope.retrieval_policy_epoch,
                prepared.fact_snapshot["policy_sha256"],
                _json(handle),
            ),
        )

    def _finish_operation(
        self,
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        prepared: PreparedStage2Command,
        result: Mapping[str, Any],
        event_id: str,
        audit_id: str,
    ) -> None:
        terminal_event = {
            "schema_version": "committed_event.v2",
            "event_id": event_id,
            "event_type": "STAGE2_COMMAND_COMMITTED",
            "operation_id": claim.work_id,
            "command_type": prepared.command_type,
            "result_ref": result.get("result_ref")
            or result.get("result_resource_id"),
            "data_mode": scope.data_mode,
            "synthetic_non_release": scope.data_mode == "replay",
        }
        cursor.execute(
            "INSERT INTO public.sleep_domain_domain_outbox ("
            "event_id, namespace_id, data_mode, event_type, aggregate_type, "
            "aggregate_id, aggregate_version, per_aggregate_sequence, "
            "subject_id, operation_id, status, available_at, event_json, "
            "created_at, protocol_version, namespace_generation, run_id, arm_id) "
            "VALUES (%s, %s, %s, 'STAGE2_COMMAND_COMMITTED', 'Operation', %s, "
            "%s, 2, %s, %s, 'committed', clock_timestamp(), %s::jsonb, "
            "clock_timestamp(), 2, %s, %s, %s)",
            (
                event_id,
                scope.namespace_id,
                scope.data_mode,
                claim.work_id,
                claim.operation_version + 1,
                scope.subject_id,
                claim.work_id,
                _json(terminal_event),
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
            ),
        )
        if result.get("care_action_id") is not None:
            self._insert_pending_delivery(
                cursor,
                scope=scope,
                claim=claim,
                result=result,
                source_event_id=event_id,
            )
        cursor.execute(
            "UPDATE public.sleep_domain_operations SET status = 'succeeded', "
            "outcome_class = 'succeeded', "
            "operation_json = operation_json || jsonb_build_object("
            "'result', %s::jsonb, 'interaction_id', %s::text), "
            "cas_version = cas_version + 1, updated_at = clock_timestamp(), "
            "lease_owner = NULL, lease_expires_at = NULL, fencing_token = NULL, "
            "worker_instance = NULL, heartbeat_at = NULL "
            "WHERE operation_id = %s AND status = 'running' AND cas_version = %s "
            "AND lease_generation = %s AND fencing_token = %s "
            "AND worker_instance = %s AND lease_expires_at > clock_timestamp() "
            "RETURNING cas_version",
            (
                _json(result),
                result.get("interaction_id"),
                claim.work_id,
                claim.operation_version,
                claim.lease_generation,
                claim.fencing_token,
                claim.worker_instance,
            ),
        )
        if cursor.fetchone() is None:
            raise Stage2LeaseLost("terminal Stage-2 Operation CAS lost")
        cursor.execute(
            "INSERT INTO public.backend_authorization_audit ("
            "audit_id, namespace_id, data_mode, subject_id, principal_id, "
            "actor_id, binding_id, decision, reason_code, policy_sha256, "
            "authorization_epoch, privacy_epoch, retrieval_policy_epoch, "
            "audit_json, occurred_at) VALUES ("
            "%s, %s, %s, %s, %s, %s, %s, 'allow', "
            "'stage2_command_committed', %s, %s, %s, %s, %s::jsonb, "
            "clock_timestamp())",
            (
                audit_id,
                scope.namespace_id,
                scope.data_mode,
                scope.subject_id,
                prepared.authorization_snapshot["principal_id"],
                prepared.authorization_snapshot["actor_id"],
                prepared.authorization_snapshot["binding_id"],
                prepared.fact_snapshot["policy_sha256"],
                scope.authorization_epoch,
                scope.privacy_epoch,
                scope.retrieval_policy_epoch,
                _json(
                    {
                        "operation_id": claim.work_id,
                        "command_type": prepared.command_type,
                        "result_kind": result.get("schema_version"),
                    }
                ),
            ),
        )

    def _insert_pending_delivery(
        self,
        cursor: Any,
        *,
        scope: UowScope,
        claim: LeaseClaim,
        result: Mapping[str, Any],
        source_event_id: str,
    ) -> None:
        care_action_id = _required_text(result.get("care_action_id"), "care action")
        delivery_intent_id = _required_text(
            result.get("delivery_intent_id"), "delivery intent"
        )
        effect_key = _sha256(
            {"destination": "replay_care_notification", "care_action_id": care_action_id}
        )
        recipient_actor_id = _required_text(
            claim.authorization_snapshot.get("actor_id"), "delivery recipient actor"
        )
        recipient_binding_id = _required_text(
            claim.authorization_snapshot.get("binding_id"),
            "delivery recipient binding",
        )
        recipient_role = _required_text(
            claim.authorization_snapshot.get("role"), "delivery recipient role"
        )
        human_decision_id = _required_text(
            result.get("human_decision_id"), "delivery confirmation decision"
        )
        payload = {
            "schema_version": "delivery_intent.v2",
            "care_action_id": care_action_id,
            "human_decision_id": human_decision_id,
            "recipient_actor_id": recipient_actor_id,
            "recipient_binding_id": recipient_binding_id,
            "recipient_role": recipient_role,
            "synthetic_non_release": scope.data_mode == "replay",
        }
        authorization = {
            "authorization_epoch": scope.authorization_epoch,
            "privacy_epoch": scope.privacy_epoch,
            "retrieval_policy_epoch": scope.retrieval_policy_epoch,
        }
        cursor.execute(
            "INSERT INTO public.backend_delivery_intents ("
            "delivery_intent_id, namespace_id, data_mode, namespace_generation, "
            "run_id, arm_id, subject_id, source_event_id, destination, "
            "handler_name, semantic_effect_key, aggregate_type, aggregate_id, "
            "aggregate_sequence, status, available_at, "
            "authorization_snapshot_json, payload_sha256, intent_json, "
            "protocol_version, recipient_actor_id, recipient_binding_id, "
            "confirmation_decision_id) VALUES ("
            "%s, %s, %s, %s, %s, %s, %s, %s, 'replay_care_notification', "
            "'deterministic_replay_sink', %s, 'CareAction', %s, 1, 'pending', "
            "clock_timestamp(), %s::jsonb, %s, %s::jsonb, 2, %s, %s, %s)",
            (
                delivery_intent_id,
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                scope.subject_id,
                source_event_id,
                effect_key,
                care_action_id,
                _json(authorization),
                _sha256(payload),
                _json(payload),
                recipient_actor_id,
                recipient_binding_id,
                human_decision_id,
            ),
        )


class Stage2WorkHandler:
    def __init__(
        self,
        *,
        queue: str,
        processor_factory: Callable[
            [UnitOfWorkFactory[Any]], Stage2CommandProcessor
        ] = Stage2CommandProcessor,
    ) -> None:
        if queue not in {SLEEP_COMMAND_QUEUE, PRODUCT_INTERACTION_QUEUE}:
            raise ValueError("unknown Stage-2 queue")
        self.queue = queue
        self.processor_factory = processor_factory

    def __call__(self, context: WorkContext) -> WorkResult:
        try:
            scope = _stage2_scope(context, queue=self.queue)
            processor = self.processor_factory(worker_uow_factory(context))
            prepared = processor.prepare(scope, context.claim)
            model_response: Mapping[str, Any] | None = None
            if prepared.command_type in MODEL_COMMANDS:
                request = {
                    "schema_version": "stage2_model_request.v1",
                    "command_type": prepared.command_type,
                    "payload": prepared.payload,
                    "fact_snapshot": prepared.fact_snapshot,
                    "source_state_version": prepared.source_state_version,
                }
                invocation_key = (
                    f"stage2:{prepared.operation_id}:"
                    f"{prepared.fact_snapshot_sha256}:deterministic.v1"
                )
                model_response = context.invocation_dispatcher().dispatch(
                    invocation_key=invocation_key,
                    request=request,
                    sender=lambda: (
                        _deterministic_model_response(prepared),
                        f"deterministic:{hashlib.sha256(invocation_key.encode()).hexdigest()[:24]}",
                    ),
                    invocation_kind=InvocationKind.MODEL,
                )
            result = processor.commit(
                scope,
                context.claim,
                prepared,
                model_response=model_response,
            )
        except Stage2AuthorizationRevoked:
            return _terminal("stage2_authorization_revoked")
        except Stage2Conflict:
            return _terminal("stage2_state_conflict")
        except Stage2InvariantError:
            return _terminal("stage2_invariant_violation")
        except (Stage2LeaseLost, LeaseLostError):
            context.mark_lease_lost()
            return WorkResult(
                disposition=WorkDisposition.OUTCOME_UNKNOWN,
                error_code="stage2_lease_lost_reconciliation_required",
            )
        return WorkResult(
            disposition=WorkDisposition.SUCCEEDED,
            result=result,
            finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
        )


def build_stage2_worker_handlers(
    settings: SleepBackendSettings,
) -> dict[str, WorkHandler]:
    selected = {
        queue for queue in settings.worker_queues
        if queue in {SLEEP_COMMAND_QUEUE, PRODUCT_INTERACTION_QUEUE}
    }
    if not selected:
        return {}
    if settings.process_role != ProcessRole.WORKER:
        raise Stage2InvariantError("Stage-2 handlers require a worker profile")
    if (
        settings.data_mode != DataMode.REPLAY
        or settings.model_mode != ModelMode.DETERMINISTIC
    ):
        raise Stage2InvariantError(
            "the current Stage-2 implementation requires deterministic replay"
        )
    return {queue: Stage2WorkHandler(queue=queue) for queue in selected}


def _stage2_scope(context: WorkContext, *, queue: str) -> UowScope:
    claim = context.claim
    if (
        claim.queue != queue
        or claim.operation_id != claim.work_id
        or claim.metadata.get("work_kind") != "operation"
        or claim.metadata.get("queue_name") != queue
        or _queue_for_command(str(claim.metadata.get("operation_type"))) != queue
    ):
        raise Stage2InvariantError("claim is not exact Stage-2 operation work")
    try:
        scope = context.store.uow_scope_for_claim(claim)
    except (AttributeError, TypeError, ValueError) as exc:
        raise Stage2InvariantError("claim cannot form an exact UoW scope") from exc
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
        getattr(scope, name) != value for name, value in expected.items()
    ):
        raise Stage2InvariantError("claim and Stage-2 UoW scope differ")
    snapshot = claim.authorization_snapshot
    if snapshot.get("schema_version") != "authorization_snapshot.v1":
        raise Stage2InvariantError("Stage-2 user authorization snapshot is invalid")
    if any(
        snapshot.get(name) != value
        for name, value in (
            ("namespace_id", scope.namespace_id),
            ("namespace_generation", scope.namespace_generation),
            ("data_mode", scope.data_mode),
            ("run_id", scope.run_id),
            ("arm_id", scope.arm_id),
            ("subject_id", scope.subject_id),
            ("authorization_epoch", scope.authorization_epoch),
            ("privacy_epoch", scope.privacy_epoch),
            ("retrieval_policy_epoch", scope.retrieval_policy_epoch),
        )
    ):
        raise Stage2InvariantError("Stage-2 authorization snapshot drifted")
    return scope


def _deterministic_model_response(
    prepared: PreparedStage2Command,
) -> dict[str, Any]:
    if prepared.command_type in {"interaction.start", "interaction.ask"}:
        return {
            "schema_version": "deterministic_interaction_model.v1",
            "next_state": "waiting_user",
            "prompt": "请补充昨晚影响睡眠的主要感受。",
            "source_fact_snapshot_sha256": prepared.fact_snapshot_sha256,
        }
    if prepared.command_type == "interaction.answer":
        return {
            "schema_version": "deterministic_interaction_model.v1",
            "next_state": "awaiting_confirmation",
            "prompt": "请确认是否创建睡眠习惯跟进行动。",
            "care_candidate": {
                "action_kind": "sleep_hygiene_followup",
                "external_delivery_required": True,
            },
            "source_fact_snapshot_sha256": prepared.fact_snapshot_sha256,
        }
    raise Stage2InvariantError("command has no deterministic model contract")


def _queue_for_command(command_type: str) -> str:
    if command_type.startswith("sleep_api."):
        return SLEEP_COMMAND_QUEUE
    if command_type.startswith("interaction."):
        return PRODUCT_INTERACTION_QUEUE
    raise Stage2InvariantError("unknown Stage-2 operation type")


def _scope_params(scope: UowScope) -> tuple[Any, ...]:
    return (
        scope.namespace_id,
        scope.data_mode,
        scope.namespace_generation,
        scope.subject_id,
        scope.run_id,
        scope.arm_id,
    )


def _json_object(value: Any, field: str) -> dict[str, Any]:
    parsed = value
    if isinstance(value, bytes):
        parsed = json.loads(value.decode("utf-8"))
    elif isinstance(value, str):
        parsed = json.loads(value)
    if not isinstance(parsed, Mapping):
        raise Stage2InvariantError(f"{field} must be a JSON object")
    return dict(parsed)


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=lambda item: item.isoformat(),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Stage2InvariantError(f"{field} is required")
    return value.strip()


def _terminal(code: str) -> WorkResult:
    return WorkResult(disposition=WorkDisposition.TERMINAL, error_code=code)


__all__ = [
    "PreparedStage2Command",
    "Stage2CommandProcessor",
    "Stage2WorkHandler",
    "build_stage2_worker_handlers",
]
