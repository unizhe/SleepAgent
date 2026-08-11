from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sleepagent.product_runtime.contracts import (
    AuthenticatedBinding,
    FactSnapshot,
    SourceScope,
    SourceScopeKind,
)
from sleepagent.product_runtime.tools.trend_analysis import (
    TrendAnalysisTool,
    TrendDirection,
    TrendMetricStatus,
)
from sleepagent.product_runtime.tooling import (
    CoreProductToolService,
    ProductToolExecutionContext,
    ProductToolExecutor,
)
from sleepagent.product_runtime.schemas import (
    RadarDataQualityStatus,
    RadarNightSummary,
)
from tests.support.golden_fixtures import (
    canonical_json_sha256,
    load_product_capability_goldens,
)


LATEST = date(2026, 7, 10)


def test_trend_tool_preserves_7_30_90_window_baseline_and_coverage_semantics() -> None:
    summaries = _history(baseline_nights=20, recent_nights=90)
    expected = load_product_capability_goldens()["trend_analysis"]
    migrated = TrendAnalysisTool().analyze(summaries)

    assert set(migrated.windows) == set(expected["windows"])
    assert migrated.latest_night.isoformat() == expected["latest_night"]
    assert expected["claim_count"] == len(expected["metrics"])
    assert expected["claim_source_kinds"] == ["trend_tool"]
    for window, window_expected in expected["windows"].items():
        migrated_metrics = migrated.windows[window]
        assert len(migrated_metrics) == len(expected["metrics"])
        for new in migrated_metrics:
            metric_expected = expected["metrics"][new.metric_name]
            assert new.status.value == metric_expected["status"]
            assert new.direction.value == metric_expected["direction"]
            assert new.sample_count == window_expected["sample_count"]
            assert new.required_sample_count == window_expected[
                "required_sample_count"
            ]
            assert new.value == metric_expected["value"]
            assert new.baseline_value == metric_expected["baseline_value"]
            assert new.absolute_delta == metric_expected["absolute_delta"]
            assert new.percent_delta == metric_expected["percent_delta"]
            assert new.confidence == metric_expected["confidence"]
            assert len(new.evidence_refs) == window_expected[
                "evidence_ref_count"
            ]
            assert canonical_json_sha256(new.evidence_refs) == window_expected[
                "evidence_refs_sha256"
            ]
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


def test_trend_tool_expresses_insufficient_window_uncertainty() -> None:
    summary = _summary(
        night=LATEST,
        sleep_minutes=360,
        coverage=0.92,
        calibration_state="unknown",
    )

    analysis = TrendAnalysisTool().analyze([summary])
    metrics = analysis.windows["7"]

    assert metrics
    assert all(
        item.direction is TrendDirection.INSUFFICIENT_DATA
        for item in metrics
    )
    assert all(
        item.evidence_refs == [summary.source_report_ref]
        for item in metrics
    )
    assert analysis.uncertainties


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


def test_product_tool_executor_dispatches_structured_trend_analysis() -> None:
    summaries = _history(baseline_nights=20, recent_nights=90)

    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
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

    result = ProductToolExecutor(
        core_service=CoreProductToolService()
    ).execute(
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
