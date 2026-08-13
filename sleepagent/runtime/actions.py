# 本模块负责四智能体运行时的一项明确能力，不提供旧运行路径兼容层。
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Role = Literal["elder", "family", "doctor", "system"]


@dataclass(frozen=True)
class ConfirmationRule:
    action_type: str
    requires_confirmation: bool
    allowed_roles: tuple[Role, ...]
    requested_role: Role
    blocks_daily_flow: bool = False
    confirmation_kind: Literal[
        "approval", "delivery_record", "doctor_annotation"
    ] = "approval"
    auto_publish: bool = False


_RULES = {
    rule.action_type: rule
    for rule in (
        ConfirmationRule(
            "publish_daily_elder_report", False, ("system",), "system", auto_publish=True
        ),
        ConfirmationRule(
            "publish_daily_family_report", False, ("system",), "system", auto_publish=True
        ),
        ConfirmationRule(
            "publish_info_notice", False, ("system",), "system", auto_publish=True
        ),
        ConfirmationRule(
            "publish_data_quality_notice", False, ("system",), "system", auto_publish=True
        ),
        ConfirmationRule(
            "show_urgent_safety_notice", False, ("system",), "system", auto_publish=True
        ),
        ConfirmationRule(
            "enable_persistent_family_reminder", True, ("family",), "family"
        ),
        ConfirmationRule(
            "push_supplemental_questionnaire", True, ("family",), "family"
        ),
        ConfirmationRule("enable_care_plan", True, ("family",), "family"),
        ConfirmationRule("write_long_term_memory", True, ("family",), "family"),
        ConfirmationRule(
            "export_doctor_material", True, ("elder", "family"), "family"
        ),
        ConfirmationRule(
            "send_doctor_material", True, ("elder", "family"), "family"
        ),
        ConfirmationRule(
            "create_medical_evaluation_card", True, ("elder", "family"), "family"
        ),
        ConfirmationRule(
            "notify_family_delivery_record",
            True,
            ("family", "system"),
            "system",
            confirmation_kind="delivery_record",
        ),
        ConfirmationRule(
            "doctor_annotation",
            True,
            ("doctor",),
            "doctor",
            confirmation_kind="doctor_annotation",
        ),
        ConfirmationRule(
            "doctor_followup_recommendation",
            True,
            ("doctor",),
            "doctor",
            confirmation_kind="doctor_annotation",
        ),
    )
}

_ALIASES = {
    "export_doctor_report": "export_doctor_material",
    "schedule_evaluation_card": "create_medical_evaluation_card",
    "medical_evaluation_card": "create_medical_evaluation_card",
    "record_family_notification_delivery": "notify_family_delivery_record",
    "send_supplemental_questionnaire": "push_supplemental_questionnaire",
}


def canonical_action(action_type: str) -> str:
    return _ALIASES.get(action_type, action_type)


def confirmation_rule(action_type: str) -> ConfirmationRule | None:
    return _RULES.get(canonical_action(action_type))


def automatic_actions(*, risk_level: str, data_quality_status: str) -> list[str]:
    actions = ["publish_daily_elder_report", "publish_daily_family_report"]
    if risk_level == "info":
        actions.append("publish_info_notice")
    if data_quality_status != "good":
        actions.append("publish_data_quality_notice")
    if risk_level == "urgent_boundary":
        actions.append("show_urgent_safety_notice")
    return actions


def matrix_rules() -> dict[str, ConfirmationRule]:
    return dict(_RULES)


__all__ = [
    "ConfirmationRule",
    "automatic_actions",
    "canonical_action",
    "confirmation_rule",
    "matrix_rules",
]
