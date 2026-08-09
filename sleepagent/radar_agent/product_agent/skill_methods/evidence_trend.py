from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from sleepagent.radar_agent.product_agent.contracts import AgentId, StrictContract
from sleepagent.radar_agent.product_agent.tools.trend_analysis import (
    TrendAnalysisResult,
    TrendDirection,
    TrendMetricStatus,
    is_watch_direction,
    metric_label,
)


class TrendEvidenceDraft(StrictContract):
    """Evidence-owned interpretation draft built from one Tool metric."""

    metric_id: str = Field(..., min_length=1)
    statement: str = Field(..., min_length=1)
    direction: TrendDirection
    risk_signal: Literal["info", "watch", "uncertain"]
    evidence_refs: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    measurement_cohort_ref: str = Field(..., min_length=1)
    date_start: date
    date_end: date
    alternative_explanations: list[str] = Field(min_length=1)


class EvidenceTrendSkill:
    """Versioned Evidence method; it does not recompute Tool observations."""

    owner = AgentId.EVIDENCE_REASONING
    skill_id = "interpret_longitudinal_pattern"
    skill_version = "1.0.0"

    def interpret(self, analysis: TrendAnalysisResult) -> list[TrendEvidenceDraft]:
        drafts: list[TrendEvidenceDraft] = []
        for metric in analysis.windows.get("7", []):
            if (
                (
                    metric.status != TrendMetricStatus.COMPUTED
                    or metric.direction
                    in {
                        TrendDirection.INSUFFICIENT_DATA,
                        TrendDirection.NOT_INTERPRETABLE,
                    }
                )
                and metric.evidence_refs
                and metric.measurement_cohort_ref
            ):
                if metric.status == TrendMetricStatus.INSUFFICIENT_DATA:
                    statement = (
                        f"7-day {metric_label(metric.metric_name)} has "
                        f"insufficient data ({metric.sample_count}/"
                        f"{metric.required_sample_count} usable nights); "
                        "no directional conclusion is available."
                    )
                else:
                    statement = (
                        f"7-day {metric_label(metric.metric_name)} has usable "
                        "observations, but a compatible baseline comparison "
                        "is unavailable; no directional conclusion is available."
                    )
                drafts.append(
                    TrendEvidenceDraft(
                        metric_id=metric.metric_name,
                        statement=statement,
                        direction=metric.direction,
                        risk_signal="uncertain",
                        evidence_refs=metric.evidence_refs,
                        confidence=metric.confidence,
                        measurement_cohort_ref=metric.measurement_cohort_ref,
                        date_start=metric.window_start,
                        date_end=metric.window_end,
                        alternative_explanations=[
                            "Missing or unusable nights can prevent a reliable "
                            "comparison with the individual's baseline."
                        ],
                    )
                )
                continue
            if (
                metric.status != TrendMetricStatus.COMPUTED
                or metric.direction
                in {
                    TrendDirection.STABLE,
                    TrendDirection.INSUFFICIENT_DATA,
                    TrendDirection.NOT_INTERPRETABLE,
                }
                or not metric.evidence_refs
                or not metric.measurement_cohort_ref
            ):
                continue
            label = metric_label(metric.metric_name)
            direction = (
                "increased"
                if metric.direction == TrendDirection.INCREASED
                else "decreased"
            )
            if metric.baseline_value is None or metric.value is None:
                statement = (
                    f"7-day {label} trend is structured but baseline "
                    "comparison is unavailable."
                )
            else:
                statement = (
                    f"7-day {label} {direction} versus the individual's "
                    f"baseline ({metric.value} vs {metric.baseline_value})."
                )
            drafts.append(
                TrendEvidenceDraft(
                    metric_id=metric.metric_name,
                    statement=statement,
                    direction=metric.direction,
                    risk_signal=(
                        "watch" if is_watch_direction(metric) else "info"
                    ),
                    evidence_refs=metric.evidence_refs,
                    confidence=metric.confidence,
                    measurement_cohort_ref=metric.measurement_cohort_ref,
                    date_start=metric.window_start,
                    date_end=metric.window_end,
                    alternative_explanations=[
                        "A short series can reflect routine, environment, device, "
                        "or measurement changes and does not establish causation."
                    ],
                )
            )
        return drafts


__all__ = ["EvidenceTrendSkill", "TrendEvidenceDraft"]
