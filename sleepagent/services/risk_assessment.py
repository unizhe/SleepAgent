from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from sleepagent.schemas import (
    AnalysisNodeResult,
    AnalysisNodeStatus,
    RespiratoryEvent,
    RespiratoryEventType,
    RespiratorySummary,
    RiskLevel,
    SleepStageSummary,
)
from sleepagent.services.respiratory_model_gate import (
    RESPIRATORY_MODEL_STATUS_NOT_VALIDATED,
    RESPIRATORY_MODEL_STATUS_VALIDATED,
)


def summarize_respiratory_events(
    events: Sequence[RespiratoryEvent],
    *,
    sleep_summary: SleepStageSummary,
) -> RespiratorySummary:
    normal_count = sum(
        event.event_type == RespiratoryEventType.NORMAL_BREATHING for event in events
    )
    hypopnea_count = sum(
        event.event_type == RespiratoryEventType.HYPOPNEA for event in events
    )
    suspected_apnea_count = sum(
        event.event_type == RespiratoryEventType.SUSPECTED_APNEA for event in events
    )
    sleep_hours = _analysis_sleep_hours(sleep_summary)
    return RespiratorySummary(
        ahi=round((hypopnea_count + suspected_apnea_count) / sleep_hours, 2),
        normal_breathing_count=normal_count,
        hypopnea_count=hypopnea_count,
        suspected_apnea_count=suspected_apnea_count,
        mean_respiratory_rate_bpm=None,
    )


def infer_risk_level_from_respiratory_summary(
    summary: RespiratorySummary,
) -> RiskLevel:
    if summary.ahi >= 15 or summary.suspected_apnea_count >= 20:
        return RiskLevel.HIGH
    if summary.ahi >= 5 or summary.suspected_apnea_count >= 5:
        return RiskLevel.MODERATE
    return RiskLevel.LOW


def build_risk_assessment_result(
    summary: RespiratorySummary,
    *,
    caveats: Sequence[str] = (),
    source_paths: dict[str, str] | None = None,
) -> AnalysisNodeResult:
    risk_level = infer_risk_level_from_respiratory_summary(summary)
    evidence_chain = [
        f"AHI={summary.ahi:.2f} events/hour from mapped SHHS respiratory annotations.",
        (
            "hypopnea_count="
            f"{summary.hypopnea_count}, suspected_apnea_count="
            f"{summary.suspected_apnea_count}."
        ),
    ]
    if "respiratory_model_unvalidated" in caveats:
        evidence_chain.append(
            "Respiratory model output is not used as negative evidence because the "
            "current checkpoint is unvalidated for conclusions."
        )
    respiratory_model_status = (
        RESPIRATORY_MODEL_STATUS_NOT_VALIDATED
        if (
            "respiratory_model_unvalidated" in caveats
            or RESPIRATORY_MODEL_STATUS_NOT_VALIDATED in caveats
        )
        else RESPIRATORY_MODEL_STATUS_VALIDATED
    )

    return AnalysisNodeResult(
        name="RiskAssessment",
        status=AnalysisNodeStatus.COMPLETED,
        payload={
            "risk_level": risk_level.value,
            "evidence_chain": evidence_chain,
            "respiratory_model_status": respiratory_model_status,
        },
        caveats=list(caveats),
        source_paths=source_paths or {},
        metrics={
            "ahi": summary.ahi,
            "normal_breathing_count": summary.normal_breathing_count,
            "hypopnea_count": summary.hypopnea_count,
            "suspected_apnea_count": summary.suspected_apnea_count,
        },
        generated_at=datetime.now(timezone.utc),
    )


def _analysis_sleep_hours(summary: SleepStageSummary) -> float:
    sleep_hours = summary.total_sleep_time_minutes / 60
    if sleep_hours > 0:
        return sleep_hours
    recording_hours = summary.total_recording_minutes / 60
    return max(recording_hours, 0.1)
