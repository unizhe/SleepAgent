# 本模块集中实现风险、照护与工作流的确定性策略。
from __future__ import annotations

from typing import Final, Literal

from pydantic import Field

from sleepagent.runtime.contracts import StrictContract


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

# 风险判定与 workflow 不变量合并于此，避免策略微模块分散。

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Literal

from sleepagent.runtime.contracts import (
    CurrentContextRisk,
    LongitudinalTrend,
    MultifactorSafetyDecision,
    MultifactorSafetyInput,
    MultiSourceConsistency,
    OnlineDataQuality,
    OnlineRiskLevel,
    RelativeBaselineDeviation,
)


RISK_POLICY_VERSION: Final = "sleepagent-risk-policy.v1"

URGENT_TERMS: tuple[str, ...] = (
    "chest pain",
    "severe difficulty breathing",
    "difficulty breathing",
    "shortness of breath",
    "cannot breathe",
    "unconscious",
    "loss of consciousness",
    "confused",
    "fell",
    "fall",
    "胸痛",
    "严重呼吸困难",
    "呼吸困难",
    "喘不上气",
    "意识不清",
    "昏迷",
    "跌倒",
    "摔倒",
)

StructuredQualityStatus = Literal["good", "partial", "unusable"]
RiskQualityStatus = Literal[
    "good",
    "partial",
    "unusable",
    "missing",
    "usable",
    "limited",
]
DeterministicRiskState = Literal[
    "unknown",
    "no_reviewed_signal",
    "no_urgent_signal",
    "operational_review",
    "reviewed_signal",
]
DeterministicDataSufficiency = Literal[
    "sufficient",
    "partial",
    "data_insufficient",
    "report_pending",
    "unknown",
]
TrendSignalLevel = Literal[
    "info",
    "normal",
    "watch",
    "escalate",
    "uncertain",
    "urgent_boundary",
]


class RiskClassificationLevel(str, Enum):
    NORMAL = "normal"
    WATCH = "watch"
    ESCALATE = "escalate"
    UNCERTAIN = "uncertain"
    URGENT_BOUNDARY = "urgent_boundary"


@dataclass(frozen=True)
class UrgentBoundaryMatch:
    matched_text: str
    matched_term: str
    evidence_ref: str


@dataclass(frozen=True)
class StructuredRiskFacts:
    summary_available: bool
    quality_status: StructuredQualityStatus = "good"
    confidence_label: str = "normal"
    health_conclusion_allowed: bool = True
    abnormal_reading_count: int = 0
    vital_fluctuation_count: int = 0
    out_of_bed_count: int = 0
    movement_count: int = 0
    trend_signal_levels: tuple[TrendSignalLevel, ...] = ()


@dataclass(frozen=True)
class DeterministicRiskFacts:
    risk_state: DeterministicRiskState
    data_sufficiency: DeterministicDataSufficiency
    health_escalation_allowed: bool = False
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskPolicyDecision:
    risk_level: RiskClassificationLevel
    reason_codes: tuple[str, ...]
    quality_blocks_escalation: bool = False
    should_stop_sleep_trend_explanation: bool = False
    safety_required: bool = False
    urgent_required: bool = False


def fuse_multifactor_safety(
    factors: MultifactorSafetyInput,
) -> MultifactorSafetyDecision:
    """在任何个性化之前应用确定性的安全下限。"""

    reasons: list[str] = []
    if factors.absolute_red_flag:
        reasons.extend(
            f"absolute_red_flag:{code}" for code in factors.absolute_red_flag_codes
        )
        return MultifactorSafetyDecision(
            risk_level=OnlineRiskLevel.ESCALATE,
            safety_required=True,
            urgent_required=factors.absolute_red_flag_requires_urgent,
            reason_codes=tuple(reasons),
            personalization_effect="explanation_only",
            source_refs=factors.source_refs,
        )
    if factors.relative_baseline_deviation is RelativeBaselineDeviation.SIGNIFICANT:
        reasons.append("relative_baseline_deviation:significant")
    if factors.current_context is CurrentContextRisk.CONCERNING:
        reasons.append("current_context:concerning")
    if factors.longitudinal_trend is LongitudinalTrend.WORSENING:
        reasons.append("longitudinal_trend:worsening")
    if factors.multi_source_consistency in {
        MultiSourceConsistency.MIXED,
        MultiSourceConsistency.CONFLICTING,
    }:
        reasons.append(
            f"multi_source_consistency:{factors.multi_source_consistency.value}"
        )
    if factors.data_quality is not OnlineDataQuality.USABLE:
        reasons.append(f"data_quality:{factors.data_quality.value}")
    escalate = factors.current_context is CurrentContextRisk.CONCERNING and (
        factors.relative_baseline_deviation is RelativeBaselineDeviation.SIGNIFICANT
        or factors.longitudinal_trend is LongitudinalTrend.WORSENING
    )
    if escalate:
        return MultifactorSafetyDecision(
            risk_level=OnlineRiskLevel.ESCALATE,
            safety_required=True,
            urgent_required=False,
            reason_codes=tuple(reasons),
            personalization_effect="noncritical_noise_reduction",
            source_refs=factors.source_refs,
        )
    watch = bool(reasons) or (
        factors.relative_baseline_deviation is RelativeBaselineDeviation.MINOR
        or factors.current_context is CurrentContextRisk.UNCERTAIN
    )
    if factors.relative_baseline_deviation is RelativeBaselineDeviation.MINOR:
        reasons.append("relative_baseline_deviation:minor")
    if factors.current_context is CurrentContextRisk.UNCERTAIN:
        reasons.append("current_context:uncertain")
    return MultifactorSafetyDecision(
        risk_level=OnlineRiskLevel.WATCH if watch else OnlineRiskLevel.NORMAL,
        safety_required=False,
        urgent_required=False,
        reason_codes=tuple(dict.fromkeys(reasons)),
        personalization_effect="noncritical_noise_reduction" if watch else "none",
        source_refs=factors.source_refs,
    )


def match_urgent_boundary(
    text_inputs: Sequence[str],
) -> UrgentBoundaryMatch | None:
    """Return the first legacy-compatible urgent text match.

    Input order has precedence over phrase order, matching the frozen boundary.
    The reference binds the complete matched input without exposing it downstream.
    """

    for text in text_inputs:
        normalized = text.lower()
        for term in URGENT_TERMS:
            if term in normalized:
                digest = sha256(text.encode("utf-8")).hexdigest()[:16]
                return UrgentBoundaryMatch(
                    matched_text=text,
                    matched_term=term,
                    evidence_ref=f"user-text:urgent-boundary:{digest}",
                )
    return None


def classify_structured_risk(facts: StructuredRiskFacts) -> RiskPolicyDecision:
    """Apply the frozen quality, watch, and escalation decision order."""

    if (
        not facts.summary_available
        or facts.quality_status == "unusable"
        or facts.confidence_label == "not_interpretable"
        or not facts.health_conclusion_allowed
    ):
        return RiskPolicyDecision(
            risk_level=RiskClassificationLevel.UNCERTAIN,
            reason_codes=("data_not_interpretable",),
            quality_blocks_escalation=True,
            should_stop_sleep_trend_explanation=True,
        )

    watch_count = sum(
        level == "watch" for level in facts.trend_signal_levels
    )
    if (
        "escalate" in facts.trend_signal_levels
        or watch_count >= 3
        or (watch_count >= 2 and facts.out_of_bed_count >= 6)
        or (watch_count >= 2 and facts.movement_count >= 25)
    ):
        return RiskPolicyDecision(
            risk_level=RiskClassificationLevel.ESCALATE,
            reason_codes=("multi_signal_escalation",),
            safety_required=True,
        )

    low_quality = (
        facts.quality_status == "partial"
        or facts.confidence_label == "low_confidence"
    )
    if low_quality and facts.abnormal_reading_count > 0:
        return RiskPolicyDecision(
            risk_level=RiskClassificationLevel.WATCH,
            reason_codes=("low_quality_vital_signal",),
        )
    if facts.vital_fluctuation_count > 0:
        return RiskPolicyDecision(
            risk_level=RiskClassificationLevel.WATCH,
            reason_codes=("vital_fluctuation_signal",),
        )
    if watch_count:
        return RiskPolicyDecision(
            risk_level=RiskClassificationLevel.WATCH,
            reason_codes=("trend_watch_signal",),
        )
    return RiskPolicyDecision(
        risk_level=RiskClassificationLevel.NORMAL,
        reason_codes=("routine_observation",),
    )


def classify_multifactor_risk(
    factors: MultifactorSafetyInput,
) -> RiskPolicyDecision:
    """Preserve the reviewed online multifactor policy behind one risk facade."""

    decision = fuse_multifactor_safety(factors)
    level = {
        OnlineRiskLevel.NORMAL: RiskClassificationLevel.NORMAL,
        OnlineRiskLevel.WATCH: RiskClassificationLevel.WATCH,
        OnlineRiskLevel.ESCALATE: RiskClassificationLevel.ESCALATE,
    }[decision.risk_level]
    return RiskPolicyDecision(
        risk_level=level,
        reason_codes=decision.reason_codes,
        safety_required=decision.safety_required,
        urgent_required=decision.urgent_required,
        should_stop_sleep_trend_explanation=decision.urgent_required,
    )


def classify_deterministic_risk(
    facts: DeterministicRiskFacts,
) -> RiskPolicyDecision:
    """Project an exact-revision CurrentRisk without scalar approximation."""

    reasons = facts.reason_codes or (f"risk_state:{facts.risk_state}",)
    if facts.data_sufficiency != "sufficient" or facts.risk_state == "unknown":
        return RiskPolicyDecision(
            risk_level=RiskClassificationLevel.UNCERTAIN,
            reason_codes=reasons,
            quality_blocks_escalation=True,
            should_stop_sleep_trend_explanation=True,
        )
    if facts.risk_state == "reviewed_signal":
        if facts.health_escalation_allowed:
            return RiskPolicyDecision(
                risk_level=RiskClassificationLevel.ESCALATE,
                reason_codes=reasons,
                safety_required=True,
            )
        return RiskPolicyDecision(
            risk_level=RiskClassificationLevel.WATCH,
            reason_codes=reasons,
        )
    if facts.risk_state == "operational_review":
        return RiskPolicyDecision(
            risk_level=RiskClassificationLevel.WATCH,
            reason_codes=reasons,
        )
    return RiskPolicyDecision(
        risk_level=RiskClassificationLevel.NORMAL,
        reason_codes=reasons,
    )


__all__ = [
    "RISK_POLICY_VERSION",
    "URGENT_TERMS",
    "RiskClassificationLevel",
    "DeterministicDataSufficiency",
    "DeterministicRiskFacts",
    "DeterministicRiskState",
    "RiskPolicyDecision",
    "RiskQualityStatus",
    "StructuredQualityStatus",
    "StructuredRiskFacts",
    "TrendSignalLevel",
    "UrgentBoundaryMatch",
    "classify_multifactor_risk",
    "classify_deterministic_risk",
    "classify_structured_risk",
    "fuse_multifactor_safety",
    "match_urgent_boundary",
]


from dataclasses import dataclass
from enum import Enum


CANONICAL_WORKFLOW_POLICY_VERSION = "sleepagent-canonical-workflow-policy.v1"


class CanonicalWorkflowInvariant(str, Enum):
    """Deterministic ordering and authority rules for Product Episodes."""

    SAFETY_PREEMPTS_MODEL = "safety_preempts_model"
    FACTS_BEFORE_EVIDENCE = "facts_before_evidence"
    ACCEPTED_EVIDENCE_BEFORE_CARE = "accepted_evidence_before_care"
    SAFETY_BEFORE_RESTRICTED_PUBLICATION = (
        "safety_before_restricted_publication"
    )
    CONFIRMATION_BEFORE_STATE_CHANGE = "confirmation_before_state_change"
    SINGLE_WRITER_COMMIT = "single_writer_commit"
    FAIL_CLOSED_ON_REQUIRED_TOOL_ERROR = "fail_closed_on_required_tool_error"
    CHECKPOINT_REVALIDATES_BINDING = "checkpoint_revalidates_binding"
    CONTEXT_IS_ROLE_SCOPED = "context_is_role_scoped"
    EXACT_TARGET_CONFLICT_REJECTION = "exact_target_conflict_rejection"


@dataclass(frozen=True, slots=True)
class CanonicalWorkflowPolicy:
    """Immutable policy mounted by the canonical Product Episode Runtime.

    The Runtime and governance layer enforce these invariants.  This policy is
    intentionally not a workflow-node roster and carries no Agent/A2A payloads.
    """

    version: str = CANONICAL_WORKFLOW_POLICY_VERSION
    invariants: tuple[CanonicalWorkflowInvariant, ...] = tuple(
        CanonicalWorkflowInvariant
    )


CANONICAL_WORKFLOW_POLICY = CanonicalWorkflowPolicy()


__all__ = [
    "CANONICAL_WORKFLOW_POLICY",
    "CANONICAL_WORKFLOW_POLICY_VERSION",
    "CanonicalWorkflowInvariant",
    "CanonicalWorkflowPolicy",
]
