from __future__ import annotations

from dataclasses import dataclass

from sleepagent.radar_agent.product_agent.contracts import (
    CurrentContextRisk,
    EventContextResolution,
    LongitudinalTrend,
    MultifactorSafetyDecision,
    MultifactorSafetyInput,
    MultiSourceConsistency,
    OnlineDataQuality,
    OnlineEventType,
    OnlineReasoningEvent,
    OnlineRiskLevel,
    RelativeBaselineDeviation,
)


ONLINE_REASONING_POLICY_VERSION = "sleepagent-online-reasoning.v1"


@dataclass(frozen=True)
class EventContextRule:
    evidence_gap_code: str
    required_concept_ids: tuple[str, ...]
    care_delivery_concept_ids: tuple[str, ...]
    baseline_metric_ids: tuple[str, ...]
    required_current_signal_keys: tuple[str, ...]
    required_quality_ref_kinds: tuple[str, ...]
    required_trend_ref_kinds: tuple[str, ...]
    required_clinical_ref_kinds: tuple[str, ...]


EVENT_CONTEXT_RULES: dict[OnlineEventType, EventContextRule] = {
    OnlineEventType.NIGHT_OUT_OF_BED: EventContextRule(
        evidence_gap_code="E-NIGHT-OBSERVATION",
        required_concept_ids=(
            "habit.night_toileting_pattern",
            "habit.night_out_of_bed_frequency",
            "habit.night_out_of_bed_time_window",
            "habit.night_out_of_bed_duration_minutes",
            "habit.night_activity_assistance_need",
            "habit.observed_night_leaving",
        ),
        care_delivery_concept_ids=(
            "habit.delivery_timing_preference",
            "habit.delivery_modality_preference",
            "habit.interruption_burden",
            "habit.family_notification_preference",
            "habit.quiet_hours",
            "habit.voice_volume_preference",
            "habit.night_activity_assistance_need",
        ),
        baseline_metric_ids=("baseline.night_out_of_bed",),
        required_current_signal_keys=(
            "out_of_bed_started_at",
            "out_of_bed_duration_minutes",
            "respiratory_rate_per_minute",
            "heart_rate_bpm",
            "gait_risk",
        ),
        required_quality_ref_kinds=("radar_data_quality", "device_status"),
        required_trend_ref_kinds=(
            "night_out_of_bed_recent_change",
            "multi_night_vital_change",
        ),
        required_clinical_ref_kinds=(
            "medication_context",
            "mobility_fall_context",
        ),
    )
}


def resolve_event_context(event: OnlineReasoningEvent) -> EventContextResolution:
    """Resolve online evidence needs without caller-selected concept IDs."""

    rule = EVENT_CONTEXT_RULES[event.event_type]
    return EventContextResolution(
        event_id=event.event_id,
        event_type=event.event_type,
        evidence_gap_code=rule.evidence_gap_code,
        required_concept_ids=rule.required_concept_ids,
        care_delivery_concept_ids=rule.care_delivery_concept_ids,
        baseline_metric_ids=rule.baseline_metric_ids,
        required_current_signal_keys=rule.required_current_signal_keys,
        current_signals=event.current_signals,
        current_signal_refs=event.current_signal_refs,
        missing_current_signal_keys=tuple(
            key
            for key in rule.required_current_signal_keys
            if key not in event.current_signals
        ),
        required_quality_ref_kinds=rule.required_quality_ref_kinds,
        quality_refs=event.quality_refs,
        required_trend_ref_kinds=rule.required_trend_ref_kinds,
        trend_refs=event.trend_refs,
        required_clinical_ref_kinds=rule.required_clinical_ref_kinds,
        clinical_context_refs=event.clinical_context_refs,
        source_refs=tuple(
            dict.fromkeys(
                (
                    *event.current_signal_refs,
                    *event.quality_refs,
                    *event.trend_refs,
                    *event.clinical_context_refs,
                    *event.safety_factors.source_refs,
                )
            )
        ),
    )


def fuse_multifactor_safety(
    factors: MultifactorSafetyInput,
) -> MultifactorSafetyDecision:
    """Apply deterministic floors before habit-based personalization."""

    reasons: list[str] = []
    if factors.absolute_red_flag:
        reasons.extend(
            f"absolute_red_flag:{code}"
            for code in factors.absolute_red_flag_codes
        )
        return MultifactorSafetyDecision(
            risk_level=OnlineRiskLevel.ESCALATE,
            safety_required=True,
            urgent_required=factors.absolute_red_flag_requires_urgent,
            reason_codes=tuple(reasons),
            personalization_effect="explanation_only",
            source_refs=factors.source_refs,
        )

    if (
        factors.relative_baseline_deviation
        == RelativeBaselineDeviation.SIGNIFICANT
    ):
        reasons.append("relative_baseline_deviation:significant")
    if factors.current_context == CurrentContextRisk.CONCERNING:
        reasons.append("current_context:concerning")
    if factors.longitudinal_trend == LongitudinalTrend.WORSENING:
        reasons.append("longitudinal_trend:worsening")
    if factors.multi_source_consistency in {
        MultiSourceConsistency.MIXED,
        MultiSourceConsistency.CONFLICTING,
    }:
        reasons.append(
            f"multi_source_consistency:{factors.multi_source_consistency.value}"
        )
    if factors.data_quality != OnlineDataQuality.USABLE:
        reasons.append(f"data_quality:{factors.data_quality.value}")

    escalate = (
        factors.current_context == CurrentContextRisk.CONCERNING
        and (
            factors.relative_baseline_deviation
            == RelativeBaselineDeviation.SIGNIFICANT
            or factors.longitudinal_trend == LongitudinalTrend.WORSENING
        )
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
        factors.relative_baseline_deviation == RelativeBaselineDeviation.MINOR
        or factors.current_context == CurrentContextRisk.UNCERTAIN
    )
    if factors.relative_baseline_deviation == RelativeBaselineDeviation.MINOR:
        reasons.append("relative_baseline_deviation:minor")
    if factors.current_context == CurrentContextRisk.UNCERTAIN:
        reasons.append("current_context:uncertain")
    return MultifactorSafetyDecision(
        risk_level=OnlineRiskLevel.WATCH if watch else OnlineRiskLevel.NORMAL,
        safety_required=False,
        urgent_required=False,
        reason_codes=tuple(dict.fromkeys(reasons)),
        personalization_effect=(
            "noncritical_noise_reduction" if watch else "none"
        ),
        source_refs=factors.source_refs,
    )


__all__ = [
    "EVENT_CONTEXT_RULES",
    "ONLINE_REASONING_POLICY_VERSION",
    "EventContextRule",
    "fuse_multifactor_safety",
    "resolve_event_context",
]
