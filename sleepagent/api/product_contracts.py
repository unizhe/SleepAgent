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
    "InteractionAnswerRequest",
    "InteractionAskRequest",
    "InteractionDecisionRequest",
    "InteractionStartRequest",
    "InteractionStatusResponse",
    "ProductRole",
    "ProductCareResponse",
    "ProductRecordsResponse",
    "ProductSleepTodayNoData",
    "ProductSleepTodayProjection",
    "ProductSleepTodayResponse",
    "ProductTrendsResponse",
    "ProductTodayState",
    "PublicOperationState",
    "SleepRecord",
    "TrendPoint",
]
