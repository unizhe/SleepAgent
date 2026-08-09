from __future__ import annotations

from typing import Literal

from pydantic import Field

from sleepagent.radar_agent.product_agent.contracts import StrictContract


CARE_ESCALATION_POLICY_VERSION = "sleepagent-care-escalation-policy.v1"
RiskLevelValue = Literal[
    "info", "watch", "uncertain", "escalate", "urgent_boundary"
]


class CareCapabilityIntent(StrictContract):
    kind: Literal[
        "primary_care",
        "questionnaire",
        "coordination",
        "artifact",
        "external_action",
        "delivery_record",
    ]
    action_code: str = Field(..., min_length=1)
    confirmation_required: bool = True
    safety_required: bool = False


class CareRoutingDecision(StrictContract):
    policy_version: str = CARE_ESCALATION_POLICY_VERSION
    risk_level: RiskLevelValue
    data_quality_status: str = Field(..., min_length=1)
    candidate_intents: list[CareCapabilityIntent] = Field(default_factory=list)
    automatic_communication_codes: tuple[str, ...] = ()
    urgent_preempt: bool = False


class CareEscalationPolicy:
    """Split a risk band into typed capabilities without executing any of them."""

    version = CARE_ESCALATION_POLICY_VERSION

    def evaluate(
        self,
        *,
        risk_level: RiskLevelValue,
        data_quality_status: str,
    ) -> CareRoutingDecision:
        intents: list[CareCapabilityIntent] = []
        if risk_level == "watch":
            intents = [
                CareCapabilityIntent(
                    kind="coordination",
                    action_code="enable_persistent_family_reminder",
                ),
                CareCapabilityIntent(
                    kind="questionnaire",
                    action_code="push_supplemental_questionnaire",
                ),
                CareCapabilityIntent(
                    kind="primary_care",
                    action_code="enable_care_plan",
                ),
            ]
        elif risk_level == "escalate":
            intents = [
                CareCapabilityIntent(
                    kind="artifact",
                    action_code="export_doctor_material",
                    safety_required=True,
                ),
                CareCapabilityIntent(
                    kind="external_action",
                    action_code="send_doctor_material",
                    safety_required=True,
                ),
                CareCapabilityIntent(
                    kind="artifact",
                    action_code="create_medical_evaluation_card",
                    safety_required=True,
                ),
            ]
        elif risk_level == "urgent_boundary":
            intents = [
                CareCapabilityIntent(
                    kind="delivery_record",
                    action_code="notify_family_delivery_record",
                    safety_required=True,
                )
            ]

        automatic = [
            "publish_daily_elder_report",
            "publish_daily_family_report",
        ]
        if risk_level == "info":
            automatic.append("publish_info_notice")
        if data_quality_status != "good":
            automatic.append("publish_data_quality_notice")
        if risk_level == "urgent_boundary":
            automatic.append("show_urgent_safety_notice")
        return CareRoutingDecision(
            risk_level=risk_level,
            data_quality_status=data_quality_status,
            candidate_intents=intents,
            automatic_communication_codes=tuple(automatic),
            urgent_preempt=risk_level == "urgent_boundary",
        )


__all__ = [
    "CARE_ESCALATION_POLICY_VERSION",
    "CareCapabilityIntent",
    "CareEscalationPolicy",
    "CareRoutingDecision",
    "RiskLevelValue",
]
