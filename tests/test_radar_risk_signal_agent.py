from __future__ import annotations

import json
from datetime import date

from sleepagent.radar_agent.agents import (
    ContextPacket,
    EvidencePacket,
    RiskSignalAgent,
    RiskSignalReason,
    TaskContext,
)
from sleepagent.radar_agent.schemas import (
    EvidenceClaim,
    RadarAgentName,
    RadarDataQualityStatus,
    RadarNightSummary,
    ReviewStatus,
    RiskLevel,
)


NIGHT = date(2026, 7, 10)


def test_risk_signal_info_level_uses_rule_claim_with_evidence_refs() -> None:
    decision = RiskSignalAgent().analyze(
        task_id="task-info",
        night_summary=_summary(),
        trend_claims=[],
    )

    assert decision.risk_level == RiskLevel.INFO
    assert decision.reasons == [RiskSignalReason.ROUTINE_OBSERVATION]
    assert decision.evidence_refs == ["night-summary:radar-001:2026-07-10"]
    assert decision.claims[0].review_status == ReviewStatus.REVIEWED
    assert decision.claims[0].evidence_refs == decision.evidence_refs
    assert decision.candidate_actions == ["publish_routine_care_summary"]


def test_risk_signal_watch_level_from_trend_claims() -> None:
    decision = RiskSignalAgent().analyze(
        task_id="task-watch",
        night_summary=_summary(),
        trend_claims=[
            _trend_claim("sleep", RiskLevel.WATCH),
        ],
    )

    assert decision.risk_level == RiskLevel.WATCH
    assert RiskSignalReason.TREND_WATCH_SIGNAL in decision.reasons
    assert "suggest_micro_questionnaire" in decision.candidate_actions
    assert decision.claims[0].risk_level == RiskLevel.WATCH
    assert decision.claims[0].evidence_refs


def test_risk_signal_escalates_only_from_multiple_rule_signals() -> None:
    decision = RiskSignalAgent().analyze(
        task_id="task-escalate",
        night_summary=_summary(out_of_bed_count=6, movement_count=30),
        trend_claims=[
            _trend_claim("sleep", RiskLevel.WATCH),
            _trend_claim("movement", RiskLevel.WATCH),
            _trend_claim("vital", RiskLevel.WATCH),
        ],
    )

    assert decision.risk_level == RiskLevel.ESCALATE
    assert decision.reasons == [RiskSignalReason.MULTI_SIGNAL_ESCALATION]
    assert "prepare_doctor_review_material" in decision.candidate_actions
    assert decision.confirmation_requests
    assert decision.confirmation_requests[0].action_type == "export_doctor_material"
    assert decision.claims[0].risk_level == RiskLevel.ESCALATE


def test_urgent_boundary_text_overrides_sleep_trend_interpretation() -> None:
    decision = RiskSignalAgent().analyze(
        task_id="task-urgent",
        night_summary=_summary(),
        trend_claims=[_trend_claim("sleep", RiskLevel.INFO)],
        text_inputs=["I have chest pain and severe difficulty breathing right now."],
    )

    assert decision.risk_level == RiskLevel.URGENT_BOUNDARY
    assert decision.reasons == [RiskSignalReason.URGENT_TEXT_BOUNDARY]
    assert decision.should_stop_sleep_trend_explanation is True
    assert "stop_sleep_trend_explanation" in decision.candidate_actions
    assert "show_offline_medical_or_emergency_evaluation" in decision.candidate_actions
    assert decision.confirmation_requests[0].action_type == "notify_family_delivery_record"
    assert decision.claims[0].risk_level == RiskLevel.URGENT_BOUNDARY


def test_data_quality_gate_blocks_forced_escalation_when_not_interpretable() -> None:
    decision = RiskSignalAgent().analyze(
        task_id="task-quality-block",
        night_summary=_summary(
            quality_status=RadarDataQualityStatus.UNUSABLE,
            confidence_label="not_interpretable",
            health_conclusion_allowed=False,
        ),
        trend_claims=[
            _trend_claim("sleep", RiskLevel.WATCH),
            _trend_claim("movement", RiskLevel.WATCH),
            _trend_claim("vital", RiskLevel.WATCH),
        ],
    )

    assert decision.risk_level == RiskLevel.UNCERTAIN
    assert decision.reasons == [RiskSignalReason.DATA_NOT_INTERPRETABLE]
    assert decision.should_stop_sleep_trend_explanation is True
    assert "quality_gate_blocks_escalation" in decision.safety_flags
    assert decision.claims[0].risk_level == RiskLevel.UNCERTAIN
    assert decision.claims[0].uncertainty == "data_not_interpretable"


def test_low_quality_vital_abnormality_is_capped_at_watch() -> None:
    decision = RiskSignalAgent().analyze(
        task_id="task-low-quality-vitals",
        night_summary=_summary(
            quality_status=RadarDataQualityStatus.PARTIAL,
            confidence_label="low_confidence",
            abnormal_reading_count=3,
            out_of_bed_count=8,
            movement_count=40,
        ),
        trend_claims=[],
    )

    assert decision.risk_level == RiskLevel.WATCH
    assert decision.reasons == [RiskSignalReason.LOW_QUALITY_VITAL_SIGNAL]
    assert decision.uncertainty == "vital_signal_quality_is_low"
    assert decision.claims[0].risk_level == RiskLevel.WATCH


def test_risk_signal_run_output_has_no_forbidden_clinical_terms_or_llm_surface() -> None:
    context = ContextPacket(
        task_context=TaskContext(
            task_id="task-run",
            trace_id="trace-run",
            purpose="risk",
        ),
        evidence_packet=EvidencePacket(
            night_summaries=[_summary()],
            data_quality={
                "text_inputs": ["昨晚睡得一般，想了解雷达观察结果。"],
            },
        ),
    )

    result = RiskSignalAgent().run(context)
    payload = json.dumps(result.model_dump(mode="json"), ensure_ascii=False).lower()

    assert result.agent_name == RadarAgentName.RISK_SIGNAL
    assert "llm" not in payload
    assert "diagnosis" not in payload
    assert "diagnostic" not in payload
    assert "ahi" not in payload
    assert "psg" not in payload
    assert "sleep_stage" not in payload
    assert "obstructive" not in payload
    assert all(claim.evidence_refs for claim in result.claims)


def test_risk_signal_run_accepts_serialized_trend_claims_from_context() -> None:
    context = ContextPacket(
        task_context=TaskContext(
            task_id="task-run-serialized",
            trace_id="trace-run-serialized",
            purpose="risk",
        ),
        evidence_packet=EvidencePacket(
            night_summaries=[_summary()],
            data_quality={
                "trend_claims": [
                    _trend_claim("serialized", RiskLevel.WATCH).model_dump(mode="json")
                ],
            },
        ),
    )

    result = RiskSignalAgent().run(context)

    assert result.claims[0].risk_level == RiskLevel.WATCH
    assert "suggest_micro_questionnaire" in result.candidate_actions


def _summary(
    *,
    quality_status: RadarDataQualityStatus = RadarDataQualityStatus.GOOD,
    confidence_label: str = "normal",
    health_conclusion_allowed: bool = True,
    abnormal_reading_count: int = 0,
    out_of_bed_count: int = 1,
    movement_count: int = 8,
) -> RadarNightSummary:
    return RadarNightSummary(
        radar_device_id="radar-001",
        subject_id="elder-001",
        night_of=NIGHT,
        total_sleep_minutes=420,
        out_of_bed_count=out_of_bed_count,
        movement_count=movement_count,
        data_coverage_ratio=0.94,
        data_quality_status=quality_status,
        confidence_label=confidence_label,
        health_conclusion_allowed=health_conclusion_allowed,
        abnormal_reading_count=abnormal_reading_count,
        source_report_ref=f"night-summary:radar-001:{NIGHT.isoformat()}",
    )


def _trend_claim(suffix: str, risk_level: RiskLevel) -> EvidenceClaim:
    return EvidenceClaim(
        claim_id=f"trend-claim-{suffix}",
        task_id="task-risk",
        text=f"Trend claim {suffix}",
        evidence_refs=[f"night-summary:radar-001:{suffix}"],
        confidence=0.72,
        risk_level=risk_level,
        generated_by=RadarAgentName.TREND.value,
        review_status=ReviewStatus.REVIEWED,
    )
