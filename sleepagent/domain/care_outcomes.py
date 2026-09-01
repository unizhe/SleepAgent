"""Deterministic, non-causal care outcome evaluation contracts.

G9 records a human attestation.  This module deliberately keeps that authority
separate from device/finalization evidence and from Habit/Memory authority.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from enum import Enum
from statistics import median
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sleepagent.domain.care_actions import CareActionType, stable_hash


CARE_OUTCOME_POLICY_VERSION = "care-outcome-evaluation.v1"
CARE_OUTCOME_SCHEMA_VERSION = "care_outcome.v1"
PERSONALIZATION_RECEIPT_SCHEMA_VERSION = "personalization_effect_receipt.v1"
CARE_OUTCOME_QUEUE = "care.outcome.evaluate.v1"
CAUSAL_DISCLAIMER_ZH_CN = (
    "这是执行前后观察结果，不代表已经证明该照护行动造成了变化。"
)


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class EvaluationMode(str, Enum):
    WAKE_TIME_CONSISTENCY = "wake_time_consistency"
    INDIRECT_SLEEP_CONTEXT = "indirect_sleep_context"
    EXECUTION_ONLY = "execution_only"


class OutcomeLifecycleState(str, Enum):
    WAITING_FOR_FOLLOWUP = "waiting_for_followup"
    READY_FOR_EVALUATION = "ready_for_evaluation"
    EVALUATED = "evaluated"
    INSUFFICIENT_DATA = "insufficient_data"
    NOT_COMPARABLE = "not_comparable"
    SUPERSEDED = "superseded"


class OutcomeCategory(str, Enum):
    IMPROVED = "improved"
    STABLE = "stable"
    WORSENED = "worsened"
    INSUFFICIENT_DATA = "insufficient_data"
    NOT_COMPARABLE = "not_comparable"
    EXECUTION_ONLY = "execution_only"


class EvidenceQuality(str, Enum):
    HIGH = "high"
    MODERATE = "moderate"
    LIMITED = "limited"
    INSUFFICIENT = "insufficient"


class PersonalizationReceiptState(str, Enum):
    NO_PERSONALIZATION_CHANGE = "no_personalization_change"
    CANDIDATE_PROPOSED = "candidate_proposed"
    CANDIDATE_ACCEPTED = "candidate_accepted"
    CANDIDATE_REJECTED = "candidate_rejected"
    SUPERSEDED = "superseded"


class PersonalizationCandidateType(str, Enum):
    GOVERNED_MEMORY = "governed_memory"
    HABIT = "habit"


class ComparableMetricDefinition(FrozenContract):
    metric_id: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    window_semantics: str = Field(min_length=1)
    coverage_semantics: str = Field(min_length=1)
    source_authority: str = Field(min_length=1)
    direct_relevance: bool

    @property
    def compatibility_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.metric_id,
            self.unit,
            self.window_semantics,
            self.coverage_semantics,
            self.source_authority,
        )


class CareOutcomeEvaluationPolicy(FrozenContract):
    policy_version: str = CARE_OUTCOME_POLICY_VERSION
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_type: CareActionType
    evaluation_mode: EvaluationMode
    eligible_baseline_authority: Literal["hard_finalized"] = "hard_finalized"
    eligible_followup_authority: Literal["hard_finalized"] = "hard_finalized"
    minimum_baseline_nights: int = Field(ge=0, le=14)
    minimum_followup_nights: int = Field(ge=0, le=14)
    maximum_baseline_nights: int = Field(ge=0, le=30)
    maximum_followup_nights: int = Field(ge=0, le=30)
    observation_window_days: int = Field(ge=0, le=90)
    comparable_metrics: tuple[ComparableMetricDefinition, ...]
    minimum_coverage_ratio: float = Field(ge=0, le=1)
    stable_threshold: float = Field(ge=0)
    personalization_inference_permitted: bool
    permitted_outcomes: tuple[OutcomeCategory, ...]

    @classmethod
    def define(
        cls,
        *,
        action_type: CareActionType,
        evaluation_mode: EvaluationMode,
        minimum_baseline_nights: int,
        minimum_followup_nights: int,
        maximum_baseline_nights: int,
        maximum_followup_nights: int,
        observation_window_days: int,
        comparable_metrics: tuple[ComparableMetricDefinition, ...],
        minimum_coverage_ratio: float,
        stable_threshold: float,
        personalization_inference_permitted: bool,
        permitted_outcomes: tuple[OutcomeCategory, ...],
    ) -> "CareOutcomeEvaluationPolicy":
        material = {
            "policy_version": CARE_OUTCOME_POLICY_VERSION,
            "action_type": action_type.value,
            "evaluation_mode": evaluation_mode.value,
            "eligible_baseline_authority": "hard_finalized",
            "eligible_followup_authority": "hard_finalized",
            "minimum_baseline_nights": minimum_baseline_nights,
            "minimum_followup_nights": minimum_followup_nights,
            "maximum_baseline_nights": maximum_baseline_nights,
            "maximum_followup_nights": maximum_followup_nights,
            "observation_window_days": observation_window_days,
            "comparable_metrics": [
                item.model_dump(mode="json") for item in comparable_metrics
            ],
            "minimum_coverage_ratio": minimum_coverage_ratio,
            "stable_threshold": stable_threshold,
            "personalization_inference_permitted": (
                personalization_inference_permitted
            ),
            "permitted_outcomes": [item.value for item in permitted_outcomes],
        }
        return cls(policy_hash=stable_hash(material), **material)

    @model_validator(mode="after")
    def validate_policy_shape(self) -> "CareOutcomeEvaluationPolicy":
        if self.minimum_baseline_nights > self.maximum_baseline_nights:
            raise ValueError("minimum baseline exceeds maximum baseline")
        if self.minimum_followup_nights > self.maximum_followup_nights:
            raise ValueError("minimum follow-up exceeds maximum follow-up")
        if self.evaluation_mode is EvaluationMode.EXECUTION_ONLY:
            if self.comparable_metrics or self.minimum_followup_nights:
                raise ValueError("execution-only policy cannot require measurements")
        elif not self.comparable_metrics:
            raise ValueError("observational policy requires explicit metrics")
        if len(set(self.permitted_outcomes)) != len(self.permitted_outcomes):
            raise ValueError("outcome policy repeats an outcome")
        return self


WAKE_TIME_METRIC = ComparableMetricDefinition(
    metric_id="wake_time_local_minute",
    unit="minute_of_local_day",
    window_semantics="observed_episode_wake_instant",
    coverage_semantics="hard_finalized_episode",
    source_authority="night_episode_observed_wake",
    direct_relevance=True,
)

SLEEP_WINDOW_METRIC = ComparableMetricDefinition(
    metric_id="sleep_window_duration",
    unit="minute",
    window_semantics="episode_bed_to_wake",
    coverage_semantics="hard_finalized_episode",
    source_authority="night_episode_device_derived",
    direct_relevance=False,
)


_OUTCOME_POLICIES = {
    CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME: (
        CareOutcomeEvaluationPolicy.define(
            action_type=CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME,
            evaluation_mode=EvaluationMode.WAKE_TIME_CONSISTENCY,
            minimum_baseline_nights=2,
            minimum_followup_nights=2,
            maximum_baseline_nights=7,
            maximum_followup_nights=7,
            observation_window_days=14,
            comparable_metrics=(WAKE_TIME_METRIC,),
            minimum_coverage_ratio=0.8,
            stable_threshold=10.0,
            personalization_inference_permitted=True,
            permitted_outcomes=(
                OutcomeCategory.IMPROVED,
                OutcomeCategory.STABLE,
                OutcomeCategory.WORSENED,
                OutcomeCategory.INSUFFICIENT_DATA,
                OutcomeCategory.NOT_COMPARABLE,
            ),
        )
    ),
    # Radar cannot observe light exposure.  Sleep-window duration is retained
    # only as indirect context and cannot establish a directional outcome.
    CareActionType.RECOMMEND_MORNING_LIGHT: CareOutcomeEvaluationPolicy.define(
        action_type=CareActionType.RECOMMEND_MORNING_LIGHT,
        evaluation_mode=EvaluationMode.INDIRECT_SLEEP_CONTEXT,
        minimum_baseline_nights=1,
        minimum_followup_nights=1,
        maximum_baseline_nights=3,
        maximum_followup_nights=3,
        observation_window_days=7,
        comparable_metrics=(SLEEP_WINDOW_METRIC,),
        minimum_coverage_ratio=0.8,
        stable_threshold=0.0,
        personalization_inference_permitted=False,
        permitted_outcomes=(
            OutcomeCategory.INSUFFICIENT_DATA,
            OutcomeCategory.NOT_COMPARABLE,
        ),
    ),
    CareActionType.REQUEST_MANUAL_FOLLOW_UP: CareOutcomeEvaluationPolicy.define(
        action_type=CareActionType.REQUEST_MANUAL_FOLLOW_UP,
        evaluation_mode=EvaluationMode.EXECUTION_ONLY,
        minimum_baseline_nights=0,
        minimum_followup_nights=0,
        maximum_baseline_nights=0,
        maximum_followup_nights=0,
        observation_window_days=0,
        comparable_metrics=(),
        minimum_coverage_ratio=0.0,
        stable_threshold=0.0,
        personalization_inference_permitted=False,
        permitted_outcomes=(OutcomeCategory.EXECUTION_ONLY,),
    ),
    CareActionType.REQUEST_MORNING_REVIEW_FEEDBACK: (
        CareOutcomeEvaluationPolicy.define(
            action_type=CareActionType.REQUEST_MORNING_REVIEW_FEEDBACK,
            evaluation_mode=EvaluationMode.EXECUTION_ONLY,
            minimum_baseline_nights=0,
            minimum_followup_nights=0,
            maximum_baseline_nights=0,
            maximum_followup_nights=0,
            observation_window_days=0,
            comparable_metrics=(),
            minimum_coverage_ratio=0.0,
            stable_threshold=0.0,
            personalization_inference_permitted=False,
            permitted_outcomes=(OutcomeCategory.EXECUTION_ONLY,),
        )
    ),
}


def outcome_policy_for(
    action_type: CareActionType | str,
) -> CareOutcomeEvaluationPolicy:
    try:
        return _OUTCOME_POLICIES[CareActionType(action_type)]
    except (ValueError, KeyError) as exc:
        raise ValueError("unsupported care outcome action") from exc


class CareExecutionEvidence(FrozenContract):
    care_plan_id: str = Field(min_length=1)
    care_execution_event_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    action_type: CareActionType
    state: str
    completed_at: datetime
    execution_authority: str
    source_analysis_revision_id: str = Field(min_length=1)
    authority_valid_at_completion: bool
    plan_invalidated_before_completion: bool = False

    @model_validator(mode="after")
    def validate_completion_time(self) -> "CareExecutionEvidence":
        _require_aware(self.completed_at, "completion time")
        return self


class OutcomeMetricFact(FrozenContract):
    metric_id: str = Field(min_length=1)
    value: float
    unit: str = Field(min_length=1)
    window_semantics: str = Field(min_length=1)
    coverage_semantics: str = Field(min_length=1)
    coverage_ratio: float = Field(ge=0, le=1)
    source_authority: str = Field(min_length=1)
    observation_semantics_version: str = Field(min_length=1)
    trusted_for_analytics: bool
    ambiguity_status: str = Field(min_length=1)

    @property
    def compatibility_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.metric_id,
            self.unit,
            self.window_semantics,
            self.coverage_semantics,
            self.source_authority,
        )

    @model_validator(mode="after")
    def reject_unsafe_numbers(self) -> "OutcomeMetricFact":
        if not math.isfinite(self.value):
            raise ValueError("outcome metric must be finite")
        if self.metric_id == "average_movement":
            raise ValueError("generic movement is forbidden")
        return self


class OutcomeNightEvidence(FrozenContract):
    night_episode_id: str = Field(min_length=1)
    night_episode_revision_id: str = Field(min_length=1)
    night_episode_revision_number: int = Field(ge=1)
    finalization_revision_id: str = Field(min_length=1)
    finalization_revision_number: int = Field(ge=1)
    finalization_material_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    finalization_state: str
    is_current_finalization_revision: bool
    subject_id: str = Field(min_length=1)
    local_sleep_date: str = Field(min_length=1)
    observation_end_at: datetime
    coverage_status: str
    metrics: tuple[OutcomeMetricFact, ...]

    @model_validator(mode="after")
    def validate_evidence(self) -> "OutcomeNightEvidence":
        _require_aware(self.observation_end_at, "night observation end")
        keys = [item.compatibility_key for item in self.metrics]
        if len(keys) != len(set(keys)):
            raise ValueError("night evidence repeats a metric identity")
        return self


class OutcomeComparisonFact(FrozenContract):
    metric_id: str
    unit: str
    window_semantics: str
    baseline_value: float
    followup_value: float
    delta: float
    interpretation: str
    direct_relevance: bool


class CareOutcome(FrozenContract):
    schema_version: str = CARE_OUTCOME_SCHEMA_VERSION
    care_outcome_id: str = Field(min_length=1)
    care_plan_id: str = Field(min_length=1)
    care_execution_event_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    action_type: CareActionType
    policy_version: str
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_revision_ids: tuple[str, ...]
    baseline_revision_hashes: tuple[str, ...]
    baseline_episode_revision_ids: tuple[str, ...]
    followup_revision_ids: tuple[str, ...]
    followup_revision_hashes: tuple[str, ...]
    followup_episode_revision_ids: tuple[str, ...]
    evaluated_metrics: tuple[str, ...]
    comparison_facts: tuple[OutcomeComparisonFact, ...]
    outcome_category: OutcomeCategory
    evidence_quality: EvidenceQuality
    caveats: tuple[str, ...]
    execution_authority: Literal["human_attested"] = "human_attested"
    causal_claim: Literal[False] = False
    created_at: datetime
    evaluation_revision: int = Field(ge=1)
    semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    supersedes_care_outcome_id: str | None = None
    supersedes_personalization_receipt_id: str | None = None

    @model_validator(mode="after")
    def validate_lineage(self) -> "CareOutcome":
        _require_aware(self.created_at, "outcome creation time")
        if len(self.baseline_revision_ids) != len(self.baseline_revision_hashes):
            raise ValueError("baseline revision/hash binding mismatch")
        if len(self.baseline_revision_ids) != len(
            self.baseline_episode_revision_ids
        ):
            raise ValueError("baseline finalization/episode binding mismatch")
        if len(self.followup_revision_ids) != len(self.followup_revision_hashes):
            raise ValueError("follow-up revision/hash binding mismatch")
        if len(self.followup_revision_ids) != len(
            self.followup_episode_revision_ids
        ):
            raise ValueError("follow-up finalization/episode binding mismatch")
        if self.evaluation_revision == 1 and self.supersedes_care_outcome_id:
            raise ValueError("first outcome revision cannot supersede")
        if self.evaluation_revision > 1 and not self.supersedes_care_outcome_id:
            raise ValueError("later outcome revision must supersede")
        if self.evaluation_revision == 1 and self.supersedes_personalization_receipt_id:
            raise ValueError("first outcome revision cannot supersede a receipt")
        if (
            self.evaluation_revision > 1
            and not self.supersedes_personalization_receipt_id
        ):
            raise ValueError("later outcome revision must supersede a receipt")
        return self


class PersonalizationCandidate(FrozenContract):
    candidate_type: PersonalizationCandidateType
    candidate_id: str = Field(min_length=1)
    candidate_semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_content: dict[str, Any]
    source_evidence_refs: tuple[str, ...]
    governance_path: Literal[
        "existing_longitudinal_memory_elder_confirmation",
        "existing_habit_elder_confirmation",
    ]
    confirmation_required: Literal[True] = True
    direct_write_permitted: Literal[False] = False


class PersonalizationEffectReceipt(FrozenContract):
    schema_version: str = PERSONALIZATION_RECEIPT_SCHEMA_VERSION
    receipt_id: str = Field(min_length=1)
    care_outcome_id: str = Field(min_length=1)
    care_outcome_semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    care_plan_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    action_type: CareActionType
    observed_comparison: tuple[OutcomeComparisonFact, ...]
    personalization_relevant: bool
    candidate: PersonalizationCandidate | None = None
    policy_version: str = CARE_OUTCOME_POLICY_VERSION
    created_at: datetime
    state: PersonalizationReceiptState
    supersedes_receipt_id: str | None = None

    @model_validator(mode="after")
    def validate_candidate_state(self) -> "PersonalizationEffectReceipt":
        _require_aware(self.created_at, "receipt creation time")
        proposed = self.state is PersonalizationReceiptState.CANDIDATE_PROPOSED
        if proposed != (self.candidate is not None):
            raise ValueError("receipt candidate/state mismatch")
        return self


class CareOutcomeEvaluationDecision(FrozenContract):
    lifecycle_state: OutcomeLifecycleState
    reason_code: str
    selected_baseline: tuple[OutcomeNightEvidence, ...] = ()
    selected_followup: tuple[OutcomeNightEvidence, ...] = ()
    outcome: CareOutcome | None = None
    personalization_receipt: PersonalizationEffectReceipt | None = None


def evaluation_eligible(execution: CareExecutionEvidence) -> bool:
    return (
        execution.state == "completed"
        and execution.execution_authority == "human_attested"
        and execution.authority_valid_at_completion
        and not execution.plan_invalidated_before_completion
    )


def evaluate_care_outcome(
    execution: CareExecutionEvidence,
    evidence: tuple[OutcomeNightEvidence, ...],
    *,
    evaluated_at: datetime,
    prior_outcome: CareOutcome | None = None,
) -> CareOutcomeEvaluationDecision:
    """Select pinned evidence and deterministically evaluate one execution."""

    _require_aware(evaluated_at, "evaluation time")
    if not evaluation_eligible(execution):
        return CareOutcomeEvaluationDecision(
            lifecycle_state=OutcomeLifecycleState.INSUFFICIENT_DATA,
            reason_code="execution_not_eligible",
        )
    policy = outcome_policy_for(execution.action_type)
    if policy.evaluation_mode is EvaluationMode.EXECUTION_ONLY:
        return _materialize(
            execution,
            policy,
            (),
            (),
            OutcomeCategory.EXECUTION_ONLY,
            EvidenceQuality.LIMITED,
            (),
            ("仅记录人工确认的任务完成，不代表生理改善。",),
            evaluated_at,
            prior_outcome,
        )

    authoritative = _current_hard_finalized(execution, evidence)
    metric_id = policy.comparable_metrics[0].metric_id
    metric_bearing = tuple(
        item
        for item in authoritative
        if any(metric.metric_id == metric_id for metric in item.metrics)
    )
    baseline = tuple(
        item for item in metric_bearing
        if item.observation_end_at <= execution.completed_at
    )[-policy.maximum_baseline_nights :]
    window_end = execution.completed_at + timedelta(days=policy.observation_window_days)
    followup = tuple(
        item
        for item in metric_bearing
        if execution.completed_at < item.observation_end_at <= window_end
    )[: policy.maximum_followup_nights]

    if len(followup) < policy.minimum_followup_nights:
        if evaluated_at < window_end:
            return CareOutcomeEvaluationDecision(
                lifecycle_state=OutcomeLifecycleState.WAITING_FOR_FOLLOWUP,
                reason_code="eligible_hard_finalized_followup_not_available",
                selected_baseline=baseline,
                selected_followup=followup,
            )
        return _materialize(
            execution,
            policy,
            baseline,
            followup,
            OutcomeCategory.INSUFFICIENT_DATA,
            EvidenceQuality.INSUFFICIENT,
            (),
            ("观察窗口内可用的 HARD_FINALIZED 后续夜晚不足。",),
            evaluated_at,
            prior_outcome,
        )
    if len(baseline) < policy.minimum_baseline_nights:
        return _materialize(
            execution,
            policy,
            baseline,
            followup,
            OutcomeCategory.INSUFFICIENT_DATA,
            EvidenceQuality.INSUFFICIENT,
            (),
            ("执行前可用的 HARD_FINALIZED 基线夜晚不足。",),
            evaluated_at,
            prior_outcome,
        )

    facts, incompatibility = _comparable_facts(policy, baseline, followup)
    if incompatibility is not None:
        return _materialize(
            execution,
            policy,
            baseline,
            followup,
            OutcomeCategory.NOT_COMPARABLE,
            EvidenceQuality.INSUFFICIENT,
            (),
            (incompatibility,),
            evaluated_at,
            prior_outcome,
        )
    assert facts is not None
    baseline_facts, followup_facts = facts
    if any(
        item.coverage_ratio < policy.minimum_coverage_ratio
        for item in (*baseline_facts, *followup_facts)
    ):
        return _materialize(
            execution,
            policy,
            baseline,
            followup,
            OutcomeCategory.INSUFFICIENT_DATA,
            EvidenceQuality.INSUFFICIENT,
            (),
            ("可比较指标的数据覆盖不足。",),
            evaluated_at,
            prior_outcome,
        )

    if policy.evaluation_mode is EvaluationMode.INDIRECT_SLEEP_CONTEXT:
        comparison = _median_comparison(
            policy.comparable_metrics[0], baseline_facts, followup_facts,
            interpretation="仅作为行动后的间接睡眠观察背景。",
        )
        return _materialize(
            execution,
            policy,
            baseline,
            followup,
            OutcomeCategory.NOT_COMPARABLE,
            EvidenceQuality.LIMITED,
            (comparison,),
            ("雷达不能直接观察晨间光照行为，因此不能评估该行为是否产生变化。",),
            evaluated_at,
            prior_outcome,
        )

    baseline_variability = _circular_mad([item.value for item in baseline_facts])
    followup_variability = _circular_mad([item.value for item in followup_facts])
    delta = baseline_variability - followup_variability
    if delta >= policy.stable_threshold:
        category = OutcomeCategory.IMPROVED
        interpretation = "执行后的起床时间变异较执行前降低。"
    elif delta <= -policy.stable_threshold:
        category = OutcomeCategory.WORSENED
        interpretation = "执行后的起床时间变异较执行前增加。"
    else:
        category = OutcomeCategory.STABLE
        interpretation = "执行前后的起床时间变异差异未超过稳定阈值。"
    comparison = OutcomeComparisonFact(
        metric_id=WAKE_TIME_METRIC.metric_id,
        unit="minute_variability",
        window_semantics=WAKE_TIME_METRIC.window_semantics,
        baseline_value=round(baseline_variability, 3),
        followup_value=round(followup_variability, 3),
        delta=round(delta, 3),
        interpretation=interpretation,
        direct_relevance=True,
    )
    quality = _evidence_quality(baseline_facts, followup_facts)
    return _materialize(
        execution,
        policy,
        baseline,
        followup,
        category,
        quality,
        (comparison,),
        (CAUSAL_DISCLAIMER_ZH_CN,),
        evaluated_at,
        prior_outcome,
    )


def _current_hard_finalized(
    execution: CareExecutionEvidence,
    evidence: tuple[OutcomeNightEvidence, ...],
) -> tuple[OutcomeNightEvidence, ...]:
    current = [
        item
        for item in evidence
        if item.subject_id == execution.subject_id
        and item.finalization_state == "hard_finalized"
        and item.is_current_finalization_revision
    ]
    # One current authority per night; conflicting inputs fail closed by
    # excluding the night rather than picking a convenient revision.
    by_night: dict[str, list[OutcomeNightEvidence]] = {}
    for item in current:
        by_night.setdefault(item.night_episode_id, []).append(item)
    unambiguous = [items[0] for items in by_night.values() if len(items) == 1]
    return tuple(
        sorted(
            unambiguous,
            key=lambda item: (
                item.observation_end_at,
                item.finalization_revision_number,
                item.finalization_revision_id,
            ),
        )
    )


def _comparable_facts(
    policy: CareOutcomeEvaluationPolicy,
    baseline: tuple[OutcomeNightEvidence, ...],
    followup: tuple[OutcomeNightEvidence, ...],
) -> tuple[
    tuple[tuple[OutcomeMetricFact, ...], tuple[OutcomeMetricFact, ...]] | None,
    str | None,
]:
    definition = policy.comparable_metrics[0]
    selected: list[tuple[OutcomeMetricFact, ...]] = []
    for nights in (baseline, followup):
        group: list[OutcomeMetricFact] = []
        for night in nights:
            exact = [
                item
                for item in night.metrics
                if item.compatibility_key == definition.compatibility_key
            ]
            same_metric = [item for item in night.metrics if item.metric_id == definition.metric_id]
            if not exact:
                if same_metric:
                    return None, "指标单位、窗口、覆盖或来源权威不兼容。"
                return None, "指标语义不可比较。"
            fact = exact[0]
            if (
                not fact.trusted_for_analytics
                or fact.ambiguity_status == "legacy_ambiguous"
                or fact.metric_id == "legacy_ambiguous_movement"
                or fact.observation_semantics_version != "observation_semantics.v2"
            ):
                return None, "指标不是可信的 Observation Semantics V2 事实。"
            group.append(fact)
        selected.append(tuple(group))
    return (selected[0], selected[1]), None


def _circular_mad(values: list[float]) -> float:
    normalized = [value % 1440.0 for value in values]
    best_center = min(
        normalized,
        key=lambda center: sum(_circular_distance(value, center) for value in normalized),
    )
    return median([_circular_distance(value, best_center) for value in normalized])


def _circular_distance(left: float, right: float) -> float:
    distance = abs(left - right) % 1440.0
    return min(distance, 1440.0 - distance)


def _median_comparison(
    definition: ComparableMetricDefinition,
    baseline: tuple[OutcomeMetricFact, ...],
    followup: tuple[OutcomeMetricFact, ...],
    *,
    interpretation: str,
) -> OutcomeComparisonFact:
    before = median(item.value for item in baseline)
    after = median(item.value for item in followup)
    return OutcomeComparisonFact(
        metric_id=definition.metric_id,
        unit=definition.unit,
        window_semantics=definition.window_semantics,
        baseline_value=round(before, 3),
        followup_value=round(after, 3),
        delta=round(after - before, 3),
        interpretation=interpretation,
        direct_relevance=definition.direct_relevance,
    )


def _evidence_quality(
    baseline: tuple[OutcomeMetricFact, ...],
    followup: tuple[OutcomeMetricFact, ...],
) -> EvidenceQuality:
    count = min(len(baseline), len(followup))
    minimum_coverage = min(item.coverage_ratio for item in (*baseline, *followup))
    if count >= 5 and minimum_coverage >= 0.95:
        return EvidenceQuality.HIGH
    if count >= 3 and minimum_coverage >= 0.9:
        return EvidenceQuality.MODERATE
    return EvidenceQuality.LIMITED


def _materialize(
    execution: CareExecutionEvidence,
    policy: CareOutcomeEvaluationPolicy,
    baseline: tuple[OutcomeNightEvidence, ...],
    followup: tuple[OutcomeNightEvidence, ...],
    category: OutcomeCategory,
    quality: EvidenceQuality,
    comparisons: tuple[OutcomeComparisonFact, ...],
    caveats: tuple[str, ...],
    evaluated_at: datetime,
    prior: CareOutcome | None,
) -> CareOutcomeEvaluationDecision:
    if category not in policy.permitted_outcomes:
        raise ValueError("outcome category is not permitted by policy")
    revision = 1 if prior is None else prior.evaluation_revision + 1
    baseline_ids = tuple(item.finalization_revision_id for item in baseline)
    baseline_hashes = tuple(item.finalization_material_sha256 for item in baseline)
    baseline_episode_ids = tuple(
        item.night_episode_revision_id for item in baseline
    )
    followup_ids = tuple(item.finalization_revision_id for item in followup)
    followup_hashes = tuple(item.finalization_material_sha256 for item in followup)
    followup_episode_ids = tuple(
        item.night_episode_revision_id for item in followup
    )
    material = {
        "care_plan_id": execution.care_plan_id,
        "care_execution_event_id": execution.care_execution_event_id,
        "policy_hash": policy.policy_hash,
        "baseline": list(
            zip(baseline_ids, baseline_hashes, baseline_episode_ids)
        ),
        "followup": list(
            zip(followup_ids, followup_hashes, followup_episode_ids)
        ),
        "category": category.value,
        "comparison_facts": [item.model_dump(mode="json") for item in comparisons],
        "causal_claim": False,
    }
    semantic_hash = stable_hash(material)
    if prior is not None and prior.semantic_hash == semantic_hash:
        state = {
            OutcomeCategory.INSUFFICIENT_DATA: OutcomeLifecycleState.INSUFFICIENT_DATA,
            OutcomeCategory.NOT_COMPARABLE: OutcomeLifecycleState.NOT_COMPARABLE,
        }.get(prior.outcome_category, OutcomeLifecycleState.EVALUATED)
        return CareOutcomeEvaluationDecision(
            lifecycle_state=state,
            reason_code=f"outcome_{prior.outcome_category.value}",
            selected_baseline=baseline,
            selected_followup=followup,
            outcome=prior,
            personalization_receipt=_personalization_receipt(
                prior, policy, prior.created_at
            ),
        )
    care_outcome_id = f"care-outcome:{semantic_hash[:32]}"
    supersedes_receipt_id = (
        None
        if prior is None
        else _personalization_receipt(prior, policy, prior.created_at).receipt_id
    )
    outcome = CareOutcome(
        care_outcome_id=care_outcome_id,
        care_plan_id=execution.care_plan_id,
        care_execution_event_id=execution.care_execution_event_id,
        subject_id=execution.subject_id,
        action_type=execution.action_type,
        policy_version=policy.policy_version,
        policy_hash=policy.policy_hash,
        baseline_revision_ids=baseline_ids,
        baseline_revision_hashes=baseline_hashes,
        baseline_episode_revision_ids=baseline_episode_ids,
        followup_revision_ids=followup_ids,
        followup_revision_hashes=followup_hashes,
        followup_episode_revision_ids=followup_episode_ids,
        evaluated_metrics=tuple(item.metric_id for item in policy.comparable_metrics),
        comparison_facts=comparisons,
        outcome_category=category,
        evidence_quality=quality,
        caveats=caveats,
        created_at=evaluated_at,
        evaluation_revision=revision,
        semantic_hash=semantic_hash,
        supersedes_care_outcome_id=(None if prior is None else prior.care_outcome_id),
        supersedes_personalization_receipt_id=supersedes_receipt_id,
    )
    receipt = _personalization_receipt(outcome, policy, evaluated_at)
    state = {
        OutcomeCategory.INSUFFICIENT_DATA: OutcomeLifecycleState.INSUFFICIENT_DATA,
        OutcomeCategory.NOT_COMPARABLE: OutcomeLifecycleState.NOT_COMPARABLE,
    }.get(category, OutcomeLifecycleState.EVALUATED)
    return CareOutcomeEvaluationDecision(
        lifecycle_state=state,
        reason_code=f"outcome_{category.value}",
        selected_baseline=baseline,
        selected_followup=followup,
        outcome=outcome,
        personalization_receipt=receipt,
    )


def _personalization_receipt(
    outcome: CareOutcome,
    policy: CareOutcomeEvaluationPolicy,
    created_at: datetime,
) -> PersonalizationEffectReceipt:
    candidate: PersonalizationCandidate | None = None
    relevant = (
        policy.personalization_inference_permitted
        and outcome.outcome_category
        in {OutcomeCategory.IMPROVED, OutcomeCategory.STABLE, OutcomeCategory.WORSENED}
        and outcome.evidence_quality is not EvidenceQuality.INSUFFICIENT
    )
    if relevant:
        candidate_seed = stable_hash(
            {
                "care_plan_id": outcome.care_plan_id,
                "concept_id": "care_outcome.consistent_wake_time_episode",
            }
        )
        memory_id = f"memory:care-outcome:{candidate_seed[:32]}"
        content = {
            "candidate_id": memory_id,
            "candidate_version": 1,
            "operation": "create",
            "subject_id": outcome.subject_id,
            "memory_type": "routine",
            "concept_id": "care_outcome.consistent_wake_time_episode",
            "value_schema_id": "enum.v1",
            "value_schema_version": "1",
            "typed_value": outcome.outcome_category.value,
            "provenance_type": "accepted_evidence",
            "source_ref": f"evidence:{outcome.care_outcome_id}",
            "sensitivity_class": "sensitive_personal",
            "allowed_roles": [
                "sleep_care",
                "evidence_reasoning",
                "care_strategy",
            ],
            "allowed_purposes": [
                "personal_evidence_context",
                "care_preference_context",
            ],
            "valid_until": None,
            "retention_policy_version": "sleepagent-retention.v1",
            "explicit_user_authorization": False,
            "confirmation_required": True,
        }
        candidate_hash = stable_hash(content)
        candidate = PersonalizationCandidate(
            candidate_type=PersonalizationCandidateType.GOVERNED_MEMORY,
            candidate_id=memory_id,
            candidate_semantic_hash=candidate_hash,
            semantic_content=content,
            source_evidence_refs=(outcome.care_outcome_id,),
            governance_path="existing_longitudinal_memory_elder_confirmation",
        )
    receipt_material = {
        "care_outcome_id": outcome.care_outcome_id,
        "care_outcome_semantic_hash": outcome.semantic_hash,
        "candidate_hash": None if candidate is None else candidate.candidate_semantic_hash,
    }
    return PersonalizationEffectReceipt(
        receipt_id=f"personalization-effect:{stable_hash(receipt_material)[:32]}",
        care_outcome_id=outcome.care_outcome_id,
        care_outcome_semantic_hash=outcome.semantic_hash,
        care_plan_id=outcome.care_plan_id,
        subject_id=outcome.subject_id,
        action_type=outcome.action_type,
        observed_comparison=outcome.comparison_facts,
        personalization_relevant=relevant,
        candidate=candidate,
        created_at=created_at,
        state=(
            PersonalizationReceiptState.CANDIDATE_PROPOSED
            if candidate is not None
            else PersonalizationReceiptState.NO_PERSONALIZATION_CHANGE
        ),
        supersedes_receipt_id=outcome.supersedes_personalization_receipt_id,
    )


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


__all__ = [
    "CAUSAL_DISCLAIMER_ZH_CN",
    "CARE_OUTCOME_POLICY_VERSION",
    "CARE_OUTCOME_QUEUE",
    "CareExecutionEvidence",
    "CareOutcome",
    "CareOutcomeEvaluationDecision",
    "CareOutcomeEvaluationPolicy",
    "ComparableMetricDefinition",
    "EvaluationMode",
    "EvidenceQuality",
    "OutcomeCategory",
    "OutcomeComparisonFact",
    "OutcomeLifecycleState",
    "OutcomeMetricFact",
    "OutcomeNightEvidence",
    "PersonalizationCandidate",
    "PersonalizationCandidateType",
    "PersonalizationEffectReceipt",
    "PersonalizationReceiptState",
    "SLEEP_WINDOW_METRIC",
    "WAKE_TIME_METRIC",
    "evaluate_care_outcome",
    "evaluation_eligible",
    "outcome_policy_for",
]
