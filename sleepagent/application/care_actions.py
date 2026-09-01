"""Application seam from accepted CareStrategy to durable G8 authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from sleepagent.domain.care_actions import (
    CARE_ACTION_POLICY_HASH,
    CareActionCandidateV2,
    CareActionProposal,
    CareActionUrgency,
    DeterministicCareActionPolicy,
    taxonomy_rule,
)
from sleepagent.persistence.uow import UowScope
from sleepagent.runtime.contracts import AgentId, CareStrategy, EvidencePacket, stable_hash
from sleepagent.runtime.reports import SharedNightAnalysis


CARE_EVALUATION_OPERATION = "care.evaluate.on_hard.v1"


@dataclass(frozen=True, slots=True)
class CareProposalBuildResult:
    proposal: CareActionProposal | None
    reason_code: str


@dataclass(frozen=True, slots=True)
class CareEvaluationReservation:
    operation_id: str
    semantic_key: str
    created: bool


@dataclass(frozen=True, slots=True)
class CareEvaluationResult:
    operation_id: str
    semantic_key: str
    reason_code: str
    proposal_id: str | None
    proposal_created: bool


def build_care_action_proposal(
    shared: SharedNightAnalysis,
    *,
    subject_id: str,
    analysis_revision_id: str,
    night_finalization_revision_id: str,
    created_at: datetime,
    source_is_current: bool,
    source_is_hard_finalized: bool,
) -> CareProposalBuildResult:
    """Derive structured semantics only from accepted CareStrategy output."""

    if shared.care is None:
        return CareProposalBuildResult(None, "no_care_strategy")
    strategy = CareStrategy.model_validate(shared.care.payload)
    source = strategy.primary_action
    if source is None or strategy.disposition not in {"propose", "adjust"}:
        return CareProposalBuildResult(None, "no_action_candidate")
    if not source.activatable or not source.confirmation_required:
        return CareProposalBuildResult(None, "candidate_not_governable")
    if source.care_action_id is None or source.care_action_version is None:
        return CareProposalBuildResult(None, "candidate_catalog_identity_missing")
    rule = taxonomy_rule(source.care_action_id, source.care_action_version)
    if rule is None:
        return CareProposalBuildResult(None, "unsupported_action")

    accepted_target_hash = getattr(shared.care, "target_hash", None)
    care_invocations = tuple(
        item
        for item in shared.agent_invocations
        if item.agent_id is AgentId.CARE_STRATEGY
        and (
            accepted_target_hash is None
            or getattr(item, "target_hash", None) == accepted_target_hash
        )
    )
    if len(care_invocations) != 1:
        return CareProposalBuildResult(None, "care_invocation_ambiguous")
    evidence = EvidencePacket.model_validate(shared.evidence.payload)
    accepted_claim_ids = {claim.claim_id for claim in evidence.claims}
    rationale_refs = tuple(source.rationale_evidence_refs)
    evidence_sufficient = bool(rationale_refs) and set(rationale_refs).issubset(
        accepted_claim_ids
    )
    urgency = (
        CareActionUrgency.URGENT_SAFETY
        if shared.source.risk_state == "urgent_boundary"
        else CareActionUrgency.WATCH
        if shared.source.risk_state not in {"info", "normal"}
        or shared.source.quality_state == "partial"
        else CareActionUrgency.NORMAL
    )
    invocation = care_invocations[0]
    try:
        candidate = CareActionCandidateV2.create(
            candidate_id=source.candidate_id,
            subject_id=subject_id,
            source_analysis_revision_id=analysis_revision_id,
            source_shared_analysis_sha256=shared.shared_analysis_sha256,
            source_night_finalization_revision_id=(
                night_finalization_revision_id
            ),
            source_care_strategy_invocation_id=invocation.invocation_id,
            source_care_strategy_version=invocation.agent_version,
            source_care_work_product_ref=shared.care.work_product_ref,
            action_type=rule["action_type"],
            catalog_action_id=source.care_action_id,
            catalog_action_version=source.care_action_version,
            intent=rule["intent"],
            rationale_evidence_refs=rationale_refs,
            urgency=urgency,
            audience=rule["audience"],
            parameters=dict(source.parameters),
            created_at=created_at,
            display_explanation=source.title,
        )
    except ValueError:
        return CareProposalBuildResult(None, "malformed_candidate")
    policy = DeterministicCareActionPolicy().evaluate(
        candidate,
        source_is_current=source_is_current,
        source_is_hard_finalized=source_is_hard_finalized,
        evidence_is_sufficient=evidence_sufficient,
    )
    if not policy.eligible:
        return CareProposalBuildResult(None, policy.reason_code)
    return CareProposalBuildResult(
        CareActionProposal.create(candidate, policy),
        policy.reason_code,
    )


def current_hard_finalization_revision(
    cursor: Any,
    scope: UowScope,
    *,
    night_episode_id: str,
    night_episode_revision_id: str,
) -> str | None:
    cursor.execute(
        """
        SELECT finalization.current_finalization_revision_id
        FROM public.sleep_domain_night_finalizations AS finalization
        JOIN public.sleep_domain_night_finalization_revisions AS revision
          ON revision.night_finalization_revision_id =
            finalization.current_finalization_revision_id
         AND revision.night_finalization_id = finalization.night_finalization_id
        WHERE finalization.namespace_id = %s AND finalization.data_mode = %s
          AND finalization.namespace_generation = %s
          AND finalization.run_id IS NOT DISTINCT FROM %s
          AND finalization.arm_id IS NOT DISTINCT FROM %s
          AND finalization.subject_id = %s
          AND finalization.night_episode_id = %s
          AND finalization.state = 'hard_finalized'
          AND revision.state = 'hard_finalized'
          AND revision.provisional = FALSE
          AND revision.source_night_episode_revision_id = %s
        """,
        (
            scope.namespace_id,
            scope.data_mode,
            scope.namespace_generation,
            scope.run_id,
            scope.arm_id,
            scope.subject_id,
            night_episode_id,
            night_episode_revision_id,
        ),
    )
    row = cursor.fetchone()
    return None if row is None else str(row[0])


def reserve_care_evaluation_if_ready(
    cursor: Any,
    scope: UowScope,
    *,
    night_episode_id: str,
    night_episode_revision_id: str,
    reserved_at: datetime,
    operation_id: str,
    hard_finalization_revision_id: str | None = None,
    analysis_revision_id: str | None = None,
    shared_analysis_sha256: str | None = None,
) -> CareEvaluationReservation | None:
    """Reserve one evaluation once exact compatible HARD and analysis exist."""

    if scope.process_role != "worker" or scope.subject_id is None:
        raise ValueError("Care evaluation reservation requires worker authority")
    cursor.execute(
        """
        SELECT revision.night_finalization_revision_id
        FROM public.sleep_domain_night_finalizations AS finalization
        JOIN public.sleep_domain_night_finalization_revisions AS revision
          ON revision.night_finalization_revision_id =
             finalization.current_finalization_revision_id
         AND revision.night_finalization_id = finalization.night_finalization_id
        WHERE finalization.namespace_id = %s AND finalization.data_mode = %s
          AND finalization.namespace_generation = %s
          AND finalization.run_id IS NOT DISTINCT FROM %s
          AND finalization.arm_id IS NOT DISTINCT FROM %s
          AND finalization.subject_id = %s
          AND finalization.night_episode_id = %s
          AND finalization.state = 'hard_finalized'
          AND revision.state = 'hard_finalized'
          AND revision.provisional = FALSE
          AND revision.source_night_episode_revision_id = %s
          AND (%s::text IS NULL OR revision.night_finalization_revision_id = %s)
        FOR SHARE OF finalization
        """,
        (
            scope.namespace_id,
            scope.data_mode,
            scope.namespace_generation,
            scope.run_id,
            scope.arm_id,
            scope.subject_id,
            night_episode_id,
            night_episode_revision_id,
            hard_finalization_revision_id,
            hard_finalization_revision_id,
        ),
    )
    hard_row = cursor.fetchone()
    if hard_row is None:
        return None
    resolved_hard_id = str(hard_row[0])

    cursor.execute(
        """
        SELECT analysis_revision_id,
               analysis_json -> 'shared_analysis' ->> 'shared_analysis_sha256'
        FROM public.sleep_domain_analysis_revisions
        WHERE namespace_id = %s AND data_mode = %s AND subject_id = %s
          AND night_episode_id = %s AND night_episode_revision_id = %s
          AND analysis_json ->> 'schema_version' = 'shared_night_analysis.v1'
          AND jsonb_typeof(analysis_json -> 'shared_analysis') = 'object'
          AND analysis_json #>>
            '{shared_analysis,source,night_episode_id}' = %s
          AND analysis_json #>>
            '{shared_analysis,source,night_episode_revision_id}' = %s
          AND (%s::text IS NULL OR analysis_revision_id = %s)
        ORDER BY revision_number DESC
        LIMIT 1
        """,
        (
            scope.namespace_id,
            scope.data_mode,
            scope.subject_id,
            night_episode_id,
            night_episode_revision_id,
            night_episode_id,
            night_episode_revision_id,
            analysis_revision_id,
            analysis_revision_id,
        ),
    )
    analysis_row = cursor.fetchone()
    if analysis_row is None or analysis_row[1] is None:
        return None
    resolved_analysis_id = str(analysis_row[0])
    resolved_shared_hash = str(analysis_row[1])
    if (
        shared_analysis_sha256 is not None
        and resolved_shared_hash != shared_analysis_sha256
    ):
        raise RuntimeError("Care evaluation shared-analysis identity collision")

    semantic_key = stable_hash(
        {
            "schema_version": "care_evaluation_identity.v1",
            "subject_id": scope.subject_id,
            "analysis_revision_id": resolved_analysis_id,
            "shared_analysis_sha256": resolved_shared_hash,
            "hard_finalization_revision_id": resolved_hard_id,
            "care_policy_version": DeterministicCareActionPolicy.version,
            "care_policy_sha256": CARE_ACTION_POLICY_HASH,
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
    operation = {
        "schema_version": "backend_operation.v2",
        "command_type": CARE_EVALUATION_OPERATION,
        "trigger": "hard_finalization_and_shared_analysis_ready",
        "night_episode_id": night_episode_id,
        "night_episode_revision_id": night_episode_revision_id,
        "analysis_revision_id": resolved_analysis_id,
        "shared_analysis_sha256": resolved_shared_hash,
        "night_finalization_revision_id": resolved_hard_id,
        "care_policy_version": DeterministicCareActionPolicy.version,
        "care_policy_sha256": CARE_ACTION_POLICY_HASH,
        "care_evaluation_semantic_key": semantic_key,
        "authorization_snapshot": workload,
    }
    operation_record = {**operation, "deduplicated_trigger_count": 0}
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (semantic_key,),
    )
    cursor.execute(
        """
        INSERT INTO public.sleep_domain_operations (
          operation_id, namespace_id, data_mode, operation_type,
          subject_id, service_principal_id, actor_id,
          target_resource_id, target_resource_key, idempotency_key,
          request_sha256, status, attempt_count, cas_version,
          operation_json, created_at, updated_at, protocol_version,
          namespace_generation, run_id, arm_id, id_scheme, origin_kind,
          semantic_key, queue_name, priority, available_at, max_attempts,
          workload_authorization_snapshot_json, policy_sha256
        ) VALUES (
          %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s,
          'pending', 0, 0, %s::jsonb, %s, %s, 2, %s, %s, %s,
          'uuidv7', 'system', %s, 'product_agent', 20, %s, 5,
          %s::jsonb, %s
        ) ON CONFLICT DO NOTHING
        """,
        (
            operation_id,
            scope.namespace_id,
            scope.data_mode,
            CARE_EVALUATION_OPERATION,
            scope.subject_id,
            scope.service_principal_id,
            night_episode_id,
            night_episode_revision_id,
            semantic_key,
            semantic_key,
            _json(operation_record),
            reserved_at,
            reserved_at,
            scope.namespace_generation,
            scope.run_id,
            scope.arm_id,
            semantic_key,
            reserved_at,
            _json(workload),
            CARE_ACTION_POLICY_HASH,
        ),
    )
    created = cursor.rowcount == 1
    cursor.execute(
        """
        SELECT operation_id, operation_json
        FROM public.sleep_domain_operations
        WHERE namespace_id = %s AND data_mode = %s
          AND namespace_generation = %s
          AND run_id IS NOT DISTINCT FROM %s
          AND arm_id IS NOT DISTINCT FROM %s
          AND operation_type = %s AND semantic_key = %s
        """,
        (
            scope.namespace_id,
            scope.data_mode,
            scope.namespace_generation,
            scope.run_id,
            scope.arm_id,
            CARE_EVALUATION_OPERATION,
            semantic_key,
        ),
    )
    existing = cursor.fetchone()
    existing_payload = None if existing is None else _json_object(existing[1])
    if existing_payload is None or any(
        existing_payload.get(key) != value for key, value in operation.items()
    ):
        raise RuntimeError("Care evaluation operation semantic collision")
    if not created:
        cursor.execute(
            """
            UPDATE public.sleep_domain_operations
            SET operation_json = jsonb_set(
                  operation_json,
                  '{deduplicated_trigger_count}',
                  to_jsonb(COALESCE(
                    (operation_json ->> 'deduplicated_trigger_count')::BIGINT,
                    0
                  ) + 1),
                  TRUE
                ),
                cas_version = cas_version + 1
            WHERE operation_id = %s
            """,
            (str(existing[0]),),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Care evaluation deduplication signal was lost")
    return CareEvaluationReservation(str(existing[0]), semantic_key, created)


def execute_care_evaluation(
    cursor: Any,
    scope: UowScope,
    *,
    operation_id: str,
    attempt_sequence: int,
    lease_generation: int,
    fencing_token: str,
    worker_instance: str,
    evaluated_at: datetime,
    before_terminal_commit: Callable[[], None] | None = None,
) -> CareEvaluationResult:
    """Evaluate and terminally persist one fenced Care authority."""

    cursor.execute(
        """
        SELECT operation_json, semantic_key, created_at
        FROM public.sleep_domain_operations
        WHERE operation_id = %s AND namespace_id = %s AND data_mode = %s
          AND namespace_generation = %s AND subject_id = %s
          AND run_id IS NOT DISTINCT FROM %s
          AND arm_id IS NOT DISTINCT FROM %s
          AND operation_type = %s AND queue_name = 'product_agent'
          AND status = 'running' AND attempt_count = %s
          AND lease_generation = %s AND fencing_token = %s
          AND worker_instance = %s AND lease_expires_at > clock_timestamp()
        FOR UPDATE
        """,
        (
            operation_id,
            scope.namespace_id,
            scope.data_mode,
            scope.namespace_generation,
            scope.subject_id,
            scope.run_id,
            scope.arm_id,
            CARE_EVALUATION_OPERATION,
            attempt_sequence,
            lease_generation,
            fencing_token,
            worker_instance,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("Care evaluation operation fence was rejected")
    operation = _json_object(row[0])
    semantic_key = str(row[1])
    created_at = row[2]
    night_episode_id = _required_text(operation, "night_episode_id")
    episode_revision_id = _required_text(
        operation, "night_episode_revision_id"
    )
    analysis_revision_id = _required_text(operation, "analysis_revision_id")
    hard_revision_id = _required_text(
        operation, "night_finalization_revision_id"
    )
    shared_hash = _required_text(operation, "shared_analysis_sha256")

    cursor.execute(
        """
        SELECT analysis.analysis_json -> 'shared_analysis',
               episode.current_revision_id,
               finalization.current_finalization_revision_id,
               revision.state, revision.provisional,
               revision.source_night_episode_revision_id
        FROM public.sleep_domain_analysis_revisions AS analysis
        JOIN public.sleep_domain_night_episodes AS episode
          ON episode.night_episode_id = analysis.night_episode_id
         AND episode.namespace_id = analysis.namespace_id
         AND episode.data_mode = analysis.data_mode
         AND episode.subject_id = analysis.subject_id
        JOIN public.sleep_domain_night_finalizations AS finalization
          ON finalization.night_episode_id = episode.night_episode_id
         AND finalization.namespace_id = episode.namespace_id
         AND finalization.data_mode = episode.data_mode
         AND finalization.namespace_generation = episode.namespace_generation
         AND finalization.subject_id = episode.subject_id
        JOIN public.sleep_domain_night_finalization_revisions AS revision
          ON revision.night_finalization_revision_id = %s
         AND revision.night_finalization_id = finalization.night_finalization_id
        WHERE analysis.analysis_revision_id = %s
          AND analysis.namespace_id = %s AND analysis.data_mode = %s
          AND analysis.subject_id = %s AND analysis.night_episode_id = %s
          AND analysis.night_episode_revision_id = %s
        FOR SHARE OF episode, finalization
        """,
        (
            hard_revision_id,
            analysis_revision_id,
            scope.namespace_id,
            scope.data_mode,
            scope.subject_id,
            night_episode_id,
            episode_revision_id,
        ),
    )
    source_row = cursor.fetchone()
    if source_row is None:
        raise RuntimeError("Care evaluation source authority is unavailable")
    shared = SharedNightAnalysis.model_validate(_json_object(source_row[0]))
    if shared.shared_analysis_sha256 != shared_hash:
        raise RuntimeError("Care evaluation analysis hash changed")
    if (
        shared.source.night_episode_id != night_episode_id
        or shared.source.night_episode_revision_id != episode_revision_id
    ):
        raise RuntimeError("Care evaluation analysis lineage changed")
    source_is_current = (
        str(source_row[1]) == episode_revision_id
        and str(source_row[2]) == hard_revision_id
    )
    source_is_hard = (
        str(source_row[3]) == "hard_finalized"
        and not bool(source_row[4])
        and str(source_row[5]) == episode_revision_id
    )
    build = build_care_action_proposal(
        shared,
        subject_id=str(scope.subject_id),
        analysis_revision_id=analysis_revision_id,
        night_finalization_revision_id=hard_revision_id,
        created_at=created_at,
        source_is_current=source_is_current,
        source_is_hard_finalized=source_is_hard,
    )
    proposal_created = False
    proposal_id = None
    if build.proposal is not None:
        proposal_id = build.proposal.proposal_id
        proposal_created = persist_care_action_proposal(
            cursor,
            scope,
            night_episode_id=night_episode_id,
            proposal=build.proposal,
            persisted_at=evaluated_at,
        )
    if before_terminal_commit is not None:
        before_terminal_commit()
    terminal = {
        **operation,
        "result": {
            "schema_version": "care_evaluation_result.v1",
            "care_evaluation_semantic_key": semantic_key,
            "reason_code": build.reason_code,
            "proposal_id": proposal_id,
            "proposal_created": proposal_created,
            "evaluated_at": evaluated_at,
        },
    }
    cursor.execute(
        """
        UPDATE public.sleep_domain_operations
        SET status = 'succeeded', outcome_class = 'succeeded',
            operation_json = %s::jsonb, updated_at = %s,
            cas_version = cas_version + 1,
            lease_owner = NULL, lease_expires_at = NULL,
            fencing_token = NULL, worker_instance = NULL, heartbeat_at = NULL
        WHERE operation_id = %s AND status = 'running'
          AND attempt_count = %s AND lease_generation = %s
          AND fencing_token = %s AND worker_instance = %s
          AND lease_expires_at > clock_timestamp()
        """,
        (
            _json(terminal),
            evaluated_at,
            operation_id,
            attempt_sequence,
            lease_generation,
            fencing_token,
            worker_instance,
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Care evaluation fence expired before commit")
    return CareEvaluationResult(
        operation_id,
        semantic_key,
        build.reason_code,
        proposal_id,
        proposal_created,
    )


def persist_care_action_proposal(
    cursor: Any,
    scope: UowScope,
    *,
    night_episode_id: str,
    proposal: CareActionProposal,
    persisted_at: datetime,
) -> bool:
    """Insert/reuse one proposal and expire older analysis authority."""

    if scope.process_role != "worker" or scope.subject_id is None:
        raise ValueError("Care proposal persistence requires worker subject authority")
    candidate = proposal.candidate
    if candidate.subject_id != scope.subject_id:
        raise ValueError("Care proposal subject differs from worker authority")
    cursor.execute(
        """
        SELECT proposal_id, state, version, updated_at, policy_version,
               policy_sha256
        FROM public.backend_care_action_proposals_v3
        WHERE namespace_id = %s AND data_mode = %s
          AND namespace_generation = %s
          AND run_id IS NOT DISTINCT FROM %s
          AND arm_id IS NOT DISTINCT FROM %s
          AND subject_id = %s AND night_episode_id = %s
          AND source_analysis_revision_id <> %s
          AND state IN ('awaiting_approval', 'approved')
        FOR UPDATE
        """,
        (
            scope.namespace_id,
            scope.data_mode,
            scope.namespace_generation,
            scope.run_id,
            scope.arm_id,
            scope.subject_id,
            night_episode_id,
            candidate.source_analysis_revision_id,
        ),
    )
    for row in cursor.fetchall():
        old_proposal_id = str(row[0])
        old_state = str(row[1])
        old_version = int(row[2])
        transition_at = max(persisted_at, row[3] + timedelta(microseconds=1))
        cursor.execute(
            """
            UPDATE public.backend_care_action_proposals_v3
            SET state = 'expired', version = version + 1, updated_at = %s
            WHERE proposal_id = %s AND state = %s AND version = %s
            """,
            (transition_at, old_proposal_id, old_state, old_version),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Care proposal supersession lost its CAS fence")
        decision_id = "care-decision:" + stable_hash(
            {"proposal_id": old_proposal_id, "choice": "supersede"}
        )
        decision_json = {
            "schema_version": "care_action_decision.v1",
            "choice": "supersede",
            "reason_code": "source_analysis_superseded",
        }
        cursor.execute(
            """
            INSERT INTO public.backend_care_action_decisions_v3 (
              decision_id, proposal_id, namespace_id, data_mode,
              namespace_generation, run_id, arm_id, subject_id,
              actor_id, binding_id, actor_role, choice, idempotency_key,
              reason_code, reason_text, previous_state, resulting_state,
              proposal_version, policy_version, policy_sha256,
              decision_json, decided_at
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s,
              NULL, NULL, 'system', 'supersede', %s,
              'source_analysis_superseded', NULL, %s, 'expired',
              %s, %s, %s, %s::jsonb, %s
            ) ON CONFLICT (decision_id) DO NOTHING
            """,
            (
                decision_id,
                old_proposal_id,
                scope.namespace_id,
                scope.data_mode,
                scope.namespace_generation,
                scope.run_id,
                scope.arm_id,
                scope.subject_id,
                f"system:supersede:{old_proposal_id}",
                old_state,
                old_version + 1,
                str(row[4]),
                str(row[5]),
                _json(decision_json),
                transition_at,
            ),
        )

    cursor.execute(
        """
        INSERT INTO public.backend_care_action_proposals_v3 (
          proposal_id, namespace_id, data_mode, namespace_generation,
          run_id, arm_id, subject_id, night_episode_id,
          source_analysis_revision_id, source_shared_analysis_sha256,
          source_night_finalization_revision_id,
          source_care_strategy_invocation_id, candidate_id, candidate_sha256,
          proposal_semantic_key, proposal_semantic_sha256, action_type,
          catalog_action_id, catalog_action_version, audience_role,
          required_approver_role, authorization_scope, urgency,
          policy_version, policy_sha256, policy_decision, policy_reason_code,
          state, version, candidate_json, proposal_json,
          created_at, expires_at, updated_at
        ) VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
          'eligible', %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s
        ) ON CONFLICT DO NOTHING
        """,
        (
            proposal.proposal_id,
            scope.namespace_id,
            scope.data_mode,
            scope.namespace_generation,
            scope.run_id,
            scope.arm_id,
            scope.subject_id,
            night_episode_id,
            candidate.source_analysis_revision_id,
            candidate.source_shared_analysis_sha256,
            candidate.source_night_finalization_revision_id,
            candidate.source_care_strategy_invocation_id,
            candidate.candidate_id,
            candidate.candidate_hash,
            proposal.proposal_semantic_key,
            proposal.proposal_semantic_hash,
            candidate.action_type.value,
            candidate.catalog_action_id,
            candidate.catalog_action_version,
            candidate.audience.value,
            proposal.required_approver_role.value,
            proposal.authorization_scope,
            candidate.urgency.value,
            proposal.policy.policy_version,
            CARE_ACTION_POLICY_HASH,
            proposal.policy.reason_code,
            proposal.state.value,
            proposal.version,
            candidate.model_dump_json(),
            proposal.model_dump_json(),
            proposal.created_at,
            proposal.expires_at,
            persisted_at,
        ),
    )
    created = cursor.rowcount == 1
    cursor.execute(
        """
        SELECT proposal_semantic_sha256, candidate_sha256
        FROM public.backend_care_action_proposals_v3
        WHERE namespace_id = %s AND data_mode = %s
          AND namespace_generation = %s
          AND run_id IS NOT DISTINCT FROM %s
          AND arm_id IS NOT DISTINCT FROM %s
          AND proposal_semantic_key = %s
        """,
        (
            scope.namespace_id,
            scope.data_mode,
            scope.namespace_generation,
            scope.run_id,
            scope.arm_id,
            proposal.proposal_semantic_key,
        ),
    )
    row = cursor.fetchone()
    if row is None or (
        str(row[0]) != proposal.proposal_semantic_hash
        or str(row[1]) != candidate.candidate_hash
    ):
        raise RuntimeError("Care proposal semantic identity collision")
    return created


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise RuntimeError("Care authority JSON must be an object")
    return value


def _required_text(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RuntimeError(f"Care authority is missing {key}")
    return item


__all__ = [
    "CARE_EVALUATION_OPERATION",
    "CareEvaluationReservation",
    "CareEvaluationResult",
    "CareProposalBuildResult",
    "build_care_action_proposal",
    "current_hard_finalization_revision",
    "execute_care_evaluation",
    "persist_care_action_proposal",
    "reserve_care_evaluation_if_ready",
]
