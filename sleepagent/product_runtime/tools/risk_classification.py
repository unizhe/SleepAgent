from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from sleepagent.product_runtime.contracts import (
    MultifactorSafetyInput,
    StrictContract,
)
from sleepagent.product_runtime.policies.risk import (
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


RISK_CLASSIFICATION_TOOL_VERSION = "sleepagent-risk-classification-tool.v1"


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
                quality_status=_quality_status(request),
                should_stop_sleep_trend_explanation=True,
                safety_required=True,
                urgent_required=True,
            )

        if request.safety_factors is not None:
            decision = classify_multifactor_risk(request.safety_factors)
            return RiskClassificationResult(
                risk_level=decision.risk_level,
                reason_codes=decision.reason_codes,
                source_refs=_dedupe(request.safety_factors.source_refs),
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
                source_refs=_dedupe(snapshot.source_refs),
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
            quality_status=_quality_status(request),
            quality_blocks_escalation=decision.quality_blocks_escalation,
            should_stop_sleep_trend_explanation=(
                decision.should_stop_sleep_trend_explanation
            ),
            safety_required=decision.safety_required,
            urgent_required=decision.urgent_required,
        )


def _quality_status(request: RiskClassificationInput) -> RiskQualityStatus:
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
    return _dedupe(refs)


def _dedupe(refs: tuple[str, ...] | list[str]) -> tuple[str, ...]:
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
