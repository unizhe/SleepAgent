from __future__ import annotations

from pydantic import Field

from sleepagent.radar_agent.confirmation import (
    automatic_actions,
    confirmation_request,
)
from sleepagent.radar_agent.schemas import (
    AgentResult,
    ContextPacket,
    EvidenceLedger,
    HumanConfirmationRequest,
    RadarAgentName,
    RadarAgentSchema,
    RiskLevel,
)


class AlertCareDecision(RadarAgentSchema):
    task_id: str
    risk_level: RiskLevel
    candidate_actions: list[str] = Field(default_factory=list)
    automatic_actions: list[str] = Field(default_factory=list)
    confirmation_requests: list[HumanConfirmationRequest] = Field(default_factory=list)
    external_action_executed: bool = False
    urgent_safety_notice_displayed: bool = False


class AlertCareAgent:
    name = RadarAgentName.ALERT_CARE

    def run(self, context: ContextPacket) -> AgentResult:
        ledger = _require_ledger(context)
        risk = RiskLevel(str(ledger.derived_metrics.get("risk_level", RiskLevel.INFO.value)))
        refs = _ledger_refs(ledger)
        actions, confirmations = _decision(context.task_context.task_id, risk, refs)
        auto = automatic_actions(
            risk_level=risk.value,
            data_quality_status=str(
                ledger.derived_metrics.get("data_quality_status", "unknown")
            ),
        )
        decision = AlertCareDecision(
            task_id=context.task_context.task_id,
            risk_level=risk,
            candidate_actions=actions,
            automatic_actions=auto,
            confirmation_requests=confirmations,
            urgent_safety_notice_displayed=(risk == RiskLevel.URGENT_BOUNDARY),
        )
        return AgentResult(
            agent_name=self.name,
            evidence_refs=refs,
            confidence=ledger.confidence,
            uncertainties=[ledger.uncertainty] if ledger.uncertainty else [],
            safety_flags=["candidate_only", "no_external_side_effect", "confirmation_matrix_applied"],
            candidate_actions=[*auto, *actions],
            output_payload={"alert_care": decision.model_dump(mode="json")},
        )


def _decision(task_id: str, risk: RiskLevel, refs: list[str]):
    if risk == RiskLevel.INFO:
        return [], []
    if risk == RiskLevel.WATCH:
        actions = [
            "enable_persistent_family_reminder",
            "push_supplemental_questionnaire",
            "enable_care_plan",
        ]
        return actions, [
            confirmation_request(task_id=task_id, action_type=action, evidence_refs=refs)
            for action in actions
        ]
    if risk == RiskLevel.UNCERTAIN:
        return [], []
    if risk == RiskLevel.ESCALATE:
        actions = [
            "export_doctor_material",
            "send_doctor_material",
            "create_medical_evaluation_card",
        ]
        return actions, [
            confirmation_request(task_id=task_id, action_type=action, evidence_refs=refs)
            for action in actions
        ]
    action = "notify_family_delivery_record"
    return [action], [
        confirmation_request(task_id=task_id, action_type=action, evidence_refs=refs)
    ]


def _require_ledger(context: ContextPacket) -> EvidenceLedger:
    if context.evidence_packet.evidence_ledger is None:
        raise ValueError("AlertCareAgent requires an EvidenceLedger.")
    return context.evidence_packet.evidence_ledger


def _ledger_refs(ledger: EvidenceLedger) -> list[str]:
    return list(dict.fromkeys(ledger.canonical_evidence_refs + [ref for claim in ledger.claims for ref in claim.evidence_refs]))


__all__ = ["AlertCareAgent", "AlertCareDecision"]
