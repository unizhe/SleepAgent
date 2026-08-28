# 本模块负责唯一 ASGI 服务的接口契约或请求编排，不承载领域状态。
"""Versioned public DTOs for ``/product/sleep``."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from sleepagent.domain.episodes import EpisodeAssignmentBasis


NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _wire_array_as_tuple(value: Any) -> Any:
    """Normalize a decoded JSON array without weakening strict item types."""

    return tuple(value) if isinstance(value, list) else value


class PublicModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
    )


class ProductRole(str, Enum):
    ELDER = "elder"
    FAMILY = "family"
    DOCTOR = "doctor"


class ProductReportState(str, Enum):
    NOT_RUN = "not_run"
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    STALE = "stale"
    POLICY_BLOCKED = "policy_blocked"
    UNUSABLE_BLOCKED = "unusable_blocked"
    URGENT_HANDLED = "urgent_handled"


class ProductReportQuality(str, Enum):
    GOOD = "good"
    PARTIAL = "partial"
    UNUSABLE = "unusable"


class ProductNarrativeState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    FALLBACK = "fallback"
    FAILED = "failed"
    STALE = "stale"


ProductReportFailureCode: TypeAlias = Literal[
    "analysis_failed",
    "doctor_safety_unavailable",
    "policy_blocked",
    "data_unusable",
    "source_stale",
]


class ProductReportTrace(PublicModel):
    """Strict, identifier-free operational trace for an explicit report read."""

    gate: Literal["analyzable", "urgent", "unusable", "not_evaluated"]
    shared_analysis: Literal[
        "created", "reused", "pending", "not_applicable"
    ]
    elder_narrative: Literal[
        "created", "reused", "pending", "fallback", "not_applicable"
    ]
    fallback_used: bool
    provider_call_count: int = Field(ge=0)
    provider_input_tokens: int = Field(ge=0)
    provider_output_tokens: int = Field(ge=0)
    provider_request_ids_present: bool


class ProductReportProjection(PublicModel):
    audience: ProductRole
    summary_text: NonEmpty
    context_notice: NonEmpty


class ProductReportNarrative(PublicModel):
    state: ProductNarrativeState
    text: NonEmpty | None = None

    @model_validator(mode="after")
    def text_matches_state(self) -> "ProductReportNarrative":
        if (self.state == ProductNarrativeState.READY) != (self.text is not None):
            raise ValueError("only a ready elder narrative may carry text")
        return self


class ProductReportRunRequest(PublicModel):
    schema_version: Literal["product_sleep_report_run.v1"] = (
        "product_sleep_report_run.v1"
    )
    # JSON has no date scalar. Keep the model strict except for canonical ISO dates.
    wake_date: date = Field(strict=False)

    @field_validator("wake_date", mode="before")
    @classmethod
    def wake_date_is_canonical_iso_date(cls, value: Any) -> date:
        if type(value) is date:
            return value
        if not isinstance(value, str):
            raise ValueError("wake_date must be an ISO date in YYYY-MM-DD form")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                "wake_date must be an ISO date in YYYY-MM-DD form"
            ) from exc
        if value != parsed.isoformat():
            raise ValueError("wake_date must be an ISO date in YYYY-MM-DD form")
        return parsed


class ProductReportRunAccepted(PublicModel):
    schema_version: Literal["product_sleep_report_run_accepted.v1"] = (
        "product_sleep_report_run_accepted.v1"
    )
    wake_date: date
    state: Literal["accepted"] = "accepted"
    status_url: NonEmpty


class ProductSleepReportResponse(PublicModel):
    schema_version: Literal["product_sleep_report.v1"] = (
        "product_sleep_report.v1"
    )
    wake_date: date
    state: ProductReportState
    audience: ProductRole
    quality: ProductReportQuality | None = None
    quality_caveat: NonEmpty | None = None
    projection: ProductReportProjection | None = None
    narrative: ProductReportNarrative | None = None
    failure_code: ProductReportFailureCode | None = None
    trace: ProductReportTrace | None = None

    @model_validator(mode="after")
    def report_shape_is_safe(self) -> "ProductSleepReportResponse":
        if self.quality == ProductReportQuality.PARTIAL and not self.quality_caveat:
            raise ValueError("PARTIAL report quality requires a caveat")
        if self.state == ProductReportState.READY:
            if self.projection is None:
                raise ValueError("ready report requires a projection")
        elif self.projection is not None:
            raise ValueError("only a ready report may carry a projection")
        if self.projection is not None and self.projection.audience != self.audience:
            raise ValueError("report projection audience must match authority")
        if self.narrative is not None and self.audience != ProductRole.ELDER:
            raise ValueError("elder narrative cannot cross an audience boundary")
        if (
            self.state == ProductReportState.UNUSABLE_BLOCKED
            and self.quality != ProductReportQuality.UNUSABLE
        ):
            raise ValueError("unusable report state requires unusable quality")
        return self


class ProductSleepReportListItem(PublicModel):
    wake_date: date
    state: ProductReportState
    audience: ProductRole
    quality: ProductReportQuality | None = None
    quality_caveat: NonEmpty | None = None
    narrative_state: ProductNarrativeState | None = None
    failure_code: ProductReportFailureCode | None = None

    @model_validator(mode="after")
    def list_item_is_safe(self) -> "ProductSleepReportListItem":
        if self.quality == ProductReportQuality.PARTIAL and not self.quality_caveat:
            raise ValueError("PARTIAL report quality requires a caveat")
        if self.narrative_state is not None and self.audience != ProductRole.ELDER:
            raise ValueError("elder narrative state cannot cross an audience boundary")
        if (
            self.state == ProductReportState.UNUSABLE_BLOCKED
            and self.quality != ProductReportQuality.UNUSABLE
        ):
            raise ValueError("unusable report state requires unusable quality")
        return self


class ProductSleepReportListResponse(PublicModel):
    schema_version: Literal["product_sleep_report_list.v1"] = (
        "product_sleep_report_list.v1"
    )
    items: tuple[ProductSleepReportListItem, ...]
    next_cursor: str | None = None
    trace: ProductReportTrace | None = None


class ProductTodayState(str, Enum):
    NO_DATA = "no_data"
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ElderTodayContent(PublicModel):
    audience: Literal["elder"] = "elder"
    summary_text: NonEmpty
    context_notice: NonEmpty


class FamilyTodayContent(PublicModel):
    audience: Literal["family"] = "family"
    summary_text: NonEmpty
    context_notice: NonEmpty


class DoctorTodayContent(PublicModel):
    audience: Literal["doctor"] = "doctor"
    summary_text: NonEmpty
    context_notice: NonEmpty
    evidence_refs: tuple[NonEmpty, ...] = ()


TodayContent: TypeAlias = Annotated[
    ElderTodayContent | FamilyTodayContent | DoctorTodayContent,
    Field(discriminator="audience"),
]


class ProductSleepTodayNoData(PublicModel):
    schema_version: Literal["product_sleep_today.v1"] = "product_sleep_today.v1"
    data_mode: Literal["live", "replay"]
    synthetic_non_release: bool
    state: Literal[ProductTodayState.NO_DATA] = ProductTodayState.NO_DATA
    subject_ref: NonEmpty
    role: ProductRole
    episode_id: None = None
    episode_revision_id: None = None
    episode_local_date: None = None
    assignment_basis: None = None
    analysis_revision_id: None = None
    projection_id: None = None
    projection_version: None = None
    committed_at: None = None
    content: None = None


class ProductSleepTodayProjection(PublicModel):
    schema_version: Literal["product_sleep_today.v1"] = "product_sleep_today.v1"
    data_mode: Literal["live", "replay"]
    synthetic_non_release: bool
    state: Literal[
        ProductTodayState.READY,
        ProductTodayState.DEGRADED,
        ProductTodayState.BLOCKED,
    ]
    subject_ref: NonEmpty
    role: ProductRole
    episode_id: NonEmpty
    episode_revision_id: NonEmpty
    episode_local_date: date
    assignment_basis: EpisodeAssignmentBasis
    analysis_revision_id: NonEmpty
    projection_id: NonEmpty
    projection_version: int = Field(ge=1)
    committed_at: datetime
    content: TodayContent

    @field_validator("committed_at")
    @classmethod
    def today_committed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("committed_at must include a timezone offset")
        return value

    @model_validator(mode="after")
    def content_matches_role(self) -> "ProductSleepTodayProjection":
        if self.content.audience != self.role.value:
            raise ValueError("today content audience must match the authorized role")
        return self


ProductSleepTodayResponse: TypeAlias = Annotated[
    ProductSleepTodayNoData | ProductSleepTodayProjection,
    Field(discriminator="state"),
]


class PublicOperationState(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class TrendPoint(PublicModel):
    night_episode_id: NonEmpty
    night_episode_revision_id: NonEmpty
    episode_local_date: date
    assignment_basis: EpisodeAssignmentBasis
    analysis_revision_id: NonEmpty
    projection_id: NonEmpty
    projection_state: Literal["ready", "degraded", "blocked"]
    sleep_window_minutes: int | None = Field(default=None, ge=0, le=1_440)
    committed_at: datetime

    @field_validator("committed_at")
    @classmethod
    def trend_committed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("committed_at must include a timezone offset")
        return value


class ProductTrendsResponse(PublicModel):
    schema_version: Literal["product_sleep_trends.v1"] = "product_sleep_trends.v1"
    data_mode: Literal["live", "replay"]
    synthetic_non_release: bool
    subject_ref: NonEmpty
    role: ProductRole
    items: tuple[TrendPoint, ...]
    next_cursor: str | None = None


class SleepRecord(PublicModel):
    analysis_revision_id: NonEmpty
    analysis_revision_number: int = Field(ge=1)
    parent_analysis_revision_id: str | None = None
    night_episode_id: NonEmpty
    night_episode_revision_id: NonEmpty
    projection_id: NonEmpty
    projection_version: int = Field(ge=1)
    projection_state: Literal["ready", "degraded", "blocked"]
    is_current: bool
    committed_at: datetime

    @field_validator("committed_at")
    @classmethod
    def record_committed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("committed_at must include a timezone offset")
        return value


class ProductRecordsResponse(PublicModel):
    schema_version: Literal["product_sleep_records.v1"] = "product_sleep_records.v1"
    data_mode: Literal["live", "replay"]
    synthetic_non_release: bool
    subject_ref: NonEmpty
    role: ProductRole
    items: tuple[SleepRecord, ...]
    next_cursor: str | None = None


class CareActionRecord(PublicModel):
    record_type: Literal["care_action"] = "care_action"
    care_action_id: NonEmpty
    interaction_id: NonEmpty
    state: Literal[
        "confirmed_pending_delivery", "active", "completed", "cancelled"
    ]
    action_kind: NonEmpty
    source_analysis_revision_id: str | None = None
    confirmed_at: datetime
    updated_at: datetime

    @field_validator("confirmed_at", "updated_at")
    @classmethod
    def care_times_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Care timestamps must include a timezone offset")
        return value


class CareFollowupRecord(PublicModel):
    record_type: Literal["care_followup"] = "care_followup"
    night_episode_id: NonEmpty
    state: Literal[
        "pending_feedback", "following_up", "completed", "ended"
    ]
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def followup_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Care follow-up timestamp must include a timezone offset")
        return value


class ProductCareResponse(PublicModel):
    schema_version: Literal["product_sleep_care.v1"] = "product_sleep_care.v1"
    data_mode: Literal["live", "replay"]
    synthetic_non_release: bool
    subject_ref: NonEmpty
    role: ProductRole
    items: tuple[CareActionRecord | CareFollowupRecord, ...]
    next_cursor: str | None = None


class InteractionStartRequest(PublicModel):
    intent: NonEmpty = Field(max_length=200)
    episode_revision_id: str | None = Field(default=None, max_length=200)


class InteractionAskRequest(PublicModel):
    message: NonEmpty = Field(max_length=4_000)


class InteractionAnswerRequest(PublicModel):
    answer_handle: NonEmpty = Field(max_length=512)
    answer: NonEmpty = Field(max_length=4_000)


class InteractionDecisionRequest(PublicModel):
    confirmation_handle: NonEmpty = Field(max_length=512)
    reason_code: str | None = Field(default=None, max_length=100)


class FeedbackRequest(PublicModel):
    interaction_id: str | None = Field(default=None, max_length=200)
    episode_revision_id: str | None = Field(default=None, max_length=200)
    feedback: NonEmpty = Field(max_length=4_000)
    # JSON has no native datetime type, so this request field must accept the
    # canonical RFC 3339 string carried on the wire even though response DTOs
    # remain strict about receiving already-typed values from the backend.
    event_at: datetime = Field(strict=False)

    @field_validator("event_at")
    @classmethod
    def event_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event_at must include a timezone offset")
        return value

    @model_validator(mode="after")
    def exactly_one_feedback_target(self) -> "FeedbackRequest":
        if (self.interaction_id is None) == (self.episode_revision_id is None):
            raise ValueError(
                "feedback requires exactly one interaction or Episode revision"
            )
        return self


class HabitQuestionSelectionRequest(PublicModel):
    episode_id: NonEmpty = Field(max_length=200)
    candidate_concept_ids: tuple[NonEmpty, ...] = ()
    remaining_episode_budget: int = Field(default=3, ge=0, le=3)

    _normalize_candidate_concepts = field_validator(
        "candidate_concept_ids", mode="before"
    )(_wire_array_as_tuple)


class HabitQuestionSelectionResponse(PublicModel):
    schema_version: Literal["habit_question_selection.v2"] = (
        "habit_question_selection.v2"
    )
    selection: dict[str, Any]
    questions: tuple[dict[str, Any], ...]
    profile_version: int = Field(ge=0)


class HabitAnswerCommand(PublicModel):
    operation: Literal["remember", "correct"]
    answer: dict[str, Any]
    target_fact_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def correction_requires_target(self) -> "HabitAnswerCommand":
        if (self.operation == "correct") != bool(self.target_fact_id):
            raise ValueError("Habit correction requires one exact target fact")
        return self


class HabitChangeRequest(PublicModel):
    operation: Literal["remember", "correct", "expire", "forget"] | None = None
    selection_id: str | None = Field(default=None, min_length=1)
    answers: tuple[HabitAnswerCommand, ...] = Field(default=(), max_length=1)
    concept_id: str | None = Field(default=None, min_length=1)
    target_fact_id: str | None = Field(default=None, min_length=1)
    confirmation_actor_id: NonEmpty

    _normalize_answers = field_validator("answers", mode="before")(
        _wire_array_as_tuple
    )

    @model_validator(mode="after")
    def one_habit_change_mode(self) -> "HabitChangeRequest":
        answering = bool(self.answers)
        lifecycle = self.operation in {"expire", "forget"}
        if answering == lifecycle:
            raise ValueError("submit Habit answers or one expire/forget command")
        if answering and not self.selection_id:
            raise ValueError("Habit answers require an exact selection")
        if answering and any(
            value is not None
            for value in (self.operation, self.concept_id, self.target_fact_id)
        ):
            raise ValueError("Habit answer mode cannot carry lifecycle fields")
        if lifecycle and not (self.concept_id and self.target_fact_id):
            raise ValueError("Habit expire/forget requires an exact target")
        if lifecycle and self.selection_id is not None:
            raise ValueError("Habit lifecycle mode cannot carry a selection")
        return self


class PendingL2Change(PublicModel):
    capability: Literal["habit", "memory"]
    change_id: NonEmpty
    change_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    confirmation_handle: NonEmpty
    expires_at: datetime


class HabitChangeResponse(PublicModel):
    schema_version: Literal["habit_change_proposal.v2"] = (
        "habit_change_proposal.v2"
    )
    evidence: tuple[dict[str, Any], ...] = ()
    pending_changes: tuple[PendingL2Change, ...] = ()


class L2ConfirmationRequest(PublicModel):
    change_id: NonEmpty
    change_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    confirmation_handle: NonEmpty = Field(max_length=512)


class L2ConfirmationResponse(PublicModel):
    schema_version: Literal["l2_confirmation.v1"] = "l2_confirmation.v1"
    capability: Literal["habit", "memory"]
    state_version: int = Field(ge=1)
    revision_ref: NonEmpty
    revision_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class HabitProfileResponse(PublicModel):
    schema_version: Literal["habit_profile.v2"] = "habit_profile.v2"
    profile_version: int = Field(ge=0)
    profile_hash: str | None = None
    current_facts: tuple[dict[str, Any], ...] = ()
    stale_concept_ids: tuple[str, ...] = ()
    disputed_concept_ids: tuple[str, ...] = ()


class MemoryChangeRequest(PublicModel):
    operation: Literal["remember", "correct", "expire", "forget"]
    memory_id: NonEmpty
    concept_id: str | None = Field(default=None, min_length=2, max_length=160)
    memory_type: Literal[
        "preference", "routine", "environment", "communication_preference"
    ] | None = None
    value_schema_id: Literal[
        "bounded_string.v1", "boolean.v1", "number.v1", "enum.v1"
    ] | None = None
    typed_value: Any = None
    sensitivity_class: Literal["personal", "sensitive_personal"] | None = None
    allowed_roles: tuple[
        Literal["sleep_care", "evidence_reasoning", "care_strategy"], ...
    ] = ()
    allowed_purposes: tuple[
        Literal[
            "personal_evidence_context",
            "care_preference_context",
            "explicit_memory_review",
            "explicit_memory_change",
            "explicit_memory_forget",
        ],
        ...,
    ] = ()
    source_scope_kind: Literal[
        "current_night", "7_day", "30_day", "historical_range"
    ] = "historical_range"
    source_text: str | None = Field(default=None, min_length=1, max_length=500)
    valid_until: datetime | None = None
    target_revision_ref: str | None = Field(default=None, min_length=1)
    target_revision_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    _normalize_access_arrays = field_validator(
        "allowed_roles", "allowed_purposes", mode="before"
    )(_wire_array_as_tuple)

    @model_validator(mode="after")
    def memory_change_shape(self) -> "MemoryChangeRequest":
        carries_value = self.operation in {"remember", "correct"}
        value_fields = (
            self.concept_id,
            self.memory_type,
            self.value_schema_id,
            self.sensitivity_class,
            self.source_text,
        )
        if carries_value and (
            not all(value_fields)
            or not self.allowed_roles
            or not self.allowed_purposes
        ):
            raise ValueError("remember/correct requires a governed typed value")
        if not carries_value and any(value is not None for value in value_fields):
            raise ValueError("expire/forget cannot carry a replacement value")
        if not carries_value and (
            self.typed_value is not None
            or self.allowed_roles
            or self.allowed_purposes
            or self.valid_until is not None
        ):
            raise ValueError("expire/forget cannot carry governed value metadata")
        target_required = self.operation != "remember"
        if target_required != bool(
            self.target_revision_ref and self.target_revision_hash
        ):
            raise ValueError("Memory change has an invalid exact target")
        return self


class MemoryQueryRequest(PublicModel):
    concept_ids: tuple[NonEmpty, ...] = Field(min_length=1, max_length=16)
    source_scope_kind: Literal[
        "current_night", "7_day", "30_day", "historical_range"
    ] = "historical_range"
    max_items: int = Field(default=8, ge=1, le=8)
    token_budget: int = Field(default=1200, ge=64, le=4096)

    _normalize_concept_ids = field_validator("concept_ids", mode="before")(
        _wire_array_as_tuple
    )


class MemoryQueryResponse(PublicModel):
    schema_version: Literal["governed_memory_query.v2"] = (
        "governed_memory_query.v2"
    )
    receipt: dict[str, Any]


class AcceptedOperationResponse(PublicModel):
    schema_version: Literal["product_accepted_operation.v1"] = (
        "product_accepted_operation.v1"
    )
    data_mode: Literal["live", "replay"]
    synthetic_non_release: bool
    operation_id: NonEmpty
    state: Literal[PublicOperationState.ACCEPTED] = PublicOperationState.ACCEPTED
    status_url: NonEmpty


class InteractionStatusResponse(PublicModel):
    schema_version: Literal["product_interaction_status.v1"] = (
        "product_interaction_status.v1"
    )
    data_mode: Literal["live", "replay"]
    synthetic_non_release: bool
    operation_id: NonEmpty
    interaction_id: str | None = None
    state: PublicOperationState
    result_ref: str | None = None
    interaction_revision: int | None = Field(default=None, ge=1)
    interaction_state: str | None = None
    answer_handle: str | None = None
    confirmation_handle: str | None = None
    human_decision_id: str | None = None
    care_action_id: str | None = None
    delivery_intent_id: str | None = None
    product_operation_id: str | None = None
    error_code: str | None = None
    retryable: bool = False
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("updated_at must include a timezone offset")
        return value


class ErrorResponse(PublicModel):
    schema_version: Literal["product_error.v1"] = "product_error.v1"
    data_mode: Literal["live", "replay"]
    synthetic_non_release: bool
    code: NonEmpty
    message: NonEmpty
    retryable: bool = False
    correlation_id: str | None = None


__all__ = [
    "AcceptedOperationResponse",
    "CareActionRecord",
    "CareFollowupRecord",
    "DoctorTodayContent",
    "ElderTodayContent",
    "EpisodeAssignmentBasis",
    "ErrorResponse",
    "FamilyTodayContent",
    "FeedbackRequest",
    "HabitAnswerCommand",
    "HabitChangeRequest",
    "HabitChangeResponse",
    "HabitProfileResponse",
    "HabitQuestionSelectionRequest",
    "HabitQuestionSelectionResponse",
    "InteractionAnswerRequest",
    "InteractionAskRequest",
    "InteractionDecisionRequest",
    "InteractionStartRequest",
    "InteractionStatusResponse",
    "L2ConfirmationRequest",
    "L2ConfirmationResponse",
    "MemoryChangeRequest",
    "MemoryQueryRequest",
    "MemoryQueryResponse",
    "PendingL2Change",
    "ProductRole",
    "ProductCareResponse",
    "ProductNarrativeState",
    "ProductReportFailureCode",
    "ProductReportProjection",
    "ProductReportQuality",
    "ProductReportRunAccepted",
    "ProductReportRunRequest",
    "ProductReportState",
    "ProductReportTrace",
    "ProductReportNarrative",
    "ProductRecordsResponse",
    "ProductSleepReportListItem",
    "ProductSleepReportListResponse",
    "ProductSleepReportResponse",
    "ProductSleepTodayNoData",
    "ProductSleepTodayProjection",
    "ProductSleepTodayResponse",
    "ProductTrendsResponse",
    "ProductTodayState",
    "PublicOperationState",
    "SleepRecord",
    "TrendPoint",
]
