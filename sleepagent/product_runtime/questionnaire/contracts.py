from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from sleepagent.product_runtime.schemas import RadarAgentSchema


class HabitQuestionTrigger(str, Enum):
    OPTIONAL_LIGHT_INTAKE = "optional_light_intake"
    EXPLICIT_HABIT_QUESTION = "explicit_habit_question"
    DECISION_RELEVANT_GAP = "decision_relevant_gap"
    STALE_FACT_NEEDED = "stale_fact_needed"
    SOURCE_CONFLICT = "source_conflict"
    EXPLICIT_PROFILE_REVIEW = "explicit_profile_review"


class HabitAnswerType(str, Enum):
    CHOICE = "choice"
    SCALE = "scale"
    BOUNDED_NUMBER = "bounded_number"
    SHORT_TEXT = "short_text"


class HabitRespondentRule(str, Enum):
    ELDER_ONLY = "elder_only"
    ELDER_OR_OBSERVER = "elder_or_observer"


class HabitPersistenceEligibility(str, Enum):
    EPISODE_ONLY = "episode_only"
    PROFILE_ELIGIBLE = "profile_eligible"


class HabitAnswerDisposition(str, Enum):
    ANSWERED = "answered"
    VARIABLE = "variable"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"
    PREFER_NOT_TO_ANSWER = "prefer_not_to_answer"
    SKIPPED = "skipped"
    NEVER_ASK = "never_ask"


class HabitConceptStatus(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    STALE = "stale"
    DISPUTED = "disputed"


class ObservationOpportunity(RadarAgentSchema):
    present: bool
    description: str | None = Field(default=None, max_length=240)
    confidence: float = Field(default=0, ge=0, le=1)

    @model_validator(mode="after")
    def require_direct_opportunity(self) -> "ObservationOpportunity":
        text = (self.description or "").lower()
        if self.present and (not text or self.confidence <= 0):
            raise ValueError(
                "present observation opportunity requires description/confidence"
            )
        if not self.present and self.confidence != 0:
            raise ValueError("absent observation opportunity cannot claim confidence")
        indirect = ("转述", "听说", "老人说", "家人说", "hearsay", "told me")
        if self.present and any(marker in text for marker in indirect):
            raise ValueError("reported hearsay is not direct observation opportunity")
        return self


class HabitConceptDefinition(RadarAgentSchema):
    model_config = ConfigDict(extra="forbid", frozen=True)

    concept_id: str = Field(..., pattern=r"^habit\.[a-z0-9_]+$")
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    domain: Literal[
        "schedule_constraint",
        "nap",
        "pre_sleep_behavior",
        "environment",
        "stimulant_timing",
        "subjective_context",
        "observable_night_behavior",
        "personal_goal",
        "data_quality",
        "night_activity",
        "care_delivery_preference",
    ]
    purpose: str = Field(..., min_length=1, max_length=300)
    canonical_question: str = Field(..., min_length=1, max_length=200)
    elder_text: str = Field(..., min_length=1, max_length=160)
    observer_text: str | None = Field(default=None, max_length=160)
    answer_type: HabitAnswerType
    options: tuple[str, ...] = ()
    unit: str | None = Field(default=None, max_length=40)
    minimum: float | None = None
    maximum: float | None = None
    short_text_max_length: int = Field(default=160, ge=1, le=240)
    respondent_rule: HabitRespondentRule
    allowed_dispositions: tuple[HabitAnswerDisposition, ...] = (
        HabitAnswerDisposition.ANSWERED,
        HabitAnswerDisposition.UNKNOWN,
        HabitAnswerDisposition.PREFER_NOT_TO_ANSWER,
        HabitAnswerDisposition.SKIPPED,
    )
    persistence: HabitPersistenceEligibility = (
        HabitPersistenceEligibility.EPISODE_ONLY
    )
    valid_for_days: int = Field(..., ge=1, le=730)
    cooldown_hours: int = Field(..., ge=0, le=8760)
    triggers: tuple[HabitQuestionTrigger, ...]
    affects_decisions: tuple[str, ...]
    forbidden_uses: tuple[str, ...] = (
        "diagnosis",
        "composite_score",
        "fixed_person_type",
        "causal_claim",
    )
    negative_answer_requires_observation_opportunity: bool = False
    safety_route_enabled: bool = False
    neutral_wording_required: Literal[True] = True
    symmetric_answer_handling: Literal[True] = True
    wording_policy: Literal["reviewed_variant_or_polite_wrapper"] = (
        "reviewed_variant_or_polite_wrapper"
    )
    migration_policy: Literal["explicit_semantic_equivalence_only"] = (
        "explicit_semantic_equivalence_only"
    )
    reconfirmation_reasons: tuple[
        Literal["expired", "decision_needed", "source_conflict", "user_review"],
        ...,
    ] = ("expired", "decision_needed", "source_conflict", "user_review")
    domain_review_status: Literal["pending", "approved", "rejected"]
    reviewer_ref: str = Field(..., min_length=1)
    content_version: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_answer_contract(self) -> "HabitConceptDefinition":
        if self.answer_type in {HabitAnswerType.CHOICE, HabitAnswerType.SCALE}:
            if len(self.options) < 2:
                raise ValueError("choice/scale Habit concept requires options")
        elif self.options:
            raise ValueError("only choice/scale Habit concept may define options")
        if self.answer_type == HabitAnswerType.BOUNDED_NUMBER:
            if self.minimum is None or self.maximum is None or not self.unit:
                raise ValueError(
                    "bounded_number Habit concept requires unit/minimum/maximum"
                )
            if self.minimum > self.maximum:
                raise ValueError("Habit concept minimum exceeds maximum")
        elif self.minimum is not None or self.maximum is not None:
            raise ValueError("only bounded_number may define numeric bounds")
        if (
            self.respondent_rule == HabitRespondentRule.ELDER_OR_OBSERVER
            and not self.observer_text
        ):
            raise ValueError("observer-eligible Habit concept requires observer text")
        if (
            self.persistence == HabitPersistenceEligibility.PROFILE_ELIGIBLE
            and self.domain_review_status != "approved"
        ):
            raise ValueError("profile-eligible Habit concept must be reviewed")
        return self


class HabitQuestionSelectionRequest(RadarAgentSchema):
    request_id: str = Field(..., min_length=1)
    episode_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    actor_id: str = Field(..., min_length=1)
    role: Literal["elder", "family", "doctor"]
    plan_id: str = Field(..., min_length=1)
    plan_revision: int = Field(..., ge=0)
    plan_step_id: str = Field(..., min_length=1)
    trigger: HabitQuestionTrigger
    decision_kind: Literal[
        "evidence", "care", "profile_review", "optional_intake"
    ]
    decision_gap_ref: str | None = None
    concept_states: dict[str, HabitConceptStatus] = Field(default_factory=dict)
    alternative_explanations: tuple[str, ...] = ()
    candidate_concept_ids: tuple[str, ...] = ()
    remaining_episode_budget: int = Field(..., ge=0, le=3)
    max_questions: int = Field(default=3, ge=1, le=3)
    profile_update_requested: bool = False

    @model_validator(mode="after")
    def require_decision_authority(self) -> "HabitQuestionSelectionRequest":
        gap_triggers = {
            HabitQuestionTrigger.DECISION_RELEVANT_GAP,
            HabitQuestionTrigger.STALE_FACT_NEEDED,
            HabitQuestionTrigger.SOURCE_CONFLICT,
        }
        if self.trigger in gap_triggers and not self.decision_gap_ref:
            raise ValueError("decision trigger requires decision_gap_ref")
        if self.trigger in gap_triggers and not self.alternative_explanations:
            raise ValueError(
                "decision trigger requires still-valid alternative explanations"
            )
        if (
            self.trigger == HabitQuestionTrigger.EXPLICIT_PROFILE_REVIEW
            and not self.profile_update_requested
            and not self.decision_gap_ref
        ):
            raise ValueError(
                "profile review must display first; selection requires update request or gap"
            )
        return self


class HabitQuestionCandidate(RadarAgentSchema):
    concept_id: str
    concept_version: str
    prompt_text: str
    answer_type: HabitAnswerType
    options: tuple[str, ...] = ()
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    allowed_dispositions: tuple[HabitAnswerDisposition, ...]
    respondent_rule: HabitRespondentRule
    persistence: HabitPersistenceEligibility
    trigger: HabitQuestionTrigger
    decision_gap_ref: str | None = None


class HabitQuestionSelectionReceipt(RadarAgentSchema):
    selection_id: str
    selection_hash: str = Field(..., min_length=64, max_length=64)
    request_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        description=(
            "Semantic selection-command hash; None is accepted only for "
            "legacy persisted receipts."
        ),
    )
    request_id: str
    episode_id: str
    subject_id: str
    actor_id: str
    role: Literal["elder", "family", "doctor"]
    plan_id: str
    plan_revision: int = Field(..., ge=0)
    plan_step_id: str
    trigger: HabitQuestionTrigger
    issued_at: datetime
    expires_at: datetime
    episode_question_count_after: int = Field(..., ge=0, le=3)
    candidates: tuple[HabitQuestionCandidate, ...] = ()


class HabitQuestionAnswer(RadarAgentSchema):
    concept_id: str
    concept_version: str
    disposition: HabitAnswerDisposition
    value: Any = None
    observation_date_start: date | None = None
    observation_date_end: date | None = None
    timezone_name: str = Field(default="Asia/Shanghai", min_length=1)
    day_type: Literal["all_days", "weekday", "weekend", "variable"] = "all_days"
    sleep_day_rule: Literal["wake_date", "bed_date"] = "wake_date"
    observation_opportunity: ObservationOpportunity | None = None
    question_opt_out_acknowledged: Literal[True] | None = None

    @model_validator(mode="after")
    def bind_question_opt_out_acknowledgement(
        self,
    ) -> "HabitQuestionAnswer":
        if self.disposition == HabitAnswerDisposition.NEVER_ASK:
            if self.question_opt_out_acknowledged is not True:
                raise ValueError(
                    "never-ask requires explicit typed opt-out acknowledgement"
                )
        elif self.question_opt_out_acknowledged is not None:
            raise ValueError(
                "question opt-out acknowledgement requires never-ask"
            )
        return self


class CapturedHabitAnswer(RadarAgentSchema):
    answer_ref: str
    episode_id: str
    subject_id: str
    actor_id: str
    role: Literal["elder", "family"]
    concept_id: str
    concept_version: str
    disposition: HabitAnswerDisposition
    normalized_value: Any = None
    origin_semantic: Literal["elder_self_report", "family_observation"]
    observation_date_start: date | None = None
    observation_date_end: date | None = None
    timezone_name: str
    day_type: Literal["all_days", "weekday", "weekend", "variable"]
    sleep_day_rule: Literal["wake_date", "bed_date"]
    observation_opportunity: ObservationOpportunity | None = None
    profile_candidate_eligible: bool = False
    trust_label: Literal["user_data"] = "user_data"
    captured_at: datetime
    episode_valid_until: datetime

    @model_validator(mode="after")
    def keep_recent_context_bounded(self) -> "CapturedHabitAnswer":
        if self.episode_valid_until <= self.captured_at:
            raise ValueError("captured Habit answer validity must be bounded")
        return self


class HabitSafetyEvent(RadarAgentSchema):
    event_id: str
    answer_ref: str
    episode_id: str
    subject_id: str
    source_role: Literal["elder", "family"]
    reason_code: str
    minimal_text: str = Field(..., max_length=240)
    captured_at: datetime
    valid_until: datetime
    retention_policy_ref: str = "clinical-safety-event-retention.v1"

    @model_validator(mode="after")
    def require_bounded_safety_event(self) -> "HabitSafetyEvent":
        if self.valid_until <= self.captured_at:
            raise ValueError("Habit Safety event validity must be positive")
        return self


class QuestionSuppression(RadarAgentSchema):
    suppression_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    concept_id: str = Field(..., pattern=r"^habit\.[a-z0-9_]+$")
    scope: Literal["profile_question"]
    withdrawal_command_ref: str = Field(..., min_length=1)
    expires_at: datetime


class HabitQuestionCapture(RadarAgentSchema):
    selection_id: str
    answers: tuple[CapturedHabitAnswer, ...] = ()
    suppressions: tuple[QuestionSuppression, ...] = ()
    safety_events: tuple[HabitSafetyEvent, ...] = ()
    stop_remaining_questions: bool = False


__all__ = [
    "CapturedHabitAnswer",
    "HabitAnswerDisposition",
    "HabitAnswerType",
    "HabitConceptDefinition",
    "HabitConceptStatus",
    "HabitPersistenceEligibility",
    "HabitQuestionAnswer",
    "HabitQuestionCandidate",
    "HabitQuestionCapture",
    "HabitQuestionSelectionReceipt",
    "HabitQuestionSelectionRequest",
    "HabitQuestionTrigger",
    "HabitRespondentRule",
    "HabitSafetyEvent",
    "ObservationOpportunity",
    "QuestionSuppression",
]
