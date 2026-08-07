from __future__ import annotations

from enum import Enum
from hashlib import sha256
from pydantic import Field

from sleepagent.radar_agent.confirmation import confirmation_request
from sleepagent.radar_agent.schemas import (
    AgentResult,
    ContextPacket,
    EvidenceClaim,
    HumanConfirmationRequest,
    QuestionnaireEntry,
    RadarAgentName,
    RadarAgentSchema,
    RadarDataQualityStatus,
    RadarNightSummary,
    ReviewStatus,
    RiskLevel,
)


class RiskSignalReason(str, Enum):
    ROUTINE_OBSERVATION = "routine_observation"
    TREND_WATCH_SIGNAL = "trend_watch_signal"
    MULTI_SIGNAL_ESCALATION = "multi_signal_escalation"
    DATA_NOT_INTERPRETABLE = "data_not_interpretable"
    LOW_QUALITY_VITAL_SIGNAL = "low_quality_vital_signal"
    VITAL_FLUCTUATION_SIGNAL = "vital_fluctuation_signal"
    URGENT_TEXT_BOUNDARY = "urgent_text_boundary"


class RiskSignalDecision(RadarAgentSchema):
    task_id: str = Field(..., min_length=1)
    risk_level: RiskLevel
    reasons: list[RiskSignalReason] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    confirmation_requests: list[HumanConfirmationRequest] = Field(default_factory=list)
    candidate_actions: list[str] = Field(default_factory=list)
    safety_flags: list[str] = Field(default_factory=list)
    should_stop_sleep_trend_explanation: bool = False
    uncertainty: str | None = None
    caveats: list[str] = Field(default_factory=list)


class RiskSignalAgent:
    name = RadarAgentName.RISK_SIGNAL

    def run(self, context: ContextPacket) -> AgentResult:
        text_inputs = _text_inputs_from_context(context)
        trend_claims = _trend_claims_from_context(context)
        decision = self.analyze(
            task_id=context.task_context.task_id,
            night_summary=_latest_summary(context.evidence_packet.night_summaries),
            trend_claims=trend_claims,
            text_inputs=text_inputs,
        )
        return AgentResult(
            agent_name=self.name,
            claims=decision.claims,
            evidence_refs=decision.evidence_refs,
            confidence=min((claim.confidence for claim in decision.claims), default=0),
            uncertainties=[decision.uncertainty] if decision.uncertainty else [],
            safety_flags=decision.safety_flags,
            candidate_actions=decision.candidate_actions,
            output_payload={"risk_signal": decision.model_dump(mode="json")},
        )

    def analyze(
        self,
        *,
        task_id: str,
        night_summary: RadarNightSummary | None,
        trend_claims: list[EvidenceClaim] | None = None,
        text_inputs: list[str] | None = None,
    ) -> RiskSignalDecision:
        trend_claims = list(trend_claims or [])
        text_inputs = list(text_inputs or [])
        urgent = _urgent_text_match(text_inputs)
        if urgent is not None:
            return self._urgent_decision(task_id=task_id, urgent_text=urgent)

        evidence_refs = _dedupe(
            [_summary_ref(night_summary)] if night_summary is not None else []
        )
        evidence_refs.extend(
            ref for claim in trend_claims for ref in claim.evidence_refs
        )
        evidence_refs = _dedupe(evidence_refs)

        if _not_interpretable(night_summary):
            return self._quality_blocked_decision(
                task_id=task_id,
                night_summary=night_summary,
                evidence_refs=evidence_refs,
            )

        watch_claims = [
            claim for claim in trend_claims if claim.risk_level == RiskLevel.WATCH
        ]
        escalate_claims = [
            claim for claim in trend_claims if claim.risk_level == RiskLevel.ESCALATE
        ]
        if _should_escalate(night_summary, watch_claims, escalate_claims):
            return self._escalate_decision(
                task_id=task_id,
                evidence_refs=evidence_refs,
                trend_claims=watch_claims + escalate_claims,
            )

        if _low_quality_vital_signal(night_summary):
            return self._watch_decision(
                task_id=task_id,
                evidence_refs=evidence_refs,
                reasons=[RiskSignalReason.LOW_QUALITY_VITAL_SIGNAL],
                uncertainty="vital_signal_quality_is_low",
                confidence=0.45,
            )

        if _vital_fluctuation_signal(night_summary):
            return self._watch_decision(
                task_id=task_id,
                evidence_refs=evidence_refs,
                reasons=[RiskSignalReason.VITAL_FLUCTUATION_SIGNAL],
                uncertainty="radar_vital_fluctuation_is_not_diagnostic",
                confidence=0.62,
            )

        if watch_claims:
            return self._watch_decision(
                task_id=task_id,
                evidence_refs=evidence_refs,
                reasons=[RiskSignalReason.TREND_WATCH_SIGNAL],
                uncertainty=None,
                confidence=_confidence_from_claims(watch_claims, default=0.65),
            )

        return self._info_decision(
            task_id=task_id,
            evidence_refs=evidence_refs,
        )

    def _info_decision(
        self,
        *,
        task_id: str,
        evidence_refs: list[str],
    ) -> RiskSignalDecision:
        claims = (
            [
                _claim(
                    task_id=task_id,
                    suffix="info",
                    text=(
                        "Risk signal remains at info level based on available "
                        "radar care metrics."
                    ),
                    evidence_refs=evidence_refs,
                    risk_level=RiskLevel.INFO,
                    confidence=0.6,
                )
            ]
            if evidence_refs
            else []
        )
        return RiskSignalDecision(
            task_id=task_id,
            risk_level=RiskLevel.INFO,
            reasons=[RiskSignalReason.ROUTINE_OBSERVATION],
            evidence_refs=evidence_refs,
            claims=claims,
            candidate_actions=["publish_routine_care_summary"],
            safety_flags=["risk_rules_only"],
            caveats=_risk_caveats(),
        )

    def _watch_decision(
        self,
        *,
        task_id: str,
        evidence_refs: list[str],
        reasons: list[RiskSignalReason],
        uncertainty: str | None,
        confidence: float,
    ) -> RiskSignalDecision:
        claim = _claim(
            task_id=task_id,
            suffix="watch",
            text="Risk signal is watch level and should be followed with additional observation.",
            evidence_refs=evidence_refs,
            risk_level=RiskLevel.WATCH,
            confidence=confidence,
            uncertainty=uncertainty,
        )
        return RiskSignalDecision(
            task_id=task_id,
            risk_level=RiskLevel.WATCH,
            reasons=reasons,
            evidence_refs=evidence_refs,
            claims=[claim] if evidence_refs else [],
            candidate_actions=[
                "suggest_micro_questionnaire",
                "continue_family_observation",
            ],
            safety_flags=["risk_rules_only"],
            uncertainty=uncertainty,
            caveats=_risk_caveats(),
        )

    def _escalate_decision(
        self,
        *,
        task_id: str,
        evidence_refs: list[str],
        trend_claims: list[EvidenceClaim],
    ) -> RiskSignalDecision:
        claim = _claim(
            task_id=task_id,
            suffix="escalate",
            text="Risk signal is escalate level because multiple care signals are elevated.",
            evidence_refs=evidence_refs,
            risk_level=RiskLevel.ESCALATE,
            confidence=_confidence_from_claims(trend_claims, default=0.72),
        )
        confirmation = confirmation_request(
            task_id=task_id,
            action_type="export_doctor_material",
            reason="Escalate-level care material export requires confirmation.",
            evidence_refs=evidence_refs,
        )
        return RiskSignalDecision(
            task_id=task_id,
            risk_level=RiskLevel.ESCALATE,
            reasons=[RiskSignalReason.MULTI_SIGNAL_ESCALATION],
            evidence_refs=evidence_refs,
            claims=[claim],
            confirmation_requests=[confirmation],
            candidate_actions=[
                "prepare_doctor_review_material",
                "ask_family_to_confirm_export",
            ],
            safety_flags=["risk_rules_only", "confirmation_required_before_external_share"],
            caveats=_risk_caveats(),
        )

    def _quality_blocked_decision(
        self,
        *,
        task_id: str,
        night_summary: RadarNightSummary | None,
        evidence_refs: list[str],
    ) -> RiskSignalDecision:
        claims = (
            [
                _claim(
                    task_id=task_id,
                    suffix="data-not-interpretable",
                    text=(
                        "Radar data is not interpretable enough to assign a "
                        "higher sleep risk signal."
                    ),
                    evidence_refs=evidence_refs,
                    risk_level=RiskLevel.UNCERTAIN,
                    confidence=0.35,
                    uncertainty="data_not_interpretable",
                )
            ]
            if evidence_refs
            else []
        )
        return RiskSignalDecision(
            task_id=task_id,
            risk_level=RiskLevel.UNCERTAIN,
            reasons=[RiskSignalReason.DATA_NOT_INTERPRETABLE],
            evidence_refs=evidence_refs,
            claims=claims,
            candidate_actions=["show_data_collection_guidance"],
            safety_flags=["risk_rules_only", "quality_gate_blocks_escalation"],
            should_stop_sleep_trend_explanation=True,
            uncertainty="data_not_interpretable",
            caveats=_risk_caveats()
            + list(night_summary.caveats if night_summary is not None else []),
        )

    def _urgent_decision(
        self,
        *,
        task_id: str,
        urgent_text: str,
    ) -> RiskSignalDecision:
        digest = sha256(urgent_text.encode("utf-8")).hexdigest()[:16]
        evidence_ref = f"user-text:urgent-boundary:{digest}"
        claim = _claim(
            task_id=task_id,
            suffix="urgent-boundary",
            text="Urgent boundary text requires immediate offline medical or emergency evaluation.",
            evidence_refs=[evidence_ref],
            risk_level=RiskLevel.URGENT_BOUNDARY,
            confidence=1.0,
            uncertainty=None,
        )
        confirmation = confirmation_request(
            task_id=task_id,
            action_type="notify_family_delivery_record",
            reason="Urgent boundary notification delivery must be recorded.",
            evidence_refs=[evidence_ref],
        )
        return RiskSignalDecision(
            task_id=task_id,
            risk_level=RiskLevel.URGENT_BOUNDARY,
            reasons=[RiskSignalReason.URGENT_TEXT_BOUNDARY],
            evidence_refs=[evidence_ref],
            claims=[claim],
            confirmation_requests=[confirmation],
            candidate_actions=[
                "stop_sleep_trend_explanation",
                "show_offline_medical_or_emergency_evaluation",
                "record_family_notification_delivery",
            ],
            safety_flags=["risk_rules_only", "urgent_boundary_overrides_sleep_analysis"],
            should_stop_sleep_trend_explanation=True,
            caveats=[
                "Urgent symptoms override sleep trend interpretation.",
                "Use local offline medical or emergency services when symptoms are severe.",
            ],
        )


def _text_inputs_from_context(context: ContextPacket) -> list[str]:
    values: list[str] = []
    for entry in context.evidence_packet.questionnaire_entries:
        values.extend(_entry_texts(entry))
    raw_values = context.evidence_packet.data_quality.get("text_inputs", [])
    if isinstance(raw_values, str):
        values.append(raw_values)
    elif isinstance(raw_values, list):
        values.extend(str(item) for item in raw_values if item is not None)
    return values


def _trend_claims_from_context(context: ContextPacket) -> list[EvidenceClaim]:
    values = context.evidence_packet.data_quality.get("trend_claims", [])
    if not isinstance(values, list):
        return []
    claims: list[EvidenceClaim] = []
    for item in values:
        if isinstance(item, EvidenceClaim):
            claims.append(item)
        elif isinstance(item, dict):
            claims.append(EvidenceClaim.model_validate(item))
    return claims


def _entry_texts(entry: QuestionnaireEntry) -> list[str]:
    return [entry.answer]


def _latest_summary(
    summaries: list[RadarNightSummary],
) -> RadarNightSummary | None:
    if not summaries:
        return None
    return max(summaries, key=lambda item: item.night_of)


def _not_interpretable(summary: RadarNightSummary | None) -> bool:
    if summary is None:
        return True
    return (
        summary.data_quality_status == RadarDataQualityStatus.UNUSABLE
        or summary.confidence_label == "not_interpretable"
        or not summary.health_conclusion_allowed
    )


def _low_quality_vital_signal(summary: RadarNightSummary | None) -> bool:
    if summary is None:
        return False
    low_quality = (
        summary.data_quality_status == RadarDataQualityStatus.PARTIAL
        or summary.confidence_label == "low_confidence"
    )
    return low_quality and summary.abnormal_reading_count > 0


def _vital_fluctuation_signal(summary: RadarNightSummary | None) -> bool:
    if summary is None or not summary.health_conclusion_allowed:
        return False
    return bool(summary.explainable_metrics.get("vital_fluctuation_count", 0))


def _should_escalate(
    summary: RadarNightSummary | None,
    watch_claims: list[EvidenceClaim],
    escalate_claims: list[EvidenceClaim],
) -> bool:
    if escalate_claims:
        return True
    if len(watch_claims) >= 3:
        return True
    if summary is None:
        return False
    if len(watch_claims) >= 2 and summary.out_of_bed_count >= 6:
        return True
    if len(watch_claims) >= 2 and summary.movement_count >= 25:
        return True
    return False


def _urgent_text_match(text_inputs: list[str]) -> str | None:
    for text in text_inputs:
        normalized = text.lower()
        if any(term in normalized for term in _URGENT_TERMS):
            return text
    return None


def urgent_text_match(text_inputs: list[str]) -> str | None:
    """Public deterministic preflight boundary; returns only the matched input."""

    return _urgent_text_match(text_inputs)


_URGENT_TERMS = (
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


def _claim(
    *,
    task_id: str,
    suffix: str,
    text: str,
    evidence_refs: list[str],
    risk_level: RiskLevel,
    confidence: float,
    uncertainty: str | None = None,
) -> EvidenceClaim:
    return EvidenceClaim(
        claim_id=f"risk:{task_id}:{suffix}",
        task_id=task_id,
        text=text,
        evidence_refs=evidence_refs,
        confidence=confidence,
        risk_level=risk_level,
        uncertainty=uncertainty,
        caveats=_risk_caveats(),
        generated_by=RadarAgentName.RISK_SIGNAL.value,
        review_status=ReviewStatus.REVIEWED,
    )


def _summary_ref(summary: RadarNightSummary | None) -> str:
    if summary is None:
        return "night-summary:missing"
    if summary.source_report_ref:
        return summary.source_report_ref
    return f"night-summary:{summary.radar_device_id}:{summary.night_of.isoformat()}"


def _confidence_from_claims(
    claims: list[EvidenceClaim],
    *,
    default: float,
) -> float:
    if not claims:
        return default
    return round(min(claim.confidence for claim in claims), 2)


def _dedupe(items) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item in seen:
            continue
        deduped.append(item)
        seen.add(item)
    return deduped


def _risk_caveats() -> list[str]:
    return [
        "Risk levels are rule-based care signals.",
        "Radar signals do not replace clinician assessment.",
    ]


__all__ = [
    "RiskSignalAgent",
    "RiskSignalDecision",
    "RiskSignalReason",
]
