from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sleepagent.radar_agent.agents import TrendAgent
from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    AuthenticatedBinding,
    FactSnapshot,
    SourceScope,
    SourceScopeKind,
)
from sleepagent.radar_agent.product_agent.skill_methods.evidence_trend import (
    EvidenceTrendSkill,
)
from sleepagent.radar_agent.product_agent.tools.trend_analysis import (
    TrendAnalysisTool,
    TrendDirection,
    TrendMetricStatus,
)
from sleepagent.radar_agent.product_agent.tooling import (
    ProductToolExecutionContext,
    ProductToolExecutor,
)
from sleepagent.radar_agent.schemas import (
    RadarDataQualityStatus,
    RadarNightSummary,
)


LATEST = date(2026, 7, 10)


def test_trend_tool_preserves_7_30_90_window_baseline_and_coverage_semantics() -> None:
    summaries = _history(baseline_nights=20, recent_nights=90)
    legacy = TrendAgent().analyze(task_id="legacy-trend", summaries=summaries)
    migrated = TrendAnalysisTool().analyze(summaries)

    assert legacy.claims
    assert all(claim.source_kind == "trend_tool" for claim in legacy.claims)
    assert set(migrated.windows) == {"7", "30", "90"}
    assert migrated.latest_night == legacy.latest_night
    for window, legacy_metrics in legacy.windows.items():
        migrated_metrics = migrated.windows[window]
        assert len(migrated_metrics) == len(legacy_metrics)
        for old, new in zip(legacy_metrics, migrated_metrics, strict=True):
            assert new.metric_name == old.metric_name
            assert new.status.value == old.status.value
            assert new.direction.value == old.direction.value
            assert new.sample_count == old.sample_count
            assert new.required_sample_count == old.required_sample_count
            assert new.value == old.value
            assert new.baseline_value == old.baseline_value
            assert new.absolute_delta == old.absolute_delta
            assert new.percent_delta == old.percent_delta
            assert new.confidence == old.confidence
            assert new.evidence_refs == old.evidence_refs
            assert new.measurement_cohort_ref

    coverage = _metric(migrated.windows["90"], "data_coverage_ratio")
    assert coverage.status == TrendMetricStatus.COMPUTED
    assert coverage.direction == TrendDirection.DECREASED
    assert coverage.confidence > 0.7


def test_trend_tool_never_borrows_baseline_across_calibration_cohorts() -> None:
    baseline = [
        _summary(
            night=LATEST - timedelta(days=110 - offset),
            sleep_minutes=430,
            coverage=0.96,
            calibration_state="known_uncalibrated",
        )
        for offset in range(20)
    ]
    recent = [
        _summary(
            night=LATEST - timedelta(days=6 - offset),
            sleep_minutes=350,
            coverage=0.9,
            calibration_state="unknown",
        )
        for offset in range(7)
    ]

    result = TrendAnalysisTool().analyze([*baseline, *recent])
    metric = _metric(result.windows["7"], "sleep_minutes")

    assert metric.status == TrendMetricStatus.COMPUTED
    assert metric.baseline_sample_count == 0
    assert metric.calibration_state == "unknown"
    assert metric.baseline_value == 350.0
    assert any("early versus late" in item for item in metric.caveats)


def test_trend_tool_preserves_valid_calibrated_cohort_version() -> None:
    summaries = [
        _summary(
            night=LATEST - timedelta(days=6 - offset),
            sleep_minutes=360 + offset,
            coverage=0.92,
            calibration_state="known_calibrated",
            calibration_version="calibration-reviewed.v2",
        )
        for offset in range(7)
    ]

    metric = _metric(
        TrendAnalysisTool().analyze(summaries).windows["7"],
        "sleep_minutes",
    )

    assert metric.status is TrendMetricStatus.COMPUTED
    assert metric.calibration_state == "known_calibrated"
    assert metric.measurement_cohort_ref


def test_trend_tool_never_borrows_baseline_across_calibration_versions() -> None:
    baseline = [
        _summary(
            night=LATEST - timedelta(days=110 - offset),
            sleep_minutes=430,
            coverage=0.96,
            calibration_state="known_calibrated",
            calibration_version="calibration-reviewed.v1",
        )
        for offset in range(20)
    ]
    recent = [
        _summary(
            night=LATEST - timedelta(days=6 - offset),
            sleep_minutes=350,
            coverage=0.9,
            calibration_state="known_calibrated",
            calibration_version="calibration-reviewed.v2",
        )
        for offset in range(7)
    ]

    metric = _metric(
        TrendAnalysisTool().analyze([*baseline, *recent]).windows["7"],
        "sleep_minutes",
    )

    assert metric.baseline_sample_count == 0
    assert metric.baseline_value == 350.0
    assert any("early versus late" in item for item in metric.caveats)


def test_evidence_trend_skill_expresses_insufficient_window_uncertainty() -> None:
    summary = _summary(
        night=LATEST,
        sleep_minutes=360,
        coverage=0.92,
        calibration_state="unknown",
    )

    drafts = EvidenceTrendSkill().interpret(
        TrendAnalysisTool().analyze([summary])
    )

    assert drafts
    assert all(item.risk_signal == "uncertain" for item in drafts)
    assert all(
        item.direction is TrendDirection.INSUFFICIENT_DATA
        for item in drafts
    )
    assert all(item.evidence_refs == [summary.source_report_ref] for item in drafts)


def test_missing_compatible_baseline_is_explicit_uncertainty() -> None:
    summaries = [
        _summary(
            night=LATEST - timedelta(days=3 - offset),
            sleep_minutes=360 + offset,
            coverage=0.92,
            calibration_state="unknown",
        )
        for offset in range(4)
    ]
    analysis = TrendAnalysisTool().analyze(summaries)
    metric = _metric(analysis.windows["7"], "sleep_minutes")

    assert metric.status is TrendMetricStatus.COMPUTED
    assert metric.direction is TrendDirection.INSUFFICIENT_DATA
    assert "7d sleep_minutes: insufficient_data" in analysis.uncertainties
    draft = next(
        item
        for item in EvidenceTrendSkill().interpret(analysis)
        if item.metric_id == "sleep_minutes"
    )
    assert draft.risk_signal == "uncertain"
    assert "baseline" in draft.statement


def test_evidence_trend_skill_interprets_tool_output_without_recreating_trend_agent() -> None:
    analysis = TrendAnalysisTool().analyze(
        _history(baseline_nights=20, recent_nights=90)
    )

    drafts = EvidenceTrendSkill().interpret(analysis)

    assert EvidenceTrendSkill.owner is AgentId.EVIDENCE_REASONING
    assert EvidenceTrendSkill.skill_id == "interpret_longitudinal_pattern"
    assert drafts
    assert any(item.risk_signal == "watch" for item in drafts)
    assert all(item.evidence_refs for item in drafts)
    assert all(item.measurement_cohort_ref for item in drafts)
    assert all(not hasattr(item, "agent_name") for item in drafts)


def test_product_tool_executor_dispatches_structured_trend_analysis() -> None:
    summaries = _history(baseline_nights=20, recent_nights=90)

    result = ProductToolExecutor().execute(
        "trend.calculate_metrics",
        {
            "night_summaries": [
                item.model_dump(mode="json") for item in summaries
            ]
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(summaries=summaries),
            episode_id="episode-phase3a-trend",
        ),
    )

    assert result.receipt.output["tool_version"].startswith(
        "sleepagent-trend-analysis-tool"
    )
    assert set(result.receipt.output["windows"]) == {"7", "30", "90"}
    assert result.receipt.source_refs


def test_product_trend_handler_rejects_cross_subject_summaries() -> None:
    summaries = _history(baseline_nights=3, recent_nights=7)

    result = ProductToolExecutor().execute(
        "trend.calculate_metrics",
        {
            "night_summaries": [
                item.model_copy(update={"subject_id": "other-subject"}).model_dump(
                    mode="json"
                )
                for item in summaries
            ]
        },
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(summaries=summaries),
        ),
    )

    assert result.receipt.outcome.value == "failed"
    assert result.receipt.error_code == "ValueError"


def _history(*, baseline_nights: int, recent_nights: int) -> list[RadarNightSummary]:
    baseline_start = LATEST - timedelta(days=recent_nights + baseline_nights - 1)
    baseline = [
        _summary(
            night=baseline_start + timedelta(days=offset),
            sleep_minutes=430,
            coverage=0.96,
            calibration_state="known_uncalibrated",
            out_of_bed=1,
            movement=8,
            breath_rate=15,
            heart_rate=64,
        )
        for offset in range(baseline_nights)
    ]
    recent_start = LATEST - timedelta(days=recent_nights - 1)
    recent = [
        _summary(
            night=recent_start + timedelta(days=offset),
            sleep_minutes=360,
            coverage=0.88,
            calibration_state="known_uncalibrated",
            out_of_bed=4,
            movement=18,
            breath_rate=18,
            heart_rate=74,
        )
        for offset in range(recent_nights)
    ]
    return [*baseline, *recent]


def _summary(
    *,
    night: date,
    sleep_minutes: float,
    coverage: float,
    calibration_state: str,
    calibration_version: str | None = None,
    out_of_bed: int = 4,
    movement: int = 18,
    breath_rate: float = 18,
    heart_rate: float = 74,
) -> RadarNightSummary:
    return RadarNightSummary(
        radar_device_id="radar-001",
        subject_id="elder-001",
        night_of=night,
        timezone_name="Asia/Shanghai",
        night_boundary_start_at=datetime.combine(
            night, datetime.min.time(), tzinfo=timezone.utc
        ),
        night_boundary_end_at=datetime.combine(
            night + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
        ),
        total_sleep_minutes=sleep_minutes,
        out_of_bed_count=out_of_bed,
        movement_count=movement,
        data_coverage_ratio=coverage,
        data_quality_status=RadarDataQualityStatus.GOOD,
        health_conclusion_allowed=True,
        explainable_metrics={
            "in_bed_minutes": sleep_minutes + 35,
            "mean_breath_rate_bpm": breath_rate,
            "mean_heart_rate_bpm": heart_rate,
            "calibration_state": calibration_state,
            **(
                {"calibration_version": calibration_version}
                if calibration_version is not None
                else {}
            ),
        },
        source_report_ref=f"night-summary:radar-001:{night.isoformat()}",
    )


def _metric(metrics, name: str):
    return next(item for item in metrics if item.metric_name == name)


def _snapshot(
    *,
    summaries: list[RadarNightSummary] | None = None,
) -> FactSnapshot:
    as_of = datetime(2026, 7, 10, 8, tzinfo=timezone.utc)
    summaries = summaries or []
    date_start = min(
        (item.night_of for item in summaries),
        default=LATEST - timedelta(days=89),
    )
    return FactSnapshot.create(
        fact_snapshot_id="phase3a-trend-snapshot",
        binding=AuthenticatedBinding(
            actor_id="elder-001",
            subject_id="elder-001",
            role="elder",
        ),
        source_scope=SourceScope(
            kind=SourceScopeKind.HISTORICAL_RANGE,
            as_of=as_of,
            timezone_name="Asia/Shanghai",
            date_start=date_start,
            date_end=LATEST,
            valid_night_count=len({item.night_of for item in summaries}),
        ),
        canonical_data_version="phase3a.v1",
        source_refs=tuple(
            dict.fromkeys(
                [
                    "range:phase3a",
                    *[
                        item.source_report_ref
                        for item in summaries
                        if item.source_report_ref
                    ],
                ]
            )
        ),
        created_at=as_of,
    )
