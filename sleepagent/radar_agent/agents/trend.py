from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Any, Literal

from pydantic import Field

from sleepagent.radar_agent.schemas import (
    A2AMessage,
    AgentResult,
    ContextPacket,
    EvidenceClaim,
    RadarAgentName,
    RadarAgentSchema,
    RadarDataQualityStatus,
    RadarNightSummary,
    ReviewStatus,
    RiskLevel,
)
from sleepagent.radar_agent.product_agent.cold_start import (
    MeasurementCohort,
)
from sleepagent.radar_agent.product_agent.contracts import stable_hash


TrendMetricName = Literal[
    "sleep_minutes",
    "in_bed_minutes",
    "out_of_bed_count",
    "movement_count",
    "breath_rate_bpm",
    "heart_rate_bpm",
    "data_coverage_ratio",
]


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


class TrendWindowMetric(RadarAgentSchema):
    metric_name: TrendMetricName
    window_days: Literal[7, 30, 90]
    status: TrendMetricStatus
    direction: TrendDirection = TrendDirection.INSUFFICIENT_DATA
    sample_count: int = Field(default=0, ge=0)
    required_sample_count: int = Field(default=0, ge=0)
    value: float | None = None
    baseline_value: float | None = None
    absolute_delta: float | None = None
    percent_delta: float | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class RadarTrendResult(RadarAgentSchema):
    task_id: str = Field(..., min_length=1)
    subject_id: str | None = None
    radar_device_id: str | None = None
    latest_night: date | None = None
    windows: dict[str, list[TrendWindowMetric]] = Field(default_factory=dict)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class TrendMetricSpec:
    name: TrendMetricName
    label: str
    stable_delta: float
    stable_percent: float
    higher_is_watch_signal: bool


class TrendAgent:
    name = RadarAgentName.TREND

    def __init__(
        self,
        *,
        windows: tuple[Literal[7, 30, 90], ...] = (7, 30, 90),
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

    def run(self, context: ContextPacket) -> AgentResult:
        result = self.analyze(
            task_id=context.task_context.task_id,
            summaries=context.evidence_packet.night_summaries,
        )
        return AgentResult(
            agent_name=self.name,
            claims=result.claims,
            evidence_refs=result.evidence_refs,
            confidence=min((claim.confidence for claim in result.claims), default=0),
            uncertainties=result.uncertainties,
            safety_flags=["trend_only_structured_metrics"],
            next_requests=[
                A2AMessage(
                    message_id=f"a2a:{context.task_context.task_id}:trend-to-risk",
                    sender=self.name.value,
                    receiver=RadarAgentName.RISK_SIGNAL.value,
                    task_id=context.task_context.task_id,
                    intent="assess_structured_trend_risk",
                    evidence_refs=result.evidence_refs,
                    confidence=min(
                        (claim.confidence for claim in result.claims),
                        default=0,
                    ),
                    requested_action="apply_non_diagnostic_risk_rules",
                    risk_level=max(
                        (claim.risk_level for claim in result.claims),
                        default=RiskLevel.INFO,
                        key=lambda level: {
                            RiskLevel.INFO: 0,
                            RiskLevel.UNCERTAIN: 1,
                            RiskLevel.WATCH: 2,
                            RiskLevel.ESCALATE: 3,
                            RiskLevel.URGENT_BOUNDARY: 4,
                        }[level],
                    ),
                    collaboration_round=1,
                )
            ],
            output_payload={"trend_result": result.model_dump(mode="json")},
        )

    def analyze(
        self,
        *,
        task_id: str,
        summaries: list[RadarNightSummary],
    ) -> RadarTrendResult:
        ordered = _dedupe_summaries(summaries)
        if not ordered:
            return RadarTrendResult(
                task_id=task_id,
                uncertainties=["No canonical night summaries are available for trend analysis."],
                caveats=_trend_caveats(),
            )

        latest_night = ordered[-1].night_of
        subject_ids = {summary.subject_id for summary in ordered if summary.subject_id}
        device_ids = {summary.radar_device_id for summary in ordered if summary.radar_device_id}
        if len(subject_ids) > 1:
            return RadarTrendResult(
                task_id=task_id,
                latest_night=latest_night,
                uncertainties=[
                    "Cross-subject summaries are not eligible for one trend."
                ],
                caveats=_trend_caveats(),
            )
        windows: dict[str, list[TrendWindowMetric]] = {}
        all_refs: list[str] = []
        uncertainties: list[str] = []
        baseline_start = latest_night - timedelta(days=max(self.windows) - 1)
        baseline_summaries = [
            summary
            for summary in ordered
            if latest_night - timedelta(days=180) <= summary.night_of < baseline_start
        ]

        for window_days in self.windows:
            window_start = latest_night - timedelta(days=window_days - 1)
            window_summaries = [
                summary for summary in ordered if window_start <= summary.night_of <= latest_night
            ]
            metrics = [
                self._build_metric(
                    spec=spec,
                    window_days=window_days,
                    window_summaries=window_summaries,
                    baseline_summaries=baseline_summaries,
                )
                for spec in _METRIC_SPECS
            ]
            windows[str(window_days)] = metrics
            for metric in metrics:
                all_refs.extend(metric.evidence_refs)
                if metric.status != TrendMetricStatus.COMPUTED:
                    uncertainties.append(
                        f"{window_days}d {metric.metric_name}: {metric.status.value}"
                    )

        claims = self._build_claims(
            task_id=task_id,
            windows=windows,
        )
        return RadarTrendResult(
            task_id=task_id,
            subject_id=_single_or_none(subject_ids),
            radar_device_id=_single_or_none(device_ids),
            latest_night=latest_night,
            windows=windows,
            claims=claims,
            evidence_refs=_dedupe(all_refs),
            uncertainties=_dedupe(uncertainties),
            caveats=_trend_caveats(),
        )

    def _build_metric(
        self,
        *,
        spec: TrendMetricSpec,
        window_days: Literal[7, 30, 90],
        window_summaries: list[RadarNightSummary],
        baseline_summaries: list[RadarNightSummary],
    ) -> TrendWindowMetric:
        required_samples = self.min_samples_by_window[window_days]
        usable_candidates = _usable_summaries_for_metric(
            window_summaries,
            spec.name,
        )
        target_cohort_ref = (
            _metric_cohort_ref(usable_candidates[-1], spec.name)
            if usable_candidates
            else None
        )
        usable_window = [
            item
            for item in usable_candidates
            if _metric_cohort_ref(item, spec.name) == target_cohort_ref
        ]
        evidence_refs = [_summary_ref(summary) for summary in usable_window]
        if len(usable_window) < required_samples:
            return TrendWindowMetric(
                metric_name=spec.name,
                window_days=window_days,
                status=TrendMetricStatus.INSUFFICIENT_DATA,
                direction=TrendDirection.INSUFFICIENT_DATA,
                sample_count=len(usable_window),
                required_sample_count=required_samples,
                confidence=0,
                evidence_refs=evidence_refs,
                caveats=["Sample count is below the minimum for this trend window."],
            )

        value = _mean(_metric_value(summary, spec.name) for summary in usable_window)
        usable_baseline = [
            item
            for item in _usable_summaries_for_metric(
                baseline_summaries,
                spec.name,
            )
            if _metric_cohort_ref(item, spec.name) == target_cohort_ref
        ]
        baseline_refs = [_summary_ref(summary) for summary in usable_baseline]
        if len(usable_baseline) < self.baseline_min_samples:
            # Replay and newly enrolled subjects can have a complete recent week
            # without an older personal baseline. Compare the early and late
            # halves of that window so deterministic worsening scenarios remain
            # observable, while still refusing samples below the window minimum.
            split_at = len(usable_window) // 2
            early_window = usable_window[:split_at]
            late_window = usable_window[split_at:]
            if len(early_window) >= 3 and len(late_window) >= 3:
                baseline_value = _mean(
                    _metric_value(summary, spec.name) for summary in early_window
                )
                recent_value = _mean(
                    _metric_value(summary, spec.name) for summary in late_window
                )
                if baseline_value is not None and recent_value is not None:
                    absolute_delta = round(recent_value - baseline_value, 2)
                    percent_delta = (
                        round((absolute_delta / baseline_value) * 100, 2)
                        if baseline_value != 0
                        else None
                    )
                    return TrendWindowMetric(
                        metric_name=spec.name,
                        window_days=window_days,
                        status=TrendMetricStatus.COMPUTED,
                        direction=_direction(
                            absolute_delta=absolute_delta,
                            percent_delta=percent_delta,
                            stable_delta=spec.stable_delta,
                            stable_percent=spec.stable_percent,
                        ),
                        sample_count=len(usable_window),
                        required_sample_count=required_samples,
                        value=recent_value,
                        baseline_value=baseline_value,
                        absolute_delta=absolute_delta,
                        percent_delta=percent_delta,
                        confidence=_confidence(usable_window),
                        evidence_refs=evidence_refs,
                        caveats=[
                            "Older personal baseline is unavailable; direction uses "
                            "the early versus late portions of this window."
                        ],
                    )
            return TrendWindowMetric(
                metric_name=spec.name,
                window_days=window_days,
                status=TrendMetricStatus.COMPUTED,
                direction=TrendDirection.INSUFFICIENT_DATA,
                sample_count=len(usable_window),
                required_sample_count=required_samples,
                value=value,
                confidence=_confidence(usable_window),
                evidence_refs=_dedupe(evidence_refs + baseline_refs),
                caveats=["Individual baseline is unavailable or too small."],
            )

        baseline_value = _mean(_metric_value(summary, spec.name) for summary in usable_baseline)
        if baseline_value is None or value is None:
            return TrendWindowMetric(
                metric_name=spec.name,
                window_days=window_days,
                status=TrendMetricStatus.NOT_INTERPRETABLE,
                direction=TrendDirection.NOT_INTERPRETABLE,
                sample_count=len(usable_window),
                required_sample_count=required_samples,
                confidence=0,
                evidence_refs=_dedupe(evidence_refs + baseline_refs),
                caveats=["Metric values are not interpretable for this window."],
            )

        absolute_delta = round(value - baseline_value, 2)
        percent_delta = (
            round((absolute_delta / baseline_value) * 100, 2)
            if baseline_value != 0
            else None
        )
        direction = _direction(
            absolute_delta=absolute_delta,
            percent_delta=percent_delta,
            stable_delta=spec.stable_delta,
            stable_percent=spec.stable_percent,
        )
        return TrendWindowMetric(
            metric_name=spec.name,
            window_days=window_days,
            status=TrendMetricStatus.COMPUTED,
            direction=direction,
            sample_count=len(usable_window),
            required_sample_count=required_samples,
            value=value,
            baseline_value=baseline_value,
            absolute_delta=absolute_delta,
            percent_delta=percent_delta,
            confidence=_confidence(usable_window),
            evidence_refs=_dedupe(evidence_refs + baseline_refs),
        )

    def _build_claims(
        self,
        *,
        task_id: str,
        windows: dict[str, list[TrendWindowMetric]],
    ) -> list[EvidenceClaim]:
        seven_day = windows.get("7", [])
        computable = [
            metric
            for metric in seven_day
            if metric.status == TrendMetricStatus.COMPUTED
            and metric.direction not in {
                TrendDirection.STABLE,
                TrendDirection.INSUFFICIENT_DATA,
                TrendDirection.NOT_INTERPRETABLE,
            }
            and metric.evidence_refs
        ]
        claims: list[EvidenceClaim] = []
        for index, metric in enumerate(computable, start=1):
            spec = _SPEC_BY_NAME[metric.metric_name]
            text = _claim_text(metric, spec)
            risk_level = (
                RiskLevel.WATCH
                if _watch_direction(metric, spec)
                else RiskLevel.INFO
            )
            claims.append(
                EvidenceClaim(
                    claim_id=f"trend:{task_id}:7d:{metric.metric_name}:{index}",
                    task_id=task_id,
                    text=text,
                    evidence_refs=metric.evidence_refs,
                    confidence=metric.confidence,
                    risk_level=risk_level,
                    uncertainty=None,
                    caveats=_trend_caveats(),
                    generated_by=RadarAgentName.TREND.value,
                    review_status=ReviewStatus.REVIEWED,
                )
            )
        if claims:
            return claims

        evidence_refs = [
            ref
            for metric in seven_day
            for ref in metric.evidence_refs
        ]
        if not evidence_refs:
            return []
        return [
            EvidenceClaim(
                claim_id=f"trend:{task_id}:insufficient",
                task_id=task_id,
                text="Recent radar trend cannot be judged because usable nights are insufficient.",
                evidence_refs=_dedupe(evidence_refs),
                confidence=0.1,
                risk_level=RiskLevel.UNCERTAIN,
                uncertainty="usable_night_sample_too_small",
                caveats=_trend_caveats(),
                generated_by=RadarAgentName.TREND.value,
                review_status=ReviewStatus.REVIEWED,
            )
        ]


_METRIC_SPECS: tuple[TrendMetricSpec, ...] = (
    TrendMetricSpec("sleep_minutes", "sleep duration", 20.0, 5.0, False),
    TrendMetricSpec("in_bed_minutes", "in-bed duration", 20.0, 5.0, False),
    TrendMetricSpec("out_of_bed_count", "out-of-bed count", 1.0, 20.0, True),
    TrendMetricSpec("movement_count", "movement count", 3.0, 20.0, True),
    TrendMetricSpec("breath_rate_bpm", "breathing rate", 1.5, 8.0, True),
    TrendMetricSpec("heart_rate_bpm", "heart rate", 5.0, 8.0, True),
    TrendMetricSpec("data_coverage_ratio", "data coverage", 0.05, 5.0, False),
)
_SPEC_BY_NAME = {spec.name: spec for spec in _METRIC_SPECS}


def _dedupe_summaries(summaries: list[RadarNightSummary]) -> list[RadarNightSummary]:
    by_key: dict[tuple[str | None, str, date], RadarNightSummary] = {}
    for summary in summaries:
        key = (
            summary.subject_id,
            summary.radar_device_id,
            summary.night_of,
        )
        current = by_key.get(key)
        if current is None or summary.generated_at > current.generated_at:
            by_key[key] = summary
    return sorted(by_key.values(), key=lambda item: item.night_of)


def _metric_cohort_ref(
    summary: RadarNightSummary,
    metric_name: TrendMetricName,
) -> str:
    """Project the legacy summary into the exact cold-start cohort key."""

    details = summary.explainable_metrics
    fingerprint = str(
        details.get("adapter_configuration_fingerprint", "")
    )
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
                "observation_schema_version",
                "radar-night-summary.v1",
            )
        ),
        "canonical_data_version": str(
            details.get("canonical_data_version", "legacy-summary.v1")
        ),
        "producer_id": str(
            details.get("producer_id", "radar-trend-summary")
        ),
        "producer_version": str(
            details.get("producer_version", "legacy.v1")
        ),
        "firmware_version": details.get("firmware_version"),
        # Missing calibration remains explicitly unknown; it is never promoted
        # to calibrated by a legacy compatibility projection.
        "calibration_state": str(
            details.get("calibration_state", "unknown")
        ),
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
    return MeasurementCohort.model_validate(values).cohort_ref


def _usable_summaries_for_metric(
    summaries: list[RadarNightSummary],
    metric_name: TrendMetricName,
) -> list[RadarNightSummary]:
    usable: list[RadarNightSummary] = []
    for summary in summaries:
        if summary.data_quality_status == RadarDataQualityStatus.UNUSABLE:
            continue
        if not summary.health_conclusion_allowed:
            continue
        if _metric_value(summary, metric_name) is None:
            continue
        usable.append(summary)
    return usable


def _metric_value(
    summary: RadarNightSummary,
    metric_name: TrendMetricName,
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
        return _float_or_none(summary.explainable_metrics.get("mean_breath_rate_bpm"))
    if metric_name == "heart_rate_bpm":
        return _float_or_none(summary.explainable_metrics.get("mean_heart_rate_bpm"))
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
        not_in_bed = _float_or_none(summary.explainable_metrics.get("not_in_bed_minutes"))
        if not_in_bed is not None:
            return round(max(0.0, boundary_minutes - not_in_bed), 2)
    return _float_or_none(summary.total_sleep_minutes)


def _mean(values) -> float | None:
    filtered = [value for value in values if value is not None]
    if not filtered:
        return None
    return round(sum(filtered) / len(filtered), 2)


def _confidence(summaries: list[RadarNightSummary]) -> float:
    if not summaries:
        return 0
    coverage = _mean(summary.data_coverage_ratio for summary in summaries) or 0
    quality_penalty = sum(
        0.15
        for summary in summaries
        if summary.data_quality_status == RadarDataQualityStatus.PARTIAL
        or summary.confidence_label == "low_confidence"
    ) / len(summaries)
    return round(max(0.0, min(1.0, coverage - quality_penalty)), 2)


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
    return TrendDirection.INCREASED if absolute_delta > 0 else TrendDirection.DECREASED


def _watch_direction(metric: TrendWindowMetric, spec: TrendMetricSpec) -> bool:
    if metric.direction == TrendDirection.STABLE:
        return False
    if spec.higher_is_watch_signal:
        return metric.direction == TrendDirection.INCREASED
    return metric.direction == TrendDirection.DECREASED


def _claim_text(metric: TrendWindowMetric, spec: TrendMetricSpec) -> str:
    if metric.baseline_value is None or metric.value is None:
        return f"7-day {spec.label} trend is structured but baseline comparison is unavailable."
    direction = "increased" if metric.direction == TrendDirection.INCREASED else "decreased"
    return (
        f"7-day {spec.label} {direction} versus the individual's baseline "
        f"({metric.value} vs {metric.baseline_value})."
    )


def _summary_ref(summary: RadarNightSummary) -> str:
    if summary.source_report_ref:
        return summary.source_report_ref
    return f"night-summary:{summary.radar_device_id}:{summary.night_of.isoformat()}"


def _single_or_none(values: set[str]) -> str | None:
    return next(iter(values)) if len(values) == 1 else None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dedupe(items) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item in seen:
            continue
        deduped.append(item)
        seen.add(item)
    return deduped


def _trend_caveats() -> list[str]:
    return [
        "Radar trend metrics are care-coordination signals only.",
        "Trend metrics require enough usable canonical night summaries.",
    ]


__all__ = [
    "RadarTrendResult",
    "TrendAgent",
    "TrendDirection",
    "TrendMetricName",
    "TrendMetricStatus",
    "TrendWindowMetric",
]
