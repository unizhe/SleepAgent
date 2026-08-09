"""Versioned public contracts for the independent sleep-domain HTTP API.

These models intentionally project committed domain state.  They do not reuse
internal execution, evidence, or provider contracts as public wire schemas.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PublicApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublicActorRole(str, Enum):
    ELDER = "elder"
    FAMILY = "family"
    CAREGIVER = "caregiver"
    DOCTOR = "doctor"


class PublicServiceState(str, Enum):
    ACTIVE = "active"
    FOLLOW_UP = "follow_up"
    DORMANT = "dormant"


class PublicMonitoringState(str, Enum):
    ACTIVE = "active"
    DORMANT = "dormant"


class PublicOperationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublicErrorCode(str, Enum):
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    INVALID_SERVICE_CREDENTIAL = "INVALID_SERVICE_CREDENTIAL"
    INVALID_ACTOR_ASSERTION = "INVALID_ACTOR_ASSERTION"
    ACTOR_ASSERTION_REPLAYED = "ACTOR_ASSERTION_REPLAYED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    AUTHORIZATION_UNAVAILABLE = "AUTHORIZATION_UNAVAILABLE"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    RESULT_PENDING = "RESULT_PENDING"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_REQUEST = "INVALID_REQUEST"
    OPERATION_CONFLICT = "OPERATION_CONFLICT"
    OPERATION_LEASE_LOST = "OPERATION_LEASE_LOST"
    BINDING_REQUIRED = "BINDING_REQUIRED"
    INVALID_LIFECYCLE_TRANSITION = "INVALID_LIFECYCLE_TRANSITION"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    CURSOR_RESYNC_REQUIRED = "CURSOR_RESYNC_REQUIRED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class RevocationTombstone(PublicApiModel):
    schema_version: Literal["revocation_tombstone.v1"] = "revocation_tombstone.v1"
    event_id: str
    event_type: Literal["ACCESS_REVOKED_TOMBSTONE"] = "ACCESS_REVOKED_TOMBSTONE"
    aggregate_id: str
    aggregate_version: Literal[1] = 1
    per_aggregate_sequence: Literal[1] = 1
    subject_id: str
    effective_at: datetime
    action_required: Literal["delete_local_authorized_cache"] = (
        "delete_local_authorized_cache"
    )
    remote_recall_possible: Literal[False] = False


class ErrorResponse(PublicApiModel):
    schema_version: Literal["error.v1"] = "error.v1"
    code: PublicErrorCode
    message: str = Field(..., min_length=1, max_length=300)
    correlation_id: str = Field(..., min_length=1, max_length=128)
    retryable: bool = False
    details: dict[str, str] = Field(default_factory=dict)
    tombstone: RevocationTombstone | None = None


class PageMetadata(PublicApiModel):
    schema_version: Literal["page.v1"] = "page.v1"
    limit: int = Field(..., ge=1, le=100)
    next_cursor: str | None = None


class LifecycleResponse(PublicApiModel):
    schema_version: Literal["lifecycle_response.v1"] = "lifecycle_response.v1"
    subject_id: str
    service_state: PublicServiceState
    monitoring_state: PublicMonitoringState
    active_night_episode_id: str | None
    data_quality_state: str | None
    data_sufficiency: str | None
    committed_at: datetime


class CurrentRiskSourceScope(PublicApiModel):
    schema_version: Literal["current_risk_source_scope.v1"] = (
        "current_risk_source_scope.v1"
    )
    night_episode_revision_id: str | None
    observation_types: tuple[str, ...]
    observation_count: int = Field(..., ge=0)
    device_count: int = Field(..., ge=0)
    window_start_at: datetime
    window_end_at: datetime


class CurrentRiskResponse(PublicApiModel):
    schema_version: Literal["current_risk_response.v1"] = (
        "current_risk_response.v1"
    )
    subject_id: str
    night_episode_id: str
    risk_state: str
    data_sufficiency: str
    source_scope: CurrentRiskSourceScope
    policy_version: str
    observed_at: datetime
    reason_codes: tuple[str, ...]
    health_escalation_allowed: bool
    committed_at: datetime


class NightEpisodeSummary(PublicApiModel):
    schema_version: Literal["night_episode_summary.v1"] = (
        "night_episode_summary.v1"
    )
    night_episode_id: str
    subject_id: str
    local_sleep_date: date = Field(
        ...,
        json_schema_extra={"deprecated": True},
        description=(
            "Deprecated compatibility date; use episode_local_date and "
            "assignment_basis for wake-date semantics."
        ),
    )
    episode_local_date: date | None = None
    assignment_basis: Literal[
        "observed_wake", "vendor_wake_date", "deadline_fallback"
    ] | None = None
    bed_local_date: date | None = None
    wake_local_date: date | None = None
    date_confidence: Literal[
        "observed", "vendor_asserted", "estimated"
    ] | None = None
    assignment_estimated: bool | None = None
    timezone_name: str
    collection_start_at: datetime
    collection_end_at: datetime | None
    lifecycle_state: str
    data_sufficiency: str
    quality_flags: tuple[str, ...]
    current_revision_number: int | None
    current_revision_id: str | None
    committed_at: datetime


class NightEpisodeRevisionSnapshot(PublicApiModel):
    schema_version: Literal["night_episode_revision_response.v1"] = (
        "night_episode_revision_response.v1"
    )
    night_episode_id: str
    night_episode_revision_id: str
    subject_id: str
    revision_number: int
    parent_revision_id: str | None
    revision_cause: str
    data_sufficiency: str
    quality_flags: tuple[str, ...]
    episode_local_date: date | None = None
    assignment_basis: Literal[
        "observed_wake", "vendor_wake_date", "deadline_fallback"
    ] | None = None
    bed_local_date: date | None = None
    wake_local_date: date | None = None
    date_confidence: Literal[
        "observed", "vendor_asserted", "estimated"
    ] | None = None
    assignment_estimated: bool | None = None
    created_at: datetime


class NightEpisodeResponse(PublicApiModel):
    schema_version: Literal["night_episode_response.v1"] = (
        "night_episode_response.v1"
    )
    summary: NightEpisodeSummary
    revision: NightEpisodeRevisionSnapshot | None


class NightEpisodePageResponse(PublicApiModel):
    schema_version: Literal["night_episode_page_response.v1"] = (
        "night_episode_page_response.v1"
    )
    items: tuple[NightEpisodeSummary, ...]
    page: PageMetadata


class RoleViewResponse(PublicApiModel):
    schema_version: Literal["role_view_response.v1"] = "role_view_response.v1"
    subject_id: str
    night_episode_id: str
    night_episode_revision_id: str
    role: PublicActorRole
    status: str
    execution_mode: str
    content: str | None
    context_notice: str | None
    failure_codes: tuple[str, ...]
    generated_at: datetime
    authorization_epoch: int = Field(..., ge=0)


class OperationStatusResponse(PublicApiModel):
    schema_version: Literal["operation_status_response.v1"] = (
        "operation_status_response.v1"
    )
    operation_id: str
    operation_type: str
    subject_id: str
    status: PublicOperationStatus
    attempt: int = Field(..., ge=0)
    result_resource_id: str | None
    error_code: str | None
    correlation_id: str
    created_at: datetime
    updated_at: datetime


class AcceptedOperationResponse(PublicApiModel):
    schema_version: Literal["accepted_operation_response.v1"] = (
        "accepted_operation_response.v1"
    )
    operation_id: str
    status: Literal["pending", "running", "succeeded", "failed", "cancelled"]
    status_url: str
    correlation_id: str


class ActivateMonitoringRequest(PublicApiModel):
    schema_version: Literal["activate_monitoring_request.v1"] = (
        "activate_monitoring_request.v1"
    )
    device_binding_id: str | None = Field(default=None, min_length=1, max_length=200)
    occurred_at: datetime | None = None


class DeactivateMonitoringRequest(PublicApiModel):
    schema_version: Literal["deactivate_monitoring_request.v1"] = (
        "deactivate_monitoring_request.v1"
    )
    occurred_at: datetime | None = None


class FeedbackRequest(PublicApiModel):
    schema_version: Literal["feedback_request.v1"] = "feedback_request.v1"
    night_episode_id: str = Field(..., min_length=1, max_length=200)
    event_at: datetime
    source_text: str | None = Field(default=None, min_length=1, max_length=4000)
    structured_answer: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_feedback_content(self) -> "FeedbackRequest":
        if self.source_text is None and self.structured_answer is None:
            raise ValueError("feedback requires source_text or structured_answer")
        return self


class ReanalysisRequest(PublicApiModel):
    schema_version: Literal["reanalysis_request.v1"] = "reanalysis_request.v1"
    reason: str | None = Field(default=None, min_length=1, max_length=500)


class ExternalDomainEvent(PublicApiModel):
    """Role-minimized projection of an immutable committed domain event."""

    schema_version: Literal["sleep_domain_event.v1"] = "sleep_domain_event.v1"
    event_id: str
    event_type: str
    event_version: str
    aggregate_id: str
    aggregate_version: int = Field(..., ge=1)
    per_aggregate_sequence: int = Field(..., ge=1)
    subject_id: str
    night_episode_id: str | None = None
    night_episode_revision_id: str | None = None
    operation_id: str | None = None
    occurred_at: datetime
    persisted_at: datetime
    payload: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict
    )


class EventPollResponse(PublicApiModel):
    schema_version: Literal["event_poll_response.v1"] = "event_poll_response.v1"
    event_schema_generation: str
    events: tuple[ExternalDomainEvent, ...]
    next_cursor: str
    cursor_expires_at: datetime
    has_more: bool
    delivery_semantics: Literal["at_least_once"] = "at_least_once"
    ordering_semantics: Literal[
        "per_aggregate_sequence_only_no_global_business_order"
    ] = "per_aggregate_sequence_only_no_global_business_order"


__all__ = [
    "AcceptedOperationResponse",
    "ActivateMonitoringRequest",
    "CurrentRiskResponse",
    "CurrentRiskSourceScope",
    "DeactivateMonitoringRequest",
    "ErrorResponse",
    "EventPollResponse",
    "ExternalDomainEvent",
    "FeedbackRequest",
    "LifecycleResponse",
    "NightEpisodePageResponse",
    "NightEpisodeResponse",
    "NightEpisodeRevisionSnapshot",
    "NightEpisodeSummary",
    "OperationStatusResponse",
    "PageMetadata",
    "PublicActorRole",
    "PublicErrorCode",
    "PublicMonitoringState",
    "PublicOperationStatus",
    "PublicServiceState",
    "ReanalysisRequest",
    "RevocationTombstone",
    "RoleViewResponse",
]
