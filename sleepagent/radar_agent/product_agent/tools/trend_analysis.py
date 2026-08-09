from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Any, Literal

from pydantic import Field

from sleepagent.radar_agent.product_agent.cold_start import MeasurementCohort
from sleepagent.radar_agent.product_agent.contracts import StrictContract, stable_hash
from sleepagent.radar_agent.schemas import (
    RadarDataQualityStatus,
    RadarNightSummary,
)


TREND_ANALYSIS_TOOL_VERSION = "sleepagent-trend-analysis-tool.v1"

TrendMetricName = Literal[
    "sleep_minutes",
    "in_bed_minutes",
    "out_of_bed_count",
    "movement_count",
    "breath_rate_bpm",
    "heart_rate_bpm",
    "data_coverage_ratio",
]
TrendWindowDays = Literal[7, 30, 90]


class TrendDirection(str, Enum):
    INCREASED = "increased"
    DECREASED = "decreased"
    STABLE = "stable"
    INSUFFICIENT_DATA = "insufficient_data"
    NOT_INTERPRETABLE = "not_interpretable"


class TrendMetricStatus(str, Enum):
    COMPUTED = "computed"
    INSUFFICIENT_DATA = "insufficient_data"
    NOT_INTERPRETABLE = "not_interpretable"


class TrendWindowMetric(StrictContract):
    metric_name: TrendMetricName
    window_days: TrendWindowDays
    window_start: date
    window_end: date
    status: TrendMetricStatus
    direction: TrendDirection = TrendDirection.INSUFFICIENT_DATA
    sample_count: int = Field(default=0, ge=0)
    required_sample_count: int = Field(default=0, ge=0)
    baseline_sample_count: int = Field(default=0, ge=0)
    required_baseline_sample_count: int = Field(default=0, ge=0)
    value: float | None = None
    baseline_value: float | None = None
    absolute_delta: float | None = None
    percent_delta: float | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    measurement_cohort_ref: str | None = None
    calibration_state: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class TrendAnalysisResult(StrictContract):
    tool_version: str = TREND_ANALYSIS_TOOL_VERSION
    subject_id: str | None = None
    radar_device_id: str | None = None
    latest_night: date | None = None
    windows: dict[str, list[TrendWindowMetric]] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _MetricSpec:
    name: TrendMetricName
    label: str
    stable_delta: float
    stable_percent: float
    higher_is_watch_signal: bool


class TrendAnalysisTool:
    """Deterministic 7/30/90-day trend computation over canonical summaries.

    Every metric is restricted to the exact measurement cohort of its latest
    usable sample.  Device, adapter, schema, firmware, calibration and sleep-day
    boundary changes therefore cannot silently borrow an incompatible baseline.
    The Tool computes observations only; EvidenceReasoning owns interpretation.
    """

    def __init__(
        self,
        *,
        windows: tuple[TrendWindowDays, ...] = (7, 30, 90),
        min_samples_by_window: dict[int, int] | None = None,
        baseline_min_samples: int = 3,
    ) -> None:
        self.windows = windows
        self.min_samples_by_window = min_samples_by_window or {
            7: 3,
            30: 7,
            90: 14,
        }
        self.baseline_min_samples = baseline_min_samples

    def analyze(self, summaries: list[RadarNightSummary]) -> TrendAnalysisResult:
        ordered = _dedupe_summaries(summaries)
        if not ordered:
            return TrendAnalysisResult(
                uncertainties=[
                    "No canonical night summaries are available for trend analysis."
                ],
                caveats=_trend_caveats(),
            )

        latest_night = ordered[-1].night_of
        subject_ids = {item.subject_id for item in ordered if item.subject_id}
        device_ids = {
            item.radar_device_id for item in ordered if item.radar_device_id
        }
        if len(subject_ids) > 1:
            return TrendAnalysisResult(
                latest_night=latest_night,
                uncertainties=[
                    "Cross-subject summaries are not eligible for one trend."
                ],
                caveats=_trend_caveats(),
            )

        baseline_start = latest_night - timedelta(days=max(self.windows) - 1)
        baseline_summaries = [
            item
            for item in ordered
            if latest_night - timedelta(days=180)
            <= item.night_of
            < baseline_start
        ]
        windows: dict[str, list[TrendWindowMetric]] = {}
        source_refs: list[str] = []
        uncertainties: list[str] = []
        for window_days in self.windows:
            window_start = latest_night - timedelta(days=window_days - 1)
            window_summaries = [
                item
                for item in ordered
                if window_start <= item.night_of <= latest_night
            ]
            metrics = [
                self._build_metric(
                    spec=spec,
                    window_days=window_days,
                    window_start=window_start,
                    latest_night=latest_night,
                    window_summaries=window_summaries,
                    baseline_summaries=baseline_summaries,
                )
                for spec in _METRIC_SPECS
            ]
            windows[str(window_days)] = metrics
            for metric in metrics:
                source_refs.extend(metric.evidence_refs)
                if (
                    metric.status != TrendMetricStatus.COMPUTED
                    or metric.direction
                    in {
                        TrendDirection.INSUFFICIENT_DATA,
                        TrendDirection.NOT_INTERPRETABLE,
                    }
                ):
                    uncertainties.append(
                        f"{window_days}d {metric.metric_name}: "
                        f"{metric.direction.value}"
                    )

        return TrendAnalysisResult(
            subject_id=_single_or_none(subject_ids),
            radar_device_id=_single_or_none(device_ids),
            latest_night=latest_night,
            windows=windows,
            source_refs=_dedupe(source_refs),
            uncertainties=_dedupe(uncertainties),
            caveats=_trend_caveats(),
        )

    def _build_metric(
        self,
        *,
        spec: _MetricSpec,
        window_days: TrendWindowDays,
        window_start: date,
        latest_night: date,
        window_summaries: list[RadarNightSummary],
        baseline_summaries: list[RadarNightSummary],
    ) -> TrendWindowMetric:
        required_samples = self.min_samples_by_window[window_days]
        candidates = _usable_summaries_for_metric(window_summaries, spec.name)
        cohort = (
            _measurement_cohort(candidates[-1], spec.name)
            if candidates
            else None
        )
        cohort_ref = cohort.cohort_ref if cohort is not None else None
        usable_window = [
            item
            for item in candidates
            if _measurement_cohort(item, spec.name).cohort_ref == cohort_ref
        ]
        evidence_refs = [_summary_ref(item) for item in usable_window]
        common = {
            "metric_name": spec.name,
            "window_days": window_days,
            "window_start": window_start,
            "window_end": latest_night,
            "sample_count": len(usable_window),
            "required_sample_count": required_samples,
            "required_baseline_sample_count": self.baseline_min_samples,
            "measurement_cohort_ref": cohort_ref,
            "calibration_state": (
                cohort.calibration_state if cohort is not None else None
            ),
        }
        if len(usable_window) < required_samples:
            return TrendWindowMetric(
                **common,
                status=TrendMetricStatus.INSUFFICIENT_DATA,
                direction=TrendDirection.INSUFFICIENT_DATA,
                evidence_refs=evidence_refs,
                caveats=[
                    "Sample count is below the minimum for this trend window."
                ],
            )

        value = _mean(_metric_value(item, spec.name) for item in usable_window)
        usable_baseline = [
            item
            for item in _usable_summaries_for_metric(
                baseline_summaries, spec.name
            )
            if _measurement_cohort(item, spec.name).cohort_ref == cohort_ref
        ]
        baseline_refs = [_summary_ref(item) for item in usable_baseline]
        common["baseline_sample_count"] = len(usable_baseline)
        if len(usable_baseline) < self.baseline_min_samples:
            split_at = len(usable_window) // 2
            early_window = usable_window[:split_at]
            late_window = usable_window[split_at:]
            if len(early_window) >= 3 and len(late_window) >= 3:
                baseline_value = _mean(
                    _metric_value(item, spec.name) for item in early_window
                )
                recent_value = _mean(
                    _metric_value(item, spec.name) for item in late_window
                )
                if baseline_value is not None and recent_value is not None:
                    absolute_delta, percent_delta = _deltas(
                        recent_value, baseline_value
                    )
                    return TrendWindowMetric(
                        **common,
                        status=TrendMetricStatus.COMPUTED,
                        direction=_direction(
                            absolute_delta=absolute_delta,
                            percent_delta=percent_delta,
                            stable_delta=spec.stable_delta,
                            stable_percent=spec.stable_percent,
                        ),
                        value=recent_value,
                        baseline_value=baseline_value,
                        absolute_delta=absolute_delta,
                        percent_delta=percent_delta,
                        confidence=_confidence(usable_window),
                        evidence_refs=evidence_refs,
                        caveats=[
                            "Older personal baseline is unavailable; direction "
                            "uses the early versus late portions of this window."
                        ],
                    )
            return TrendWindowMetric(
                **common,
                status=TrendMetricStatus.COMPUTED,
                direction=TrendDirection.INSUFFICIENT_DATA,
                value=value,
                confidence=_confidence(usable_window),
                evidence_refs=_dedupe([*evidence_refs, *baseline_refs]),
                caveats=["Individual baseline is unavailable or too small."],
            )

        baseline_value = _mean(
            _metric_value(item, spec.name) for item in usable_baseline
        )
        if baseline_value is None or value is None:
            return TrendWindowMetric(
                **common,
                status=TrendMetricStatus.NOT_INTERPRETABLE,
                direction=TrendDirection.NOT_INTERPRETABLE,
                evidence_refs=_dedupe([*evidence_refs, *baseline_refs]),
                caveats=["Metric values are not interpretable for this window."],
            )
        absolute_delta, percent_delta = _deltas(value, baseline_value)
        return TrendWindowMetric(
            **common,
            status=TrendMetricStatus.COMPUTED,
            direction=_direction(
                absolute_delta=absolute_delta,
                percent_delta=percent_delta,
                stable_delta=spec.stable_delta,
                stable_percent=spec.stable_percent,
            ),
            value=value,
            baseline_value=baseline_value,
            absolute_delta=absolute_delta,
            percent_delta=percent_delta,
            confidence=_confidence(usable_window),
            evidence_refs=_dedupe([*evidence_refs, *baseline_refs]),
        )


_METRIC_SPECS: tuple[_MetricSpec, ...] = (
    _MetricSpec("sleep_minutes", "sleep duration", 20.0, 5.0, False),
    _MetricSpec("in_bed_minutes", "in-bed duration", 20.0, 5.0, False),
    _MetricSpec("out_of_bed_count", "out-of-bed count", 1.0, 20.0, True),
    _MetricSpec("movement_count", "movement count", 3.0, 20.0, True),
    _MetricSpec("breath_rate_bpm", "breathing rate", 1.5, 8.0, True),
    _MetricSpec("heart_rate_bpm", "heart rate", 5.0, 8.0, True),
    _MetricSpec("data_coverage_ratio", "data coverage", 0.05, 5.0, False),
)
METRIC_SPECS = {item.name: item for item in _METRIC_SPECS}


def is_watch_direction(metric: TrendWindowMetric) -> bool:
    spec = METRIC_SPECS[metric.metric_name]
    if metric.direction == TrendDirection.STABLE:
        return False
    if spec.higher_is_watch_signal:
        return metric.direction == TrendDirection.INCREASED
    return metric.direction == TrendDirection.DECREASED


def metric_label(metric_name: TrendMetricName) -> str:
    return METRIC_SPECS[metric_name].label


def _dedupe_summaries(
    summaries: list[RadarNightSummary],
) -> list[RadarNightSummary]:
    by_key: dict[tuple[str | None, str, date], RadarNightSummary] = {}
    for summary in summaries:
        key = (summary.subject_id, summary.radar_device_id, summary.night_of)
        current = by_key.get(key)
        if current is None or summary.generated_at > current.generated_at:
            by_key[key] = summary
    return sorted(by_key.values(), key=lambda item: item.night_of)


def _measurement_cohort(
    summary: RadarNightSummary,
    metric_name: TrendMetricName,
) -> MeasurementCohort:
    details = summary.explainable_metrics
    fingerprint = str(details.get("adapter_configuration_fingerprint", ""))
    if len(fingerprint) != 64:
        fingerprint = stable_hash(
            {
                "legacy_device": summary.radar_device_id,
                "configuration": fingerprint or "unknown",
            }
        )
    data_mode = str(details.get("data_mode", "live"))
    if data_mode not in {"live", "replay"}:
        data_mode = "live"
    values: dict[str, Any] = {
        "subject_id": summary.subject_id or "unbound",
        "metric_id": metric_name,
        "data_mode": data_mode,
        "device_binding_version": str(
            details.get(
                "device_binding_version",
                f"legacy-binding:{summary.radar_device_id}",
            )
        ),
        "device_measurement_domain": str(
            details.get("device_measurement_domain", "legacy-radar.v1")
        ),
        "adapter_id": str(details.get("adapter_id", "legacy-radar")),
        "adapter_version": str(details.get("adapter_version", "legacy.v1")),
        "adapter_configuration_fingerprint": fingerprint,
        "observation_schema_version": str(
            details.get(
                "observation_schema_version", "radar-night-summary.v1"
            )
        ),
        "canonical_data_version": str(
            details.get("canonical_data_version", "legacy-summary.v1")
        ),
        "producer_id": str(details.get("producer_id", "radar-trend-summary")),
        "producer_version": str(details.get("producer_version", "legacy.v1")),
        "firmware_version": details.get("firmware_version"),
        "calibration_state": str(details.get("calibration_state", "unknown")),
        "calibration_version": details.get("calibration_version"),
    }
    if metric_name in {"sleep_minutes", "in_bed_minutes"}:
        values.update(
            {
                "timezone_name": summary.timezone_name,
                "sleep_day_policy_version": str(
                    details.get("sleep_day_policy_version", "wake-date.v1")
                ),
                "boundary_policy_version": str(
                    details.get(
                        "boundary_policy_version",
                        "legacy-radar-boundary.v1",
                    )
                ),
            }
        )
    return MeasurementCohort.model_validate(values)


def _usable_summaries_for_metric(
    summaries: list[RadarNightSummary], metric_name: TrendMetricName
) -> list[RadarNightSummary]:
    return [
        item
        for item in summaries
        if item.data_quality_status != RadarDataQualityStatus.UNUSABLE
        and item.health_conclusion_allowed
        and _metric_value(item, metric_name) is not None
    ]


def _metric_value(
    summary: RadarNightSummary, metric_name: TrendMetricName
) -> float | None:
    available = summary.explainable_metrics.get("available_metrics")
    if isinstance(available, list) and metric_name not in available:
        return None
    if metric_name == "sleep_minutes":
        return _float_or_none(summary.total_sleep_minutes)
    if metric_name == "in_bed_minutes":
        return _in_bed_minutes(summary)
    if metric_name == "out_of_bed_count":
        return float(summary.out_of_bed_count)
    if metric_name == "movement_count":
        return float(summary.movement_count)
    if metric_name == "breath_rate_bpm":
        return _float_or_none(
            summary.explainable_metrics.get("mean_breath_rate_bpm")
        )
    if metric_name == "heart_rate_bpm":
        return _float_or_none(
            summary.explainable_metrics.get("mean_heart_rate_bpm")
        )
    if metric_name == "data_coverage_ratio":
        return summary.data_coverage_ratio
    return None


def _in_bed_minutes(summary: RadarNightSummary) -> float | None:
    direct = _float_or_none(summary.explainable_metrics.get("in_bed_minutes"))
    if direct is not None:
        return direct
    if summary.night_boundary_start_at and summary.night_boundary_end_at:
        boundary_minutes = (
            summary.night_boundary_end_at - summary.night_boundary_start_at
        ).total_seconds() / 60
        not_in_bed = _float_or_none(
            summary.explainable_metrics.get("not_in_bed_minutes")
        )
        if not_in_bed is not None:
            return round(max(0.0, boundary_minutes - not_in_bed), 2)
    return _float_or_none(summary.total_sleep_minutes)


def _mean(values: Any) -> float | None:
    filtered = [value for value in values if value is not None]
    return round(sum(filtered) / len(filtered), 2) if filtered else None


def _confidence(summaries: list[RadarNightSummary]) -> float:
    coverage = _mean(item.data_coverage_ratio for item in summaries) or 0
    penalty = sum(
        0.15
        for item in summaries
        if item.data_quality_status == RadarDataQualityStatus.PARTIAL
        or item.confidence_label == "low_confidence"
    ) / len(summaries)
    return round(max(0.0, min(1.0, coverage - penalty)), 2)


def _deltas(value: float, baseline: float) -> tuple[float, float | None]:
    absolute = round(value - baseline, 2)
    percent = round((absolute / baseline) * 100, 2) if baseline != 0 else None
    return absolute, percent


def _direction(
    *,
    absolute_delta: float,
    percent_delta: float | None,
    stable_delta: float,
    stable_percent: float,
) -> TrendDirection:
    if abs(absolute_delta) <= stable_delta:
        return TrendDirection.STABLE
    if percent_delta is not None and abs(percent_delta) <= stable_percent:
        return TrendDirection.STABLE
    return (
        TrendDirection.INCREASED
        if absolute_delta > 0
        else TrendDirection.DECREASED
    )


def _summary_ref(summary: RadarNightSummary) -> str:
    return summary.source_report_ref or (
        f"night-summary:{summary.radar_device_id}:{summary.night_of.isoformat()}"
    )


def _single_or_none(values: set[str]) -> str | None:
    return next(iter(values)) if len(values) == 1 else None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dedupe(items: Any) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _trend_caveats() -> list[str]:
    return [
        "Radar trend metrics are care-coordination signals only.",
        "Trend metrics require enough usable canonical night summaries.",
    ]


__all__ = [
    "METRIC_SPECS",
    "TREND_ANALYSIS_TOOL_VERSION",
    "TrendAnalysisResult",
    "TrendAnalysisTool",
    "TrendDirection",
    "TrendMetricName",
    "TrendMetricStatus",
    "TrendWindowMetric",
    "is_watch_direction",
    "metric_label",
]
