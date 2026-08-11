from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Literal

from sleepagent.product_runtime.contracts import (
    MultifactorSafetyInput,
    OnlineRiskLevel,
)
from sleepagent.product_runtime.online_reasoning import (
    fuse_multifactor_safety,
)


RISK_POLICY_VERSION = "sleepagent-risk-policy.v1"

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
    "match_urgent_boundary",
]
