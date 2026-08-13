"""Product Runtime 的 canonical 工具实现。"""

from __future__ import annotations


# 合并自 tools/radar_data.py。
from datetime import date
from typing import Any, Final, Literal

from pydantic import Field

from sleepagent.runtime.contracts import StrictContract, stable_hash


class CanonicalRadarDeviceStatus(StrictContract):
    """Minimal canonical device state; no provider payload crosses this edge."""

    schema_version: Literal["canonical_radar_device_status.v1"] = (
        "canonical_radar_device_status.v1"
    )
    data_mode: Literal["live", "replay", "unknown"] = "unknown"
    offline: bool = False
    stale: bool = False
    source_refs: list[str] = Field(default_factory=list)


class CanonicalRadarEvidenceTool:
    """Validate already-persisted Product facts at the Tool boundary."""

    @staticmethod
    def read(
        arguments: dict[str, Any],
        context: Any,
    ) -> dict[str, Any]:
        if _caller_name(context) != "runtime":
            raise PermissionError(
                "canonical radar facts must be injected by runtime"
            )
        data = arguments.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("canonical radar evidence must be an object")
        requested_refs = _radar_dedupe(
            [str(item) for item in arguments.get("source_refs", [])]
        )
        snapshot_refs = set(context.fact_snapshot.source_refs)
        if not set(requested_refs).issubset(snapshot_refs):
            raise ValueError("radar evidence refs exceed FactSnapshot scope")
        if data.get("schema_version") == "product_revision_facts.v1":
            from sleepagent.domain.product_data import ProductRevisionFacts

            facts = ProductRevisionFacts.model_validate(data)
            binding = context.fact_snapshot.binding
            if not _subject_matches_binding(
                canonical_subject=facts.subject_id,
                binding_subject=binding.subject_id,
                data_mode=facts.data_mode.value,
            ):
                raise ValueError("canonical radar evidence subject mismatch")
            if (
                facts.canonical_data_version
                != context.fact_snapshot.canonical_data_version
            ):
                raise ValueError("canonical radar evidence version mismatch")
            if not requested_refs or tuple(requested_refs) != (
                facts.agent_source_refs()
            ):
                raise ValueError(
                    "canonical radar evidence refs must match Product facts"
                )
            scope = context.fact_snapshot.source_scope
            local_sleep_date = date.fromisoformat(facts.local_sleep_date)
            if (
                scope.date_start is None
                or scope.date_end is None
                or not scope.date_start <= local_sleep_date <= scope.date_end
            ):
                raise ValueError(
                    "canonical radar evidence date exceeds FactSnapshot scope"
                )
            data = facts.agent_night_evidence()
        return {
            "data": data,
            "source_refs": requested_refs[:50],
        }

    @staticmethod
    def assess_quality(
        arguments: dict[str, Any],
        context: Any,
    ) -> dict[str, Any]:
        if _caller_name(context) != "runtime":
            raise PermissionError(
                "canonical quality facts must be injected by runtime"
            )
        data = arguments.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("quality assessment must be an object")
        requested_refs = _radar_dedupe(
            [str(item) for item in arguments.get("source_refs", [])]
        )
        if not set(requested_refs).issubset(
            set(context.fact_snapshot.source_refs)
        ):
            raise ValueError("quality refs exceed FactSnapshot scope")
        coverage = float(
            data.get("coverage_ratio", arguments.get("coverage_ratio", 0))
        )
        if data.get("schema_version") == (
            "deterministic_quality_assessment.v1"
        ):
            policy_version = str(data.get("policy_version", ""))
            if not policy_version:
                raise ValueError("pinned quality assessment requires policy_version")
            sufficient = data.get("data_sufficiency") == "sufficient"
            usable = bool(
                sufficient
                and data.get("quality_state") != "data_insufficient"
                and not data.get("stale", False)
                and not data.get("offline", False)
                and not data.get("clock_invalid", False)
            )
            return {
                "coverage_ratio": coverage,
                "usable": usable,
                "quality_state": data.get("quality_state"),
                "data_sufficiency": data.get("data_sufficiency"),
                "policy_version": policy_version,
                "reason_codes": list(data.get("reason_codes", [])),
                "source_refs": requested_refs[:50],
            }
        return {
            "coverage_ratio": coverage,
            "usable": coverage >= 0.6,
            "source_refs": requested_refs[:50],
        }

    @staticmethod
    def read_device_status(
        arguments: dict[str, Any],
        context: Any,
    ) -> dict[str, Any]:
        if _caller_name(context) != "runtime":
            raise PermissionError(
                "canonical device status must be injected by runtime"
            )
        data = arguments.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("canonical device status must be an object")
        requested_refs = _radar_dedupe(
            [str(item) for item in arguments.get("source_refs", [])]
        )
        if not set(requested_refs).issubset(
            set(context.fact_snapshot.source_refs)
        ):
            raise ValueError("device status refs exceed FactSnapshot scope")
        return CanonicalRadarDeviceStatus.model_validate(
            {**data, "source_refs": requested_refs[:50]}
        ).model_dump(mode="json")


def _caller_name(context: Any) -> str:
    caller = context.caller
    return caller.value if hasattr(caller, "value") else str(caller)


def _subject_matches_binding(
    *,
    canonical_subject: str,
    binding_subject: str,
    data_mode: str | None = None,
) -> bool:
    if not canonical_subject:
        return False
    if binding_subject == canonical_subject:
        return True
    if data_mode is not None:
        expected = stable_hash(
            {"data_mode": data_mode, "subject_id": canonical_subject}
        )[:32]
        if binding_subject == f"subject:{expected}":
            return True
    marker = "::subject::"
    if marker not in binding_subject:
        return False
    return binding_subject.split(marker, 1)[1] == canonical_subject


def _radar_dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


__all__ = [
    "CanonicalRadarDeviceStatus",
    "CanonicalRadarEvidenceTool",
]


# 合并自 tools/risk_classification.py。
from typing import Literal

from pydantic import Field, model_validator

from sleepagent.runtime.contracts import (
    MultifactorSafetyInput,
    StrictContract,
)
from sleepagent.runtime.policies import (
    DeterministicDataSufficiency,
    DeterministicRiskFacts,
    DeterministicRiskState,
    RISK_POLICY_VERSION,
    RiskClassificationLevel,
    RiskQualityStatus,
    StructuredQualityStatus,
    StructuredRiskFacts,
    TrendSignalLevel,
    classify_deterministic_risk,
    classify_multifactor_risk,
    classify_structured_risk,
    match_urgent_boundary,
)


RISK_CLASSIFICATION_TOOL_VERSION: Final = "sleepagent-risk-classification-tool.v1"


class RiskObservation(StrictContract):
    """Minimal canonical facts needed by the structured risk policy."""

    quality_status: StructuredQualityStatus = "good"
    confidence_label: str = Field(default="normal", min_length=1)
    health_conclusion_allowed: bool = True
    abnormal_reading_count: int = Field(default=0, ge=0)
    vital_fluctuation_count: int = Field(default=0, ge=0)
    out_of_bed_count: int = Field(default=0, ge=0)
    movement_count: int = Field(default=0, ge=0)
    source_refs: tuple[str, ...] = ()


class TrendRiskSignal(StrictContract):
    """Accepted, source-bound trend classification without an Agent payload."""

    risk_level: TrendSignalLevel
    confidence: float = Field(default=0, ge=0, le=1)
    source_refs: tuple[str, ...] = ()


class DeterministicRiskSnapshot(StrictContract):
    """Exact-revision deterministic risk state produced by the domain layer."""

    risk_state: DeterministicRiskState
    data_sufficiency: DeterministicDataSufficiency = "unknown"
    health_escalation_allowed: bool = False
    reason_codes: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()


class RiskClassificationInput(StrictContract):
    text_inputs: tuple[str, ...] = ()
    observation: RiskObservation | None = None
    trend_signals: tuple[TrendRiskSignal, ...] = ()
    safety_factors: MultifactorSafetyInput | None = None
    deterministic_snapshot: DeterministicRiskSnapshot | None = None

    @model_validator(mode="after")
    def require_one_risk_fact_mode(self) -> "RiskClassificationInput":
        mode_count = sum(
            (
                self.safety_factors is not None,
                self.deterministic_snapshot is not None,
                self.observation is not None or bool(self.trend_signals),
            )
        )
        if mode_count > 1:
            raise ValueError("use only one risk fact mode per classification")
        return self


class RiskClassificationResult(StrictContract):
    """Side-effect-free risk result for Runtime and SafetyReview consumption."""

    tool_version: Literal[
        "sleepagent-risk-classification-tool.v1"
    ] = RISK_CLASSIFICATION_TOOL_VERSION
    policy_version: Literal["sleepagent-risk-policy.v1"] = RISK_POLICY_VERSION
    risk_level: RiskClassificationLevel
    reason_codes: tuple[str, ...]
    source_refs: tuple[str, ...] = ()
    quality_status: RiskQualityStatus
    quality_blocks_escalation: bool = False
    should_stop_sleep_trend_explanation: bool = False
    safety_required: bool = False
    urgent_required: bool = False


class RiskClassificationTool:
    """Execute deterministic risk policies without claims or side effects."""

    def classify(
        self,
        request: RiskClassificationInput,
    ) -> RiskClassificationResult:
        if type(request) is not RiskClassificationInput:
            raise TypeError(
                "RiskClassificationTool requires RiskClassificationInput"
            )

        urgent = match_urgent_boundary(request.text_inputs)
        if urgent is not None:
            return RiskClassificationResult(
                risk_level=RiskClassificationLevel.URGENT_BOUNDARY,
                reason_codes=("urgent_text_boundary",),
                source_refs=(urgent.evidence_ref,),
                quality_status=_risk_quality_status(request),
                should_stop_sleep_trend_explanation=True,
                safety_required=True,
                urgent_required=True,
            )

        if request.safety_factors is not None:
            decision = classify_multifactor_risk(request.safety_factors)
            return RiskClassificationResult(
                risk_level=decision.risk_level,
                reason_codes=decision.reason_codes,
                source_refs=_risk_dedupe(request.safety_factors.source_refs),
                quality_status=request.safety_factors.data_quality.value,
                quality_blocks_escalation=(
                    decision.quality_blocks_escalation
                ),
                should_stop_sleep_trend_explanation=(
                    decision.should_stop_sleep_trend_explanation
                ),
                safety_required=decision.safety_required,
                urgent_required=decision.urgent_required,
            )

        if request.deterministic_snapshot is not None:
            snapshot = request.deterministic_snapshot
            decision = classify_deterministic_risk(
                DeterministicRiskFacts(
                    risk_state=snapshot.risk_state,
                    data_sufficiency=snapshot.data_sufficiency,
                    health_escalation_allowed=(
                        snapshot.health_escalation_allowed
                    ),
                    reason_codes=snapshot.reason_codes,
                )
            )
            return RiskClassificationResult(
                risk_level=decision.risk_level,
                reason_codes=decision.reason_codes,
                source_refs=_risk_dedupe(snapshot.source_refs),
                quality_status=(
                    "good"
                    if snapshot.data_sufficiency == "sufficient"
                    else (
                        "partial"
                        if snapshot.data_sufficiency == "partial"
                        else "unusable"
                    )
                ),
                quality_blocks_escalation=(
                    decision.quality_blocks_escalation
                ),
                should_stop_sleep_trend_explanation=(
                    decision.should_stop_sleep_trend_explanation
                ),
                safety_required=decision.safety_required,
                urgent_required=decision.urgent_required,
            )

        observation = request.observation
        decision = classify_structured_risk(
            StructuredRiskFacts(
                summary_available=observation is not None,
                quality_status=(
                    observation.quality_status
                    if observation is not None
                    else "unusable"
                ),
                confidence_label=(
                    observation.confidence_label
                    if observation is not None
                    else "not_interpretable"
                ),
                health_conclusion_allowed=(
                    observation.health_conclusion_allowed
                    if observation is not None
                    else False
                ),
                abnormal_reading_count=(
                    observation.abnormal_reading_count
                    if observation is not None
                    else 0
                ),
                vital_fluctuation_count=(
                    observation.vital_fluctuation_count
                    if observation is not None
                    else 0
                ),
                out_of_bed_count=(
                    observation.out_of_bed_count
                    if observation is not None
                    else 0
                ),
                movement_count=(
                    observation.movement_count
                    if observation is not None
                    else 0
                ),
                trend_signal_levels=tuple(
                    signal.risk_level for signal in request.trend_signals
                ),
            )
        )
        return RiskClassificationResult(
            risk_level=decision.risk_level,
            reason_codes=decision.reason_codes,
            source_refs=_structured_source_refs(request),
            quality_status=_risk_quality_status(request),
            quality_blocks_escalation=decision.quality_blocks_escalation,
            should_stop_sleep_trend_explanation=(
                decision.should_stop_sleep_trend_explanation
            ),
            safety_required=decision.safety_required,
            urgent_required=decision.urgent_required,
        )


def _risk_quality_status(request: RiskClassificationInput) -> RiskQualityStatus:
    if request.observation is not None:
        return request.observation.quality_status
    if request.safety_factors is not None:
        return request.safety_factors.data_quality.value
    return "missing"


def _structured_source_refs(
    request: RiskClassificationInput,
) -> tuple[str, ...]:
    refs: list[str] = []
    if request.observation is not None:
        refs.extend(request.observation.source_refs)
    refs.extend(
        ref for signal in request.trend_signals for ref in signal.source_refs
    )
    return _risk_dedupe(refs)


def _risk_dedupe(refs: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref for ref in refs if ref))


__all__ = [
    "RISK_CLASSIFICATION_TOOL_VERSION",
    "DeterministicRiskSnapshot",
    "RiskClassificationInput",
    "RiskClassificationLevel",
    "RiskClassificationResult",
    "RiskClassificationTool",
    "RiskObservation",
    "TrendRiskSignal",
]


# 合并自 tools/trend_analysis.py。
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Any, Literal

from pydantic import Field

from sleepagent.runtime.cold_start import MeasurementCohort
from sleepagent.runtime.contracts import StrictContract, stable_hash
from sleepagent.runtime.schemas import (
    RadarDataQualityStatus,
    RadarNightSummary,
)


TREND_ANALYSIS_TOOL_VERSION: Final = "sleepagent-trend-analysis-tool.v1"

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
        ordered = _trend_dedupe_summaries(summaries)
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
            source_refs=_trend_dedupe(source_refs),
            uncertainties=_trend_dedupe(uncertainties),
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
            return TrendWindowMetric.model_validate(
                {
                    **common,
                    "status": TrendMetricStatus.INSUFFICIENT_DATA,
                    "direction": TrendDirection.INSUFFICIENT_DATA,
                    "evidence_refs": evidence_refs,
                    "caveats": [
                    "Sample count is below the minimum for this trend window."
                    ],
                }
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
                    return TrendWindowMetric.model_validate(
                        {
                        **common,
                        "status": TrendMetricStatus.COMPUTED,
                        "direction": _direction(
                            absolute_delta=absolute_delta,
                            percent_delta=percent_delta,
                            stable_delta=spec.stable_delta,
                            stable_percent=spec.stable_percent,
                        ),
                        "value": recent_value,
                        "baseline_value": baseline_value,
                        "absolute_delta": absolute_delta,
                        "percent_delta": percent_delta,
                        "confidence": _confidence(usable_window),
                        "evidence_refs": evidence_refs,
                        "caveats": [
                            "Older personal baseline is unavailable; direction "
                            "uses the early versus late portions of this window."
                        ],
                        }
                    )
            return TrendWindowMetric.model_validate(
                {
                    **common,
                    "status": TrendMetricStatus.COMPUTED,
                    "direction": TrendDirection.INSUFFICIENT_DATA,
                    "value": value,
                    "confidence": _confidence(usable_window),
                    "evidence_refs": _trend_dedupe([*evidence_refs, *baseline_refs]),
                    "caveats": ["Individual baseline is unavailable or too small."],
                }
            )

        baseline_value = _mean(
            _metric_value(item, spec.name) for item in usable_baseline
        )
        if baseline_value is None or value is None:
            return TrendWindowMetric.model_validate(
                {
                    **common,
                    "status": TrendMetricStatus.NOT_INTERPRETABLE,
                    "direction": TrendDirection.NOT_INTERPRETABLE,
                    "evidence_refs": _trend_dedupe([*evidence_refs, *baseline_refs]),
                    "caveats": ["Metric values are not interpretable for this window."],
                }
            )
        absolute_delta, percent_delta = _deltas(value, baseline_value)
        return TrendWindowMetric.model_validate(
            {
            **common,
            "status": TrendMetricStatus.COMPUTED,
            "direction": _direction(
                absolute_delta=absolute_delta,
                percent_delta=percent_delta,
                stable_delta=spec.stable_delta,
                stable_percent=spec.stable_percent,
            ),
            "value": value,
            "baseline_value": baseline_value,
            "absolute_delta": absolute_delta,
            "percent_delta": percent_delta,
            "confidence": _confidence(usable_window),
            "evidence_refs": _trend_dedupe([*evidence_refs, *baseline_refs]),
            }
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


def _trend_dedupe_summaries(
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


def _trend_dedupe(items: Any) -> list[str]:
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


# 合并自 tools/knowledge_retrieval.py。
from sleepagent.runtime.knowledge import (
    ReviewedKnowledgeQuery,
    ReviewedKnowledgeResult,
    ReviewedKnowledgeService,
    default_reviewed_knowledge_service,
)


KNOWLEDGE_RETRIEVAL_TOOL_VERSION: Final = "sleepagent-knowledge-retrieval-tool.v1"


class KnowledgeRetrievalResult(ReviewedKnowledgeResult):
    tool_version: str = KNOWLEDGE_RETRIEVAL_TOOL_VERSION


class KnowledgeRetrievalTool:
    """Read reviewed knowledge through a typed, provider-neutral boundary."""

    def __init__(self, service: ReviewedKnowledgeService | None = None) -> None:
        self._service = service or default_reviewed_knowledge_service()

    def retrieve(self, request: ReviewedKnowledgeQuery) -> KnowledgeRetrievalResult:
        result = self._service.retrieve(request)
        return KnowledgeRetrievalResult.model_validate(result.model_dump(mode="python"))


__all__ = [
    "KNOWLEDGE_RETRIEVAL_TOOL_VERSION",
    "KnowledgeRetrievalResult",
    "KnowledgeRetrievalTool",
    "ReviewedKnowledgeQuery",
    "ReviewedKnowledgeResult",
]


# 合并自 tools/care_coordination.py。
from typing import Literal

from pydantic import Field, model_validator

from sleepagent.runtime.contracts import (
    StrictContract,
)
from sleepagent.runtime.policies import (
    CareEscalationPolicy,
    CareRoutingDecision,
    RiskLevelValue,
)
CARE_COORDINATION_TOOL_VERSION: Final = "sleepagent-care-coordination-tool.v1"


class CareRiskReceiptBinding(StrictContract):
    """Accepted Risk Tool decision made visible to Care coordination."""

    risk_receipt_ref: str = Field(..., min_length=1)
    risk_level: Literal[
        "normal",
        "watch",
        "escalate",
        "uncertain",
        "urgent_boundary",
    ]
    quality_status: Literal[
        "good",
        "partial",
        "unusable",
        "missing",
        "usable",
        "limited",
    ]
    urgent_required: bool = False
    source_refs: tuple[str, ...] = ()


class CareCoordinationPolicyRequest(StrictContract):
    """Runtime-bound accepted Evidence and Risk decisions for Care policy."""

    coordination_policy_ref: str = Field(..., min_length=1)
    accepted_evidence_ref: str = Field(..., min_length=1)
    accepted_evidence_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    risk_decisions: tuple[CareRiskReceiptBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def bind_accepted_inputs(self) -> "CareCoordinationPolicyRequest":
        if not self.accepted_evidence_ref.endswith(
            f":{self.accepted_evidence_hash}"
        ):
            raise ValueError(
                "Care coordination Evidence reference does not bind its hash"
            )
        refs = [item.risk_receipt_ref for item in self.risk_decisions]
        if len(refs) != len(set(refs)):
            raise ValueError("Care coordination Risk receipts must be unique")
        return self


class CareCoordinationPolicyResult(StrictContract):
    tool_version: Literal["sleepagent-care-coordination-tool.v1"] = (
        CARE_COORDINATION_TOOL_VERSION
    )
    coordination_policy_ref: str = Field(..., min_length=1)
    accepted_evidence_ref: str = Field(..., min_length=1)
    accepted_evidence_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    risk_receipt_refs: tuple[str, ...] = Field(min_length=1)
    family_notification_requires_candidate: Literal[True] = True
    routing: CareRoutingDecision
    source_refs: list[str] = Field(max_length=50)

    @model_validator(mode="after")
    def bind_result_sources(self) -> "CareCoordinationPolicyResult":
        if not self.accepted_evidence_ref.endswith(
            f":{self.accepted_evidence_hash}"
        ):
            raise ValueError(
                "Care coordination result does not bind accepted Evidence"
            )
        required_prefix = list(
            dict.fromkeys(
                [
                    self.coordination_policy_ref,
                    self.accepted_evidence_ref,
                    *self.risk_receipt_refs,
                ]
            )
        )
        if self.source_refs[: len(required_prefix)] != required_prefix:
            raise ValueError(
                "Care coordination sources must preserve policy, Evidence, "
                "and Risk receipt bindings"
            )
        return self


class CareCoordinationTool:
    """Read deterministic coordination policy without executing an action."""

    def read_policy(
        self,
        request: CareCoordinationPolicyRequest,
    ) -> CareCoordinationPolicyResult:
        if type(request) is not CareCoordinationPolicyRequest:
            raise TypeError(
                "CareCoordinationTool requires CareCoordinationPolicyRequest"
            )
        risk_level = _risk_level(request.risk_decisions)
        data_quality = _quality_status(request.risk_decisions)
        routing = CareEscalationPolicy().evaluate(
            risk_level=risk_level,
            data_quality_status=data_quality,
        )
        risk_receipt_refs = tuple(
            item.risk_receipt_ref for item in request.risk_decisions
        )
        refs = list(
            dict.fromkeys(
                [
                    request.coordination_policy_ref,
                    request.accepted_evidence_ref,
                    *risk_receipt_refs,
                    *(
                        ref
                        for decision in request.risk_decisions
                        for ref in decision.source_refs
                    ),
                ]
            )
        )
        return CareCoordinationPolicyResult(
            coordination_policy_ref=request.coordination_policy_ref,
            accepted_evidence_ref=request.accepted_evidence_ref,
            accepted_evidence_hash=request.accepted_evidence_hash,
            risk_receipt_refs=risk_receipt_refs,
            routing=routing,
            source_refs=refs,
        )


def _risk_level(
    values: tuple[CareRiskReceiptBinding, ...],
) -> RiskLevelValue:
    if any(
        item.urgent_required or item.risk_level == "urgent_boundary"
        for item in values
    ):
        return "urgent_boundary"
    if any(item.risk_level == "escalate" for item in values):
        return "escalate"
    if any(item.risk_level == "watch" for item in values):
        return "watch"
    if all(item.risk_level == "normal" for item in values):
        return "info"
    return "uncertain"


def _quality_status(values: tuple[CareRiskReceiptBinding, ...]) -> str:
    qualities = {item.quality_status for item in values}
    if qualities.intersection({"unusable", "missing"}):
        return "unusable"
    if qualities.intersection({"partial", "limited"}):
        return "partial"
    return "good"


__all__ = [
    "CARE_COORDINATION_TOOL_VERSION",
    "CareCoordinationPolicyRequest",
    "CareCoordinationPolicyResult",
    "CareRiskReceiptBinding",
    "CareCoordinationTool",
]


# 合并自 tools/artifact_rendering.py。
from typing import Literal, TypeAlias

from pydantic import Field, model_validator

from sleepagent.runtime.contracts import (
    AgentId,
    EvidencePacket as ProductEvidencePacket,
    StrictContract,
    stable_hash,
)
from sleepagent.runtime.reports import build_role_report_templates
from sleepagent.runtime.schemas import (
    ContextPacket,
    EvidenceLedger,
    ReviewStatus,
    RiskLevel,
    RoleReportArtifact,
)


ARTIFACT_RENDERING_TOOL_VERSION: Final = "sleepagent-artifact-rendering-tool.v1"

AudienceRole: TypeAlias = Literal["elder", "family", "doctor"]

ROLE_REPORT_SAFETY_NOTICES = (
    "本报告由 AI 辅助整理，内容来自结构化证据。",
    "本报告仅用于睡眠健康观察。",
    "本报告不构成临床诊断或医疗建议。",
    "本项目不宣称 HIPAA/FDA、医疗器械或临床诊断合规。",
)

_RISK_RANK = {
    RiskLevel.INFO: 0,
    RiskLevel.UNCERTAIN: 1,
    RiskLevel.WATCH: 2,
    RiskLevel.ESCALATE: 3,
    RiskLevel.URGENT_BOUNDARY: 4,
}


class ArtifactRenderRequest(StrictContract):
    """Exact deterministic inputs for one audience-specific report view."""

    context: ContextPacket
    evidence_ledger: EvidenceLedger
    audience_role: AudienceRole

    @model_validator(mode="after")
    def require_exact_context_ledger(self) -> "ArtifactRenderRequest":
        embedded = self.context.evidence_packet.evidence_ledger
        if embedded is None or embedded != self.evidence_ledger:
            raise ValueError(
                "Artifact rendering requires the exact EvidenceLedger from Context."
            )
        if self.context.task_context.task_id != self.evidence_ledger.task_id:
            raise ValueError("Context and EvidenceLedger task identities differ.")
        rag = self.context.rag_context
        if any(
            (
                rag.chunk_ids,
                rag.citation_ids,
                rag.snippets,
                rag.caveats,
                rag.source_metadata,
            )
        ):
            raise ValueError(
                "Artifact RAG Context requires an exact accepted Knowledge "
                "receipt binding."
            )
        if self.evidence_ledger.review_status is not ReviewStatus.REVIEWED:
            raise ValueError("Artifact rendering requires a reviewed EvidenceLedger.")
        if any(
            claim.review_status is not ReviewStatus.REVIEWED
            for claim in self.evidence_ledger.claims
        ):
            raise ValueError("Artifact rendering requires reviewed claims.")
        if any(
            claim.task_id != self.evidence_ledger.task_id
            for claim in self.evidence_ledger.claims
        ):
            raise ValueError("Artifact claim task identity differs from its ledger.")
        if any(
            claim.generated_by != AgentId.EVIDENCE_REASONING.value
            for claim in self.evidence_ledger.claims
        ):
            raise ValueError(
                "Artifact claims require the canonical EvidenceReasoning owner."
            )
        if self.evidence_ledger.raw_evidence_refs:
            raise ValueError("Artifact rendering cannot consume raw evidence refs.")
        canonical_refs = set(self.evidence_ledger.canonical_evidence_refs)
        if any(
            not set(claim.evidence_refs).issubset(canonical_refs)
            for claim in self.evidence_ledger.claims
        ):
            raise ValueError("Artifact claims exceed canonical Evidence refs.")
        if any(
            entry.evidence_ref is not None
            and entry.evidence_ref not in canonical_refs
            for entry in self.evidence_ledger.questionnaire_entries
        ):
            raise ValueError("Artifact questionnaire exceeds canonical Evidence refs.")
        if any(
            item.review_status is not ReviewStatus.REVIEWED
            for item in self.evidence_ledger.supplementary_documents
        ):
            raise ValueError("Artifact supplementary documents must be reviewed.")
        declared_risk = RiskLevel(
            str(
                self.evidence_ledger.derived_metrics.get(
                    "risk_level",
                    RiskLevel.INFO.value,
                )
            )
        )
        claim_risk = max(
            (claim.risk_level for claim in self.evidence_ledger.claims),
            key=lambda item: _RISK_RANK[item],
            default=RiskLevel.INFO,
        )
        if _RISK_RANK[declared_risk] < _RISK_RANK[claim_risk]:
            raise ValueError("Artifact risk is below the reviewed claim risk floor.")
        return self


class ArtifactRenderResult(StrictContract):
    """Read-only rendering result; publication remains a Runtime responsibility."""

    tool_version: str = ARTIFACT_RENDERING_TOOL_VERSION
    audience_role: AudienceRole
    artifact: RoleReportArtifact
    source_refs: list[str]
    committed: Literal[False] = False
    exported: Literal[False] = False

    @model_validator(mode="after")
    def bind_result_to_one_audience(self) -> "ArtifactRenderResult":
        if self.artifact.role != self.audience_role:
            raise ValueError("Rendered artifact audience mismatch.")
        if self.source_refs != self.artifact.source_refs:
            raise ValueError("Render source_refs must come from the rendered artifact.")
        return self


class ProductArtifactBasisRequest(StrictContract):
    """Accepted Product Evidence used to prepare one role-material basis."""

    episode_id: str = Field(..., min_length=1)
    accepted_evidence_ref: str = Field(..., min_length=1)
    accepted_evidence_hash: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
    )
    evidence_packet: ProductEvidencePacket
    audience_role: AudienceRole

    @model_validator(mode="after")
    def bind_accepted_evidence(self) -> "ProductArtifactBasisRequest":
        if not self.accepted_evidence_ref.endswith(
            f":{self.accepted_evidence_hash}"
        ):
            raise ValueError(
                "artifact Evidence reference does not bind its accepted hash"
            )
        return self


class ProductArtifactBasisResult(StrictContract):
    """Prepared, non-published material basis for SleepCare expression.

    This result deliberately does not claim that a user-facing artifact has
    already been rendered.  SleepCare's versioned role-material Skill owns the
    final expression; publication remains a Runtime responsibility.
    """

    schema_version: Literal["product_artifact_basis.v1"] = (
        "product_artifact_basis.v1"
    )
    artifact_basis_id: str = Field(..., min_length=1)
    episode_id: str = Field(..., min_length=1)
    audience_role: AudienceRole
    accepted_evidence_ref: str = Field(..., min_length=1)
    accepted_evidence_hash: str = Field(..., min_length=64, max_length=64)
    claim_refs: list[str] = Field(max_length=30)
    evidence_refs: list[str] = Field(max_length=49)
    source_refs: list[str] = Field(max_length=50)
    basis_prepared: Literal[True] = True
    rendered: Literal[False] = False
    committed: Literal[False] = False
    exported: Literal[False] = False

    @model_validator(mode="after")
    def bind_source_projection(self) -> "ProductArtifactBasisResult":
        if self.source_refs != [
            self.accepted_evidence_ref,
            *self.evidence_refs,
        ]:
            raise ValueError(
                "artifact basis sources must bind accepted Evidence exactly"
            )
        return self


class ArtifactRenderingTool:
    """Render one role artifact from an exact Context/EvidenceLedger pair.

    The provider-neutral renderer owns the deterministic artifact structure.
    This Tool selects one requested audience and returns it without persistence,
    export, publication, model invocation, or Agent identity.
    """

    def render(self, request: ArtifactRenderRequest) -> ArtifactRenderResult:
        if type(request) is not ArtifactRenderRequest:
            raise TypeError("ArtifactRenderingTool requires ArtifactRenderRequest")

        risk = RiskLevel(
            str(
                request.evidence_ledger.derived_metrics.get(
                    "risk_level",
                    RiskLevel.INFO.value,
                )
            )
        )
        templates = build_role_report_templates(
            context=request.context,
            ledger=request.evidence_ledger,
            risk=risk,
            safety_notices=list(ROLE_REPORT_SAFETY_NOTICES),
        )
        artifact = next(
            item for item in templates if item.role == request.audience_role
        )
        return ArtifactRenderResult(
            audience_role=request.audience_role,
            artifact=artifact,
            source_refs=list(artifact.source_refs),
        )

    def prepare_product_basis(
        self,
        request: ProductArtifactBasisRequest,
    ) -> ProductArtifactBasisResult:
        if type(request) is not ProductArtifactBasisRequest:
            raise TypeError(
                "ArtifactRenderingTool requires ProductArtifactBasisRequest"
            )
        claim_refs = [claim.claim_id for claim in request.evidence_packet.claims]
        evidence_refs = list(
            dict.fromkeys(
                ref
                for claim in request.evidence_packet.claims
                for ref in claim.evidence_refs
            )
        )
        material = {
            "episode_id": request.episode_id,
            "audience_role": request.audience_role,
            "accepted_evidence_ref": request.accepted_evidence_ref,
            "accepted_evidence_hash": request.accepted_evidence_hash,
            "claim_refs": claim_refs,
            "evidence_refs": evidence_refs,
        }
        return ProductArtifactBasisResult.model_validate(
            {
                "artifact_basis_id": f"artifact-basis:{stable_hash(material)[:24]}",
                **material,
                "source_refs": [request.accepted_evidence_ref, *evidence_refs],
            }
        )


__all__ = [
    "ARTIFACT_RENDERING_TOOL_VERSION",
    "ArtifactRenderRequest",
    "ArtifactRenderResult",
    "ArtifactRenderingTool",
    "AudienceRole",
    "ROLE_REPORT_SAFETY_NOTICES",
    "ProductArtifactBasisRequest",
    "ProductArtifactBasisResult",
]
