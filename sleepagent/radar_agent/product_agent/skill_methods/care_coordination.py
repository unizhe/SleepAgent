from __future__ import annotations

from typing import Literal

from pydantic import Field

from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    CoordinationCandidate,
    StrictContract,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.policies.care_coordination import (
    CareCapabilityIntent,
    CareEscalationPolicy,
    CareRoutingDecision,
)


class CareCapabilityPlan(StrictContract):
    evidence_refs: list[str] = Field(min_length=1, max_length=40)
    primary_care_intents: list[CareCapabilityIntent] = Field(default_factory=list)
    questionnaire_intents: list[CareCapabilityIntent] = Field(default_factory=list)
    coordination_candidates: list[CoordinationCandidate] = Field(
        default_factory=list
    )
    external_action_intents: list[CareCapabilityIntent] = Field(
        default_factory=list
    )
    side_effect_executed: Literal[False] = False


class CareCoordinationSkill:
    """Care-owned method that projects policy intents into typed candidates."""

    owner = AgentId.CARE_STRATEGY
    skill_id = "draft_coordination_candidate"
    skill_version = "1.0.0"

    def plan(
        self,
        decision: CareRoutingDecision,
        *,
        episode_id: str,
        evidence_refs: list[str],
    ) -> CareCapabilityPlan:
        if not episode_id:
            raise ValueError("Care coordination requires an Episode identity")
        canonical_decision = CareEscalationPolicy().evaluate(
            risk_level=decision.risk_level,
            data_quality_status=decision.data_quality_status,
        )
        if decision != canonical_decision:
            raise ValueError(
                "Care coordination requires the canonical Care policy decision"
            )
        bound_refs = list(dict.fromkeys(ref for ref in evidence_refs if ref))
        if not bound_refs:
            raise ValueError(
                "Care coordination requires accepted Evidence references"
            )
        primary = [
            item
            for item in decision.candidate_intents
            if item.kind == "primary_care"
        ]
        if len(primary) > 1:
            raise ValueError("Care planning permits at most one primary action")
        questionnaires = [
            item
            for item in decision.candidate_intents
            if item.kind == "questionnaire"
        ]
        coordination = [
            self._coordination_candidate(
                item,
                episode_id=episode_id,
                evidence_refs=bound_refs,
            )
            for item in decision.candidate_intents
            if item.kind in {"coordination", "delivery_record"}
        ]
        external = [
            item
            for item in decision.candidate_intents
            if item.kind in {"artifact", "external_action"}
        ]
        return CareCapabilityPlan(
            evidence_refs=bound_refs,
            primary_care_intents=primary,
            questionnaire_intents=questionnaires,
            coordination_candidates=coordination,
            external_action_intents=external,
        )

    @staticmethod
    def _coordination_candidate(
        intent: CareCapabilityIntent,
        *,
        episode_id: str,
        evidence_refs: list[str],
    ) -> CoordinationCandidate:
        material = {
            "episode_id": episode_id,
            "action_code": intent.action_code,
            "evidence_refs": evidence_refs,
        }
        digest = stable_hash(material)
        return CoordinationCandidate(
            candidate_id=f"coordination:{digest[:24]}",
            recipient_role="family",
            reason=(
                f"{intent.action_code} is a candidate derived from accepted "
                "Evidence and deterministic coordination policy."
            ),
            timing=(
                "immediate"
                if intent.kind == "delivery_record"
                else "next_available_window"
            ),
            dedupe_key=f"coordination:{episode_id}:{intent.action_code}",
            stop_conditions=[
                "authorization_changed",
                "target_superseded",
                "user_declined",
            ],
        )


__all__ = ["CareCapabilityPlan", "CareCoordinationSkill"]
