"""Application seam from accepted CareStrategy to durable G8 authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

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


@dataclass(frozen=True, slots=True)
class CareProposalBuildResult:
    proposal: CareActionProposal | None
    reason_code: str


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

    care_invocations = tuple(
        item
        for item in shared.agent_invocations
        if item.agent_id is AgentId.CARE_STRATEGY
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


__all__ = [
    "CareProposalBuildResult",
    "build_care_action_proposal",
    "current_hard_finalization_revision",
    "persist_care_action_proposal",
]
