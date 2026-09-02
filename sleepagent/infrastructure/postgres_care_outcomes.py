"""PostgreSQL adapters for deterministic care outcome evaluation."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from sleepagent.application.care_execution import CareExecutionPrincipal
from sleepagent.application.care_outcomes import (
    CareOutcomeReadRepository,
    CareOutcomeView,
)
from sleepagent.domain.care_execution import CareExecutionError, CarePlanEntry
from sleepagent.domain.care_outcomes import (
    CareExecutionEvidence,
    CareOutcome,
    CareOutcomeEvaluationDecision,
    OutcomeLifecycleState,
    OutcomeMetricFact,
    OutcomeNightEvidence,
    PersonalizationEffectReceipt,
    evaluate_care_outcome,
    outcome_policy_for,
)
from sleepagent.application.personalization_governance import (
    OutcomePersonalizationGovernance,
    build_outcome_personalization_governance,
)
from sleepagent.observability import log_event
from sleepagent.persistence.uow import UnitOfWorkFactory, UowScope


_READ_VIEW = """
SELECT plan.plan_json, execution.state, execution.completed_at,
  evaluation.state, evaluation.observation_window_start,
  evaluation.observation_window_end, evaluation.reason_code,
  outcome.outcome_json, receipt.receipt_json
FROM public.backend_care_plans_v1 AS plan
JOIN public.backend_care_execution_states_v1 AS execution
  ON execution.care_plan_id = plan.care_plan_id
LEFT JOIN public.backend_care_outcome_evaluations_v1 AS evaluation
  ON evaluation.care_plan_id = plan.care_plan_id
LEFT JOIN public.backend_care_outcomes_v1 AS outcome
  ON outcome.care_outcome_id = evaluation.current_care_outcome_id
LEFT JOIN public.backend_personalization_effect_receipts_v1 AS receipt
  ON receipt.care_outcome_id = outcome.care_outcome_id
"""


class PostgresCareOutcomeReadRepository(CareOutcomeReadRepository):
    def __init__(self, uow_factory: UnitOfWorkFactory[Any]) -> None:
        self.uow_factory = uow_factory

    def get_outcome_view(
        self,
        principal: CareExecutionPrincipal,
        *,
        care_plan_id: str,
        now: datetime,
    ) -> CareOutcomeView | None:
        del now
        with self.uow_factory.begin(_api_scope(principal)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    _READ_VIEW
                    + """
                    WHERE plan.care_plan_id = %s
                      AND plan.namespace_id = %s AND plan.data_mode = %s
                      AND plan.namespace_generation = %s
                      AND plan.run_id IS NOT DISTINCT FROM %s
                      AND plan.arm_id IS NOT DISTINCT FROM %s
                      AND plan.subject_id = %s
                    """,
                    (
                        care_plan_id,
                        principal.namespace_id,
                        principal.data_mode,
                        principal.namespace_generation,
                        principal.run_id,
                        principal.arm_id,
                        principal.subject_id,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        return None if row is None else _read_view(row)

    def list_outcome_views(
        self,
        principal: CareExecutionPrincipal,
        *,
        limit: int,
        now: datetime,
    ) -> tuple[CareOutcomeView, ...]:
        del now
        with self.uow_factory.begin(_api_scope(principal)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    _READ_VIEW
                    + """
                    WHERE plan.namespace_id = %s AND plan.data_mode = %s
                      AND plan.namespace_generation = %s
                      AND plan.run_id IS NOT DISTINCT FROM %s
                      AND plan.arm_id IS NOT DISTINCT FROM %s
                      AND plan.subject_id = %s
                      AND execution.state = 'completed'
                    ORDER BY execution.completed_at DESC, plan.care_plan_id DESC
                    LIMIT %s
                    """,
                    (
                        principal.namespace_id,
                        principal.data_mode,
                        principal.namespace_generation,
                        principal.run_id,
                        principal.arm_id,
                        principal.subject_id,
                        limit,
                    ),
                )
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return tuple(_read_view(row) for row in rows)


class PostgresCareOutcomeEvaluator:
    """Fenced worker-side evaluator; inserts immutable evidence idempotently."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
        scope: UowScope,
        *,
        lease_generation: int,
        fencing_token: str,
    ) -> None:
        if scope.process_role != "worker" or scope.subject_id is None:
            raise ValueError("care outcome evaluation requires exact worker scope")
        self.uow_factory = uow_factory
        self.scope = scope
        if lease_generation < 1 or not fencing_token:
            raise ValueError("care outcome evaluator requires an exact fence")
        self.lease_generation = lease_generation
        self.fencing_token = fencing_token

    def evaluate_operation(
        self,
        *,
        operation_id: str,
        evaluated_at: datetime,
    ) -> CareOutcomeEvaluationDecision:
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ValueError("care outcome evaluation time must be aware")
        with self.uow_factory.begin(self.scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT public.sleepagent_operation_fence_allows(%s,%s,%s)",
                    (
                        operation_id,
                        self.lease_generation,
                        self.fencing_token,
                    ),
                )
                fence = cursor.fetchone()
                if fence is None or fence[0] is not True:
                    raise CareExecutionError("Care outcome operation lost its fence")
                registration, execution, prior = self._load_case(
                    cursor, operation_id
                )
                was_ready = registration["state"] == (
                    OutcomeLifecycleState.READY_FOR_EVALUATION.value
                )
                nights = self._load_nights(cursor, execution)
                decision = evaluate_care_outcome(
                    execution,
                    nights,
                    evaluated_at=evaluated_at,
                    prior_outcome=prior,
                )
                receipt_created = self._persist(
                    cursor,
                    registration,
                    decision,
                    evaluated_at,
                    operation_id=operation_id,
                )
                uow.commit()
            finally:
                cursor.close()
        _log_decision(
            decision,
            receipt_created=receipt_created,
            was_ready=was_ready,
        )
        return decision

    def _load_case(
        self, cursor: Any, operation_id: str
    ) -> tuple[dict[str, Any], CareExecutionEvidence, CareOutcome | None]:
        cursor.execute(
            """
            SELECT evaluation.evaluation_registration_id,
              evaluation.care_plan_id, evaluation.care_execution_event_id,
              evaluation.action_type, evaluation.completed_at,
              evaluation.execution_authority, evaluation.policy_version,
              evaluation.policy_sha256, evaluation.state,
              evaluation.current_care_outcome_id,
              evaluation.current_evaluation_revision,
              plan.subject_id, plan.source_analysis_revision_id,
              event.resulting_state, event.source_authority,
              outcome.outcome_json,
              operation.operation_json ->> 'evaluation_registration_id'
            FROM public.sleep_domain_operations AS operation
            JOIN public.backend_care_outcome_evaluations_v1 AS evaluation
              ON evaluation.evaluation_registration_id =
                operation.operation_json ->> 'evaluation_registration_id'
            JOIN public.backend_care_plans_v1 AS plan
              ON plan.care_plan_id = evaluation.care_plan_id
            JOIN public.backend_care_execution_events_v1 AS event
              ON event.event_id = evaluation.care_execution_event_id
            LEFT JOIN public.backend_care_outcomes_v1 AS outcome
              ON outcome.care_outcome_id = evaluation.current_care_outcome_id
            WHERE operation.operation_id = %s
              AND operation.operation_type = 'care.outcome.evaluate.v1'
              AND evaluation.namespace_id = %s
              AND evaluation.data_mode = %s
              AND evaluation.namespace_generation = %s
              AND evaluation.subject_id = %s
            FOR UPDATE OF evaluation
            """,
            (
                operation_id,
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.subject_id,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise CareExecutionError("Care outcome operation target is unavailable")
        policy = outcome_policy_for(str(row[3]))
        if row[6] != policy.policy_version or row[7] != policy.policy_hash:
            raise CareExecutionError("Care outcome policy binding changed")
        execution = CareExecutionEvidence(
            care_plan_id=str(row[1]),
            care_execution_event_id=str(row[2]),
            subject_id=str(row[11]),
            action_type=str(row[3]),
            state=str(row[13]),
            completed_at=row[4],
            execution_authority=str(row[14]),
            source_analysis_revision_id=str(row[12]),
            # G9's command function checks active grant/proposal/window before
            # inserting this exact completion event in the same transaction.
            authority_valid_at_completion=True,
            plan_invalidated_before_completion=False,
        )
        registration = {
            "id": str(row[0]),
            "care_plan_id": str(row[1]),
            "current_outcome_id": None if row[9] is None else str(row[9]),
            "current_revision": int(row[10]),
            "state": str(row[8]),
        }
        prior = (
            None
            if row[15] is None
            else CareOutcome.model_validate(_json_object(row[15]))
        )
        return registration, execution, prior

    def _load_nights(
        self, cursor: Any, execution: CareExecutionEvidence
    ) -> tuple[OutcomeNightEvidence, ...]:
        cursor.execute(
            """
            SELECT episode.night_episode_id,
              source_revision.night_episode_revision_id,
              source_revision.revision_number,
              revision.night_finalization_revision_id,
              revision.finalization_revision_number,
              revision.material_sha256, revision.state,
              TRUE, episode.episode_local_date, revision.created_at,
              revision.coverage_status, source_revision.revision_json
            FROM public.sleep_domain_night_finalizations AS finalization
            JOIN public.sleep_domain_night_finalization_revisions AS revision
              ON revision.night_finalization_revision_id =
                finalization.current_finalization_revision_id
            JOIN public.sleep_domain_night_episodes AS episode
              ON episode.night_episode_id = finalization.night_episode_id
             AND episode.namespace_id = finalization.namespace_id
             AND episode.data_mode = finalization.data_mode
            JOIN public.sleep_domain_night_episode_revisions AS source_revision
              ON source_revision.night_episode_revision_id =
                revision.source_night_episode_revision_id
             AND source_revision.namespace_id = revision.namespace_id
             AND source_revision.data_mode = revision.data_mode
            WHERE finalization.namespace_id = %s
              AND finalization.data_mode = %s
              AND finalization.namespace_generation = %s
              AND finalization.run_id IS NOT DISTINCT FROM %s
              AND finalization.arm_id IS NOT DISTINCT FROM %s
              AND finalization.subject_id = %s
              AND finalization.state = 'hard_finalized'
              AND revision.state = 'hard_finalized'
              AND NOT revision.provisional
            ORDER BY episode.episode_local_date,
              revision.finalization_revision_number
            """,
            (
                self.scope.namespace_id,
                self.scope.data_mode,
                self.scope.namespace_generation,
                self.scope.run_id,
                self.scope.arm_id,
                execution.subject_id,
            ),
        )
        rows = cursor.fetchall()
        return tuple(self._night(row, execution) for row in rows)

    @staticmethod
    def _night(row: Any, execution: CareExecutionEvidence) -> OutcomeNightEvidence:
        revision = _json_object(row[11])
        episode = revision.get("episode")
        if not isinstance(episode, dict):
            episode = {}
        wake_at = _instant(episode.get("wake_at"))
        bed_at = _instant(episode.get("bed_at"))
        created_at = row[9]
        observation_end = wake_at or created_at
        coverage = 1.0 if str(row[10]) == "complete" else 0.5
        metrics: list[OutcomeMetricFact] = []
        if wake_at is not None:
            timezone_name = str(episode.get("timezone_name") or "UTC")
            local = wake_at.astimezone(ZoneInfo(timezone_name))
            authority = (
                "night_episode_observed_wake"
                if episode.get("assignment_basis") == "observed_wake"
                else "night_episode_inferred_wake"
            )
            metrics.append(
                OutcomeMetricFact(
                    metric_id="wake_time_local_minute",
                    value=local.hour * 60 + local.minute + local.second / 60,
                    unit="minute_of_local_day",
                    window_semantics="observed_episode_wake_instant",
                    coverage_semantics="hard_finalized_episode",
                    coverage_ratio=coverage,
                    source_authority=authority,
                    observation_semantics_version="observation_semantics.v2",
                    trusted_for_analytics=authority == "night_episode_observed_wake",
                    ambiguity_status="native_v2",
                )
            )
        if bed_at is not None and wake_at is not None and wake_at > bed_at:
            metrics.append(
                OutcomeMetricFact(
                    metric_id="sleep_window_duration",
                    value=(wake_at - bed_at).total_seconds() / 60,
                    unit="minute",
                    window_semantics="episode_bed_to_wake",
                    coverage_semantics="hard_finalized_episode",
                    coverage_ratio=coverage,
                    source_authority="night_episode_device_derived",
                    observation_semantics_version="observation_semantics.v2",
                    trusted_for_analytics=True,
                    ambiguity_status="native_v2",
                )
            )
        return OutcomeNightEvidence(
            night_episode_id=str(row[0]),
            night_episode_revision_id=str(row[1]),
            night_episode_revision_number=int(row[2]),
            finalization_revision_id=str(row[3]),
            finalization_revision_number=int(row[4]),
            finalization_material_sha256=str(row[5]),
            finalization_state=str(row[6]),
            is_current_finalization_revision=bool(row[7]),
            subject_id=execution.subject_id,
            local_sleep_date=str(row[8]),
            observation_end_at=observation_end,
            coverage_status=str(row[10]),
            metrics=tuple(metrics),
        )

    def _persist(
        self,
        cursor: Any,
        registration: dict[str, Any],
        decision: CareOutcomeEvaluationDecision,
        evaluated_at: datetime,
        *,
        operation_id: str,
    ) -> bool:
        receipt_created = False
        outcome = decision.outcome
        if outcome is not None and outcome.care_outcome_id != registration["current_outcome_id"]:
            cursor.execute(
                """
                INSERT INTO public.backend_care_outcomes_v1 (
                  care_outcome_id, evaluation_registration_id, care_plan_id,
                  care_execution_event_id, namespace_id, data_mode,
                  namespace_generation, run_id, arm_id, subject_id,
                  action_type, policy_version, policy_sha256,
                  evaluation_revision, supersedes_care_outcome_id,
                  semantic_sha256, baseline_revision_ids_json,
                  followup_revision_ids_json, outcome_category,
                  evidence_quality, execution_authority, causal_claim,
                  outcome_json, created_at
                ) VALUES (
                  %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                  %s::jsonb,%s::jsonb,%s,%s,%s,FALSE,%s::jsonb,%s
                ) ON CONFLICT (evaluation_registration_id, semantic_sha256)
                  DO NOTHING
                """,
                (
                    outcome.care_outcome_id,
                    registration["id"],
                    outcome.care_plan_id,
                    outcome.care_execution_event_id,
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.run_id,
                    self.scope.arm_id,
                    outcome.subject_id,
                    outcome.action_type.value,
                    outcome.policy_version,
                    outcome.policy_hash,
                    outcome.evaluation_revision,
                    outcome.supersedes_care_outcome_id,
                    outcome.semantic_hash,
                    json.dumps(list(outcome.baseline_revision_ids)),
                    json.dumps(list(outcome.followup_revision_ids)),
                    outcome.outcome_category.value,
                    outcome.evidence_quality.value,
                    outcome.execution_authority,
                    outcome.model_dump_json(),
                    outcome.created_at,
                ),
            )
            outcome_created = cursor.rowcount == 1
            receipt = decision.personalization_receipt
            if receipt is None:
                raise CareExecutionError("Care outcome lacks personalization receipt")
            candidate = receipt.candidate
            cursor.execute(
                """
                INSERT INTO public.backend_personalization_effect_receipts_v1 (
                  receipt_id, supersedes_receipt_id, care_outcome_id,
                  evaluation_registration_id,
                  namespace_id, data_mode, namespace_generation, run_id,
                  arm_id, subject_id, action_type,
                  care_outcome_semantic_sha256, state,
                  personalization_relevant, candidate_type,
                  candidate_semantic_sha256, candidate_json,
                  direct_memory_write, receipt_json, created_at
                ) VALUES (
                  %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                  %s::jsonb,FALSE,%s::jsonb,%s
                ) ON CONFLICT (care_outcome_id) DO NOTHING
                """,
                (
                    receipt.receipt_id,
                    receipt.supersedes_receipt_id,
                    receipt.care_outcome_id,
                    registration["id"],
                    self.scope.namespace_id,
                    self.scope.data_mode,
                    self.scope.namespace_generation,
                    self.scope.run_id,
                    self.scope.arm_id,
                    receipt.subject_id,
                    receipt.action_type.value,
                    receipt.care_outcome_semantic_hash,
                    receipt.state.value,
                    receipt.personalization_relevant,
                    None if candidate is None else candidate.candidate_type.value,
                    None if candidate is None else candidate.candidate_semantic_hash,
                    None if candidate is None else candidate.model_dump_json(),
                    receipt.model_dump_json(),
                    receipt.created_at,
                ),
            )
            receipt_created = outcome_created and cursor.rowcount == 1
            if receipt_created:
                governance = build_outcome_personalization_governance(
                    outcome,
                    receipt,
                )
                _register_personalization_governance(
                    cursor,
                    scope=self.scope,
                    receipt=receipt,
                    governance=governance,
                    registered_at=receipt.created_at,
                )
        cursor.execute(
            """
            UPDATE public.backend_care_outcome_evaluations_v1
            SET state = %s, reason_code = %s,
                current_care_outcome_id = COALESCE(%s, current_care_outcome_id),
                current_evaluation_revision = CASE WHEN %s::text IS NULL
                  THEN current_evaluation_revision ELSE %s END,
                cas_version = cas_version + 1,
                last_evaluated_at = %s, updated_at = %s
            WHERE evaluation_registration_id = %s
              AND public.sleepagent_operation_fence_allows(%s,%s,%s)
            """,
            (
                decision.lifecycle_state.value,
                decision.reason_code,
                None if outcome is None else outcome.care_outcome_id,
                None if outcome is None else outcome.care_outcome_id,
                0 if outcome is None else outcome.evaluation_revision,
                evaluated_at,
                evaluated_at,
                registration["id"],
                operation_id,
                self.lease_generation,
                self.fencing_token,
            ),
        )
        if cursor.rowcount != 1:
            raise CareExecutionError("Care outcome evaluation projection lost CAS")
        return receipt_created


def _register_personalization_governance(
    cursor: Any,
    *,
    scope: UowScope,
    receipt: PersonalizationEffectReceipt,
    governance: OutcomePersonalizationGovernance | None,
    registered_at: datetime,
) -> None:
    supersedes_governance_id = None
    if receipt.supersedes_receipt_id is not None:
        cursor.execute(
            """
            SELECT governance_id
            FROM public.backend_personalization_governance_v1
            WHERE receipt_id = %s
            FOR UPDATE
            """,
            (receipt.supersedes_receipt_id,),
        )
        prior = cursor.fetchone()
        if prior is not None:
            supersedes_governance_id = str(prior[0])
            cursor.execute(
                """
                UPDATE public.backend_personalization_governance_v1
                SET status = 'superseded', state_version = state_version + 1,
                    superseded_at = %s, updated_at = %s
                WHERE governance_id = %s AND status = 'pending'
                """,
                (
                    registered_at,
                    registered_at,
                    supersedes_governance_id,
                ),
            )
            if cursor.rowcount == 1:
                log_event("personalization_candidate_superseded")
    if governance is None:
        return
    candidate = governance.memory_candidate
    cursor.execute(
        """
        INSERT INTO public.backend_personalization_governance_v1 (
          governance_id, receipt_id, care_outcome_id,
          supersedes_governance_id, namespace_id, data_mode,
          namespace_generation, run_id, arm_id, subject_id, care_plan_id,
          action_type, care_outcome_semantic_sha256, outcome_policy_version,
          outcome_policy_sha256, evaluation_revision, outcome_category,
          candidate_semantic_sha256, candidate_target_sha256,
          memory_id, memory_concept_id, memory_purpose, memory_source_ref,
          causal_claim, confirmation_required, status, state_version,
          governance_json, candidate_json, registered_at, updated_at
        ) VALUES (
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,FALSE,TRUE,'pending',1,%s::jsonb,%s::jsonb,%s,%s
        ) ON CONFLICT (receipt_id) DO NOTHING
        """,
        (
            governance.governance_id,
            governance.receipt_id,
            governance.care_outcome_id,
            supersedes_governance_id,
            scope.namespace_id,
            scope.data_mode,
            scope.namespace_generation,
            scope.run_id,
            scope.arm_id,
            governance.subject_id,
            governance.care_plan_id,
            governance.action_type,
            governance.care_outcome_semantic_hash,
            governance.outcome_policy_version,
            governance.outcome_policy_hash,
            governance.evaluation_revision,
            governance.outcome_category.value,
            governance.candidate_semantic_hash,
            governance.candidate_target_hash,
            candidate.candidate_id,
            candidate.concept_id,
            governance.memory_purpose,
            candidate.source_ref,
            governance.model_dump_json(),
            candidate.model_dump_json(),
            registered_at,
            registered_at,
        ),
    )
    log_event(
        "personalization_candidate_registered"
        if cursor.rowcount == 1
        else "personalization_candidate_deduplicated"
    )


def _api_scope(principal: CareExecutionPrincipal) -> UowScope:
    return UowScope(
        namespace_id=principal.namespace_id,
        data_mode=cast(Any, principal.data_mode),
        process_role="api",
        purpose="sleep_care",
        service_principal_id=principal.service_principal_id,
        namespace_generation=principal.namespace_generation,
        subject_id=principal.subject_id,
        actor_id=principal.actor_id,
        actor_role=principal.actor_role.value,
        run_id=principal.run_id,
        arm_id=principal.arm_id,
        authorization_epoch=principal.authorization_epoch,
        privacy_epoch=principal.privacy_epoch,
        retrieval_policy_epoch=principal.retrieval_policy_epoch,
    )


def _read_view(row: Any) -> CareOutcomeView:
    plan = CarePlanEntry.model_validate(_json_object(row[0]))
    state = (
        OutcomeLifecycleState.WAITING_FOR_FOLLOWUP
        if row[3] is None
        else OutcomeLifecycleState(str(row[3]))
    )
    return CareOutcomeView(
        plan=plan,
        execution_state=str(row[1]),
        completed_at=row[2],
        lifecycle_state=state,
        observation_window_start=row[4],
        observation_window_end=row[5],
        reason_code=None if row[6] is None else str(row[6]),
        outcome=(
            None if row[7] is None else CareOutcome.model_validate(_json_object(row[7]))
        ),
        personalization_receipt=(
            None
            if row[8] is None
            else PersonalizationEffectReceipt.model_validate(_json_object(row[8]))
        ),
    )


def _instant(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise CareExecutionError("Care outcome persistence payload is invalid")
    return value


def _log_decision(
    decision: CareOutcomeEvaluationDecision,
    *,
    receipt_created: bool,
    was_ready: bool,
) -> None:
    if was_ready:
        log_event(
            "care_outcome_ready",
            reason_code="eligible_hard_finalized_followup_available",
        )
    event = {
        OutcomeLifecycleState.WAITING_FOR_FOLLOWUP: "care_outcome_waiting",
        OutcomeLifecycleState.READY_FOR_EVALUATION: "care_outcome_ready",
        OutcomeLifecycleState.EVALUATED: "care_outcome_evaluated",
        OutcomeLifecycleState.INSUFFICIENT_DATA: "care_outcome_insufficient_data",
        OutcomeLifecycleState.NOT_COMPARABLE: "care_outcome_not_comparable",
        OutcomeLifecycleState.SUPERSEDED: "care_outcome_superseded",
    }[decision.lifecycle_state]
    log_event(event, reason_code=decision.reason_code)
    if (
        receipt_created
        and decision.outcome is not None
        and decision.outcome.supersedes_care_outcome_id is not None
    ):
        log_event(
            "care_outcome_superseded",
            reason_code="late_authoritative_evidence",
        )
    receipt = decision.personalization_receipt
    if receipt is not None and receipt_created:
        log_event(
            "personalization_effect_receipt_created",
            state=receipt.state.value,
        )
        if receipt.candidate is not None:
            log_event(
                "personalization_candidate_proposed",
                candidate_type=receipt.candidate.candidate_type.value,
            )


__all__ = [
    "PostgresCareOutcomeEvaluator",
    "PostgresCareOutcomeReadRepository",
]
