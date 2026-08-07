from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from sleepagent.radar_agent.agents import (
    ContextPacket,
    EvidencePacket,
    TaskContext,
    TrendAgent,
    TrendDirection,
    TrendMetricStatus,
)
from sleepagent.radar_agent.schemas import (
    RadarAgentName,
    RadarDataQualityStatus,
    RadarNightSummary,
    ReviewStatus,
    RiskLevel,
)


LATEST_NIGHT = date(2026, 7, 10)


def test_trend_agent_computes_7_30_90_day_structured_metrics_with_evidence_refs() -> None:
    summaries = _history(
        baseline_nights=20,
        recent_nights=90,
        latest_night=LATEST_NIGHT,
    )
    result = TrendAgent().analyze(task_id="task-trend", summaries=summaries)

    assert set(result.windows) == {"7", "30", "90"}
    assert result.latest_night == LATEST_NIGHT
    assert result.subject_id == "elder-001"
    assert result.radar_device_id == "radar-001"
    for window, metrics in result.windows.items():
        assert len(metrics) == 7
        assert all(metric.status == TrendMetricStatus.COMPUTED for metric in metrics)
        assert all(metric.evidence_refs for metric in metrics)
        assert all(metric.sample_count >= metric.required_sample_count for metric in metrics)

    seven_day_sleep = _metric(result.windows["7"], "sleep_minutes")
    seven_day_oob = _metric(result.windows["7"], "out_of_bed_count")
    ninety_day_quality = _metric(result.windows["90"], "data_coverage_ratio")

    assert seven_day_sleep.direction == TrendDirection.DECREASED
    assert seven_day_sleep.value == 360.0
    assert seven_day_sleep.baseline_value == 430.0
    assert seven_day_sleep.absolute_delta == -70.0
    assert seven_day_oob.direction == TrendDirection.INCREASED
    assert seven_day_oob.value == 4.0
    assert seven_day_oob.baseline_value == 1.0
    assert ninety_day_quality.direction == TrendDirection.DECREASED
    assert ninety_day_quality.confidence > 0.7
    assert result.evidence_refs
    assert all(claim.evidence_refs for claim in result.claims)
    assert all(claim.review_status == ReviewStatus.REVIEWED for claim in result.claims)


def test_trend_agent_run_returns_agent_result_claims_without_forbidden_outputs() -> None:
    summaries = _history(
        baseline_nights=20,
        recent_nights=90,
        latest_night=LATEST_NIGHT,
    )
    context = ContextPacket(
        task_context=TaskContext(
            task_id="task-trend-run",
            trace_id="trace-trend-run",
            purpose="analysis",
        ),
        evidence_packet=EvidencePacket(night_summaries=summaries),
    )

    agent_result = TrendAgent().run(context)
    payload_text = json.dumps(agent_result.model_dump(mode="json")).lower()

    assert agent_result.agent_name == RadarAgentName.TREND
    assert agent_result.claims
    assert any(claim.risk_level == RiskLevel.WATCH for claim in agent_result.claims)
    assert "ahi" not in payload_text
    assert "psg" not in payload_text
    assert "sleep_stage" not in payload_text
    assert "diagnosis" not in payload_text
    assert "obstructive" not in payload_text


def test_trend_agent_reports_insufficient_data_for_small_samples() -> None:
    summaries = _history(
        baseline_nights=0,
        recent_nights=2,
        latest_night=LATEST_NIGHT,
    )

    result = TrendAgent().analyze(task_id="task-small", summaries=summaries)
    seven_day_metrics = result.windows["7"]

    assert all(
        metric.status == TrendMetricStatus.INSUFFICIENT_DATA
        for metric in seven_day_metrics
    )
    assert all(
        metric.direction == TrendDirection.INSUFFICIENT_DATA
        for metric in seven_day_metrics
    )
    assert result.uncertainties
    assert len(result.claims) == 1
    assert result.claims[0].risk_level == RiskLevel.UNCERTAIN
    assert result.claims[0].uncertainty == "usable_night_sample_too_small"


def test_trend_agent_ignores_uninterpretable_nights_for_metric_samples() -> None:
    summaries = _history(
        baseline_nights=20,
        recent_nights=2,
        latest_night=LATEST_NIGHT,
    )
    summaries.extend(
        _summary(
            night=LATEST_NIGHT - timedelta(days=offset),
            total_sleep_minutes=None,
            in_bed_minutes=0,
            out_of_bed_count=0,
            movement_count=0,
            breath_rate=0,
            heart_rate=0,
            coverage=0.2,
            quality_status=RadarDataQualityStatus.UNUSABLE,
            health_conclusion_allowed=False,
        )
        for offset in range(2, 7)
    )

    result = TrendAgent().analyze(task_id="task-unusable", summaries=summaries)
    seven_day_sleep = _metric(result.windows["7"], "sleep_minutes")

    assert seven_day_sleep.status == TrendMetricStatus.INSUFFICIENT_DATA
    assert seven_day_sleep.sample_count == 2
    assert "7d sleep_minutes: insufficient_data" in result.uncertainties


def test_trend_agent_does_not_borrow_old_device_baseline() -> None:
    old_device = _history(
        baseline_nights=20,
        recent_nights=0,
        latest_night=LATEST_NIGHT - timedelta(days=7),
    )
    recent_device = _history(
        baseline_nights=0,
        recent_nights=2,
        latest_night=LATEST_NIGHT,
    )
    recent_device = [
        item.model_copy(update={"radar_device_id": "radar-002"})
        for item in recent_device
    ]

    result = TrendAgent().analyze(
        task_id="task-device-change",
        summaries=[*old_device, *recent_device],
    )
    seven_day_sleep = _metric(result.windows["7"], "sleep_minutes")

    assert seven_day_sleep.status == TrendMetricStatus.INSUFFICIENT_DATA
    assert seven_day_sleep.sample_count == 2
    assert seven_day_sleep.baseline_value is None


def _history(
    *,
    baseline_nights: int,
    recent_nights: int,
    latest_night: date,
) -> list[RadarNightSummary]:
    summaries: list[RadarNightSummary] = []
    baseline_start = latest_night - timedelta(days=recent_nights + baseline_nights - 1)
    for offset in range(baseline_nights):
        summaries.append(
            _summary(
                night=baseline_start + timedelta(days=offset),
                total_sleep_minutes=430,
                in_bed_minutes=460,
                out_of_bed_count=1,
                movement_count=8,
                breath_rate=15,
                heart_rate=64,
                coverage=0.96,
            )
        )

    recent_start = latest_night - timedelta(days=recent_nights - 1)
    for offset in range(recent_nights):
        summaries.append(
            _summary(
                night=recent_start + timedelta(days=offset),
                total_sleep_minutes=360,
                in_bed_minutes=400,
                out_of_bed_count=4,
                movement_count=18,
                breath_rate=18,
                heart_rate=74,
                coverage=0.88,
                quality_status=(
                    RadarDataQualityStatus.PARTIAL
                    if offset % 10 == 0
                    else RadarDataQualityStatus.GOOD
                ),
            )
        )
    return summaries


def _summary(
    *,
    night: date,
    total_sleep_minutes: float | None,
    in_bed_minutes: float,
    out_of_bed_count: int,
    movement_count: int,
    breath_rate: float,
    heart_rate: float,
    coverage: float,
    quality_status: RadarDataQualityStatus = RadarDataQualityStatus.GOOD,
    health_conclusion_allowed: bool = True,
) -> RadarNightSummary:
    return RadarNightSummary(
        radar_device_id="radar-001",
        subject_id="elder-001",
        night_of=night,
        timezone_name="Asia/Shanghai",
        night_boundary_start_at=datetime.combine(
            night,
            datetime.min.time(),
            tzinfo=timezone.utc,
        ),
        night_boundary_end_at=datetime.combine(
            night + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ),
        total_sleep_minutes=total_sleep_minutes,
        out_of_bed_count=out_of_bed_count,
        movement_count=movement_count,
        data_coverage_ratio=coverage,
        data_quality_status=quality_status,
        confidence_label=(
            "low_confidence"
            if quality_status == RadarDataQualityStatus.PARTIAL
            else "normal"
        ),
        health_conclusion_allowed=health_conclusion_allowed,
        explainable_metrics={
            "in_bed_minutes": in_bed_minutes,
            "mean_breath_rate_bpm": breath_rate,
            "mean_heart_rate_bpm": heart_rate,
            "coverage_ratio": coverage,
        },
        source_report_ref=f"night-summary:radar-001:{night.isoformat()}",
    )


def _metric(metrics, metric_name: str):
    return next(metric for metric in metrics if metric.metric_name == metric_name)
