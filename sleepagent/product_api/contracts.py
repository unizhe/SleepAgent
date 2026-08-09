"""Versioned public DTOs for ``/product/sleep``."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from sleepagent.sleep_domain.episode_v2 import EpisodeAssignmentBasis


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


class PublicOperationState(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ProjectionRecord(PublicModel):
    projection_id: NonEmpty
    projection_version: int = Field(ge=1)
    subject_ref: NonEmpty
    role: ProductRole
    episode_id: str | None = None
    episode_revision_id: str | None = None
    episode_local_date: date | None = None
    assignment_basis: EpisodeAssignmentBasis | None = None
    status: NonEmpty
    content: dict[str, Any]
    committed_at: datetime

    @field_validator("committed_at")
    @classmethod
    def committed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("committed_at must include a timezone offset")
        return value


class ProjectionResponse(PublicModel):
    schema_version: Literal["product_projection_response.v1"] = (
        "product_projection_response.v1"
    )
    data_mode: Literal["live", "replay"]
    synthetic_non_release: bool
    kind: Literal["today", "trends", "care", "records"]
    items: tuple[ProjectionRecord, ...]
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
    event_at: datetime

    @field_validator("event_at")
    @classmethod
    def event_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event_at must include a timezone offset")
        return value


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
    code: NonEmpty
    message: NonEmpty
    retryable: bool = False
    correlation_id: str | None = None


__all__ = [
    "AcceptedOperationResponse",
    "EpisodeAssignmentBasis",
    "ErrorResponse",
    "FeedbackRequest",
    "InteractionAnswerRequest",
    "InteractionAskRequest",
    "InteractionDecisionRequest",
    "InteractionStartRequest",
    "InteractionStatusResponse",
    "ProductRole",
    "ProjectionRecord",
    "ProjectionResponse",
    "PublicOperationState",
]
