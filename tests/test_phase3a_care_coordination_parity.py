from __future__ import annotations

from datetime import date

import pytest

from sleepagent.radar_agent.agents import (
    AlertCareAgent,
    ContextPacket,
    EvidencePacket,
    TaskContext,
)
from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
)
from sleepagent.radar_agent.product_agent.policies.care_coordination import (
    CareEscalationPolicy,
)
from sleepagent.radar_agent.product_agent.skill_methods.care_coordination import (
    CareCapabilityPlan,
    CareCoordinationSkill,
)
from sleepagent.radar_agent.product_agent.tools.care_coordination import (
    CareCoordinationPolicyRequest,
    CareCoordinationTool,
    CareRiskReceiptBinding,
)
from sleepagent.radar_agent.schemas import (
    EvidenceClaim,
    EvidenceLedger,
    RadarDataQualityStatus,
    RadarNightSummary,
    ReviewStatus,
    RiskLevel,
)


@pytest.mark.parametrize(
    ("risk", "quality"),
    [
        (RiskLevel.INFO, "good"),
        (RiskLevel.WATCH, "good"),
        (RiskLevel.UNCERTAIN, "partial"),
        (RiskLevel.ESCALATE, "good"),
        (RiskLevel.URGENT_BOUNDARY, "good"),
    ],
)
def test_care_escalation_policy_preserves_alert_capabilities_without_agent_identity(
    risk: RiskLevel,
    quality: str,
) -> None:
    legacy = AlertCareAgent().run(_context(risk=risk, quality=quality))
    legacy_decision = legacy.output_payload["alert_care"]

    migrated = CareEscalationPolicy().evaluate(
        risk_level=risk.value,
        data_quality_status=quality,
    )

    assert [item.action_code for item in migrated.candidate_intents] == (
        legacy_decision["candidate_actions"]
    )
    assert list(migrated.automatic_communication_codes) == (
        legacy_decision["automatic_actions"]
    )
    assert migrated.urgent_preempt == legacy_decision[
        "urgent_safety_notice_displayed"
    ]
    assert not hasattr(migrated, "agent_name")
    assert not hasattr(migrated, "external_action_executed")


def test_watch_capabilities_are_split_into_one_care_questionnaire_and_coordination() -> None:
    decision = CareEscalationPolicy().evaluate(
        risk_level="watch",
        data_quality_status="good",
    )

    plan = CareCoordinationSkill().plan(
        decision,
        episode_id="phase3a-care",
        evidence_refs=["evidence:phase3a-watch"],
    )

    assert CareCoordinationSkill.owner is AgentId.CARE_STRATEGY
    assert CareCoordinationSkill.skill_id == "draft_coordination_candidate"
    assert len(plan.primary_care_intents) == 1
    assert len(plan.questionnaire_intents) == 1
    assert len(plan.coordination_candidates) == 1
    assert plan.coordination_candidates[0].recipient_role == "family"
    assert plan.evidence_refs == ["evidence:phase3a-watch"]
    assert plan.external_action_intents == []
    assert plan.side_effect_executed is False


def test_coordination_candidate_requires_accepted_evidence_binding() -> None:
    decision = CareEscalationPolicy().evaluate(
        risk_level="watch",
        data_quality_status="good",
    )

    with pytest.raises(ValueError, match="accepted Evidence"):
        CareCoordinationSkill().plan(
            decision,
            episode_id="phase3a-care",
            evidence_refs=[],
        )


def test_care_skill_rejects_caller_forged_policy_intents() -> None:
    canonical = CareEscalationPolicy().evaluate(
        risk_level="escalate",
        data_quality_status="good",
    )
    forged = canonical.model_copy(
        update={
            "candidate_intents": [
                item.model_copy(
                    update={
                        "confirmation_required": False,
                        "safety_required": False,
                    }
                )
                for item in canonical.candidate_intents
            ]
        }
    )

    with pytest.raises(ValueError, match="canonical Care policy"):
        CareCoordinationSkill().plan(
            forged,
            episode_id="phase3a-care-forged",
            evidence_refs=["evidence:phase3a-escalate"],
        )


def test_care_skill_contract_cannot_claim_side_effect_execution() -> None:
    with pytest.raises(ValueError, match="side_effect_executed"):
        CareCapabilityPlan(
            evidence_refs=["evidence:phase3a"],
            side_effect_executed=True,
        )


def test_coordination_tool_projects_accepted_risk_into_same_policy() -> None:
    accepted_hash = "a" * 64
    risk = CareRiskReceiptBinding(
        risk_receipt_ref="tool:risk.classify_signal:phase3a-care",
        risk_level="escalate",
        quality_status="usable",
        source_refs=("accepted-risk:phase3a-care",),
    )

    result = CareCoordinationTool().read_policy(
        CareCoordinationPolicyRequest(
            coordination_policy_ref="coordination-policy:phase3a",
            accepted_evidence_ref=(
                f"work-product:evidence:{accepted_hash}"
            ),
            accepted_evidence_hash=accepted_hash,
            risk_decisions=(risk,),
        )
    )
    direct = CareEscalationPolicy().evaluate(
        risk_level="escalate",
        data_quality_status="good",
    )

    assert result.routing == direct
    assert result.source_refs == [
        "coordination-policy:phase3a",
        f"work-product:evidence:{accepted_hash}",
        "tool:risk.classify_signal:phase3a-care",
        "accepted-risk:phase3a-care",
    ]
    assert result.family_notification_requires_candidate is True
    assert not hasattr(result, "side_effect_executed")


def test_coordination_tool_rejects_unbound_evidence_hash() -> None:
    with pytest.raises(ValueError, match="does not bind"):
        CareCoordinationPolicyRequest(
            coordination_policy_ref="coordination-policy:phase3a",
            accepted_evidence_ref=f"work-product:evidence:{'a' * 64}",
            accepted_evidence_hash="b" * 64,
            risk_decisions=(
                CareRiskReceiptBinding(
                    risk_receipt_ref=(
                        "tool:risk.classify_signal:phase3a-care"
                    ),
                    risk_level="watch",
                    quality_status="good",
                ),
            ),
        )


def test_escalate_and_urgent_remain_candidates_and_runtime_preemption() -> None:
    escalate = CareCoordinationSkill().plan(
        CareEscalationPolicy().evaluate(
            risk_level="escalate",
            data_quality_status="good",
        ),
        episode_id="phase3a-escalate",
        evidence_refs=["evidence:phase3a-escalate"],
    )
    urgent = CareEscalationPolicy().evaluate(
        risk_level="urgent_boundary",
        data_quality_status="good",
    )

    assert escalate.primary_care_intents == []
    assert {item.action_code for item in escalate.external_action_intents} == {
        "export_doctor_material",
        "send_doctor_material",
        "create_medical_evaluation_card",
    }
    assert all(item.safety_required for item in escalate.external_action_intents)
    assert all(item.confirmation_required for item in escalate.external_action_intents)
    assert urgent.urgent_preempt is True
    assert urgent.candidate_intents[0].action_code == (
        "notify_family_delivery_record"
    )


def _context(*, risk: RiskLevel, quality: str) -> ContextPacket:
    ref = "night-summary:phase3a-care"
    summary = RadarNightSummary(
        radar_device_id="radar-phase3a",
        subject_id="elder-phase3a",
        night_of=date(2026, 7, 10),
        data_coverage_ratio=0.92,
        data_quality_status=RadarDataQualityStatus(quality),
        source_report_ref=ref,
    )
    claim = EvidenceClaim(
        claim_id="claim-phase3a-care",
        task_id="task-phase3a-care",
        text="Characterized accepted evidence.",
        evidence_refs=[ref],
        confidence=0.8,
        risk_level=risk,
        generated_by="evidence_reasoning",
        review_status=ReviewStatus.REVIEWED,
    )
    ledger = EvidenceLedger(
        ledger_id="ledger-phase3a-care",
        task_id="task-phase3a-care",
        canonical_evidence_refs=[ref],
        derived_metrics={
            "risk_level": risk.value,
            "data_quality_status": quality,
        },
        claims=[claim],
        confidence=0.8,
        review_status=ReviewStatus.REVIEWED,
    )
    return ContextPacket(
        task_context=TaskContext(
            task_id="task-phase3a-care",
            trace_id="trace-phase3a-care",
            purpose="alert",
        ),
        evidence_packet=EvidencePacket(
            night_summaries=[summary],
            evidence_ledger=ledger,
        ),
    )
