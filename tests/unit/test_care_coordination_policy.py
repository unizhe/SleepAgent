from __future__ import annotations

import pytest

from sleepagent.runtime.policies import (
    CareEscalationPolicy,
)
from sleepagent.runtime.tools import (
    CareCoordinationPolicyRequest,
    CareCoordinationTool,
    CareRiskReceiptBinding,
)
from sleepagent.runtime.schemas import (
    RiskLevel,
)
from tests.support.golden_fixtures import load_product_capability_goldens


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
    expected = load_product_capability_goldens()["care_coordination"][
        f"{risk.value}:{quality}"
    ]

    migrated = CareEscalationPolicy().evaluate(
        risk_level=risk.value,
        data_quality_status=quality,
    )

    assert [item.action_code for item in migrated.candidate_intents] == expected[
        "candidate_actions"
    ]
    assert list(migrated.automatic_communication_codes) == expected[
        "automatic_actions"
    ]
    assert migrated.urgent_preempt == expected["urgent_preempt"]
    assert not hasattr(migrated, "agent_name")
    assert not hasattr(migrated, "external_action_executed")


def test_watch_policy_splits_primary_questionnaire_and_coordination_intents() -> None:
    decision = CareEscalationPolicy().evaluate(
        risk_level="watch",
        data_quality_status="good",
    )

    assert sum(
        item.kind == "primary_care" for item in decision.candidate_intents
    ) == 1
    assert sum(
        item.kind == "questionnaire" for item in decision.candidate_intents
    ) == 1
    assert sum(
        item.kind == "coordination" for item in decision.candidate_intents
    ) == 1
    assert not any(
        item.kind in {"artifact", "external_action"}
        for item in decision.candidate_intents
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
    escalate = CareEscalationPolicy().evaluate(
        risk_level="escalate",
        data_quality_status="good",
    )
    urgent = CareEscalationPolicy().evaluate(
        risk_level="urgent_boundary",
        data_quality_status="good",
    )
    external = [
        item
        for item in escalate.candidate_intents
        if item.kind in {"artifact", "external_action"}
    ]

    assert not any(
        item.kind == "primary_care" for item in escalate.candidate_intents
    )
    assert {item.action_code for item in external} == {
        "export_doctor_material",
        "send_doctor_material",
        "create_medical_evaluation_card",
    }
    assert all(item.safety_required for item in external)
    assert all(item.confirmation_required for item in external)
    assert urgent.urgent_preempt is True
    assert urgent.candidate_intents[0].action_code == (
        "notify_family_delivery_record"
    )
