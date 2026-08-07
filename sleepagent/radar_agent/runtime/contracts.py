from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Protocol

from pydantic import Field, model_validator

from sleepagent.radar_agent.schemas import (
    A2AMessage,
    EvidenceLedger,
    HumanConfirmationRequest,
    RadarAgentSchema,
    RoleReportArtifact,
)


class RadarTaskStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_FOR_USER_INPUT = "waiting_for_user_input"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RadarNodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class RadarTaskFailure(RadarAgentSchema):
    error_code: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    failed_node: str | None = None
    retryable: bool = True
    details: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RadarTaskEvent(RadarAgentSchema):
    event_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    sequence: int = Field(..., ge=1)
    event_type: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RadarAgentTask(RadarAgentSchema):
    task_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    radar_device_id: str = Field(..., min_length=1)
    role: Literal["elder", "family", "doctor", "system"] = "family"
    requested_by_user_id: str | None = None
    role_binding_ids: list[str] = Field(default_factory=list)
    authorization_id: str | None = None
    scenario: str = "replay"
    provider_input: dict[str, Any] = Field(default_factory=dict)
    runtime_kind: Literal[
        "legacy_fixed",
        "dynamic_goal",
        "product_episode",
    ] = "legacy_fixed"
    runtime_contract_version: str = "radar-legacy.v1"
    execution_mode: Literal[
        "intelligent",
        "safe_degraded",
        "deterministic_only",
        "legacy_fixed",
    ] | None = None
    completion_status: Literal[
        "complete", "partial", "blocked"
    ] | None = None
    goal_payload: dict[str, Any] | None = None
    current_plan_id: str | None = None
    pending_user_input_request_id: str | None = None
    task_version: int = Field(default=1, ge=1)
    status: RadarTaskStatus = RadarTaskStatus.CREATED
    node_status: dict[str, RadarNodeStatus] = Field(default_factory=dict)
    retry_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=3, ge=0)
    idempotency_key: str | None = None
    failure: RadarTaskFailure | None = None
    parent_task_id: str | None = None
    last_event_sequence: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def reject_mixed_runtime_state(self) -> "RadarAgentTask":
        if self.runtime_kind == "dynamic_goal":
            if self.runtime_contract_version != "radar-dynamic.v1":
                raise ValueError("dynamic task requires radar-dynamic.v1")
            if self.node_status:
                raise ValueError("dynamic tasks cannot persist legacy node state")
        elif self.runtime_kind == "legacy_fixed":
            if self.runtime_contract_version != "radar-legacy.v1":
                raise ValueError("legacy task requires radar-legacy.v1")
            if any(
                value is not None
                for value in (
                    self.goal_payload,
                    self.current_plan_id,
                    self.pending_user_input_request_id,
                )
            ):
                raise ValueError("legacy tasks cannot persist dynamic runtime state")
        else:
            if self.runtime_contract_version != "product-episode.v1":
                raise ValueError(
                    "product task requires product-episode.v1"
                )
            if self.node_status:
                raise ValueError(
                    "product tasks cannot persist legacy node state"
                )
            if any(
                value is not None
                for value in (
                    self.current_plan_id,
                    self.pending_user_input_request_id,
                )
            ):
                raise ValueError(
                    "product task state belongs to ProductEpisodeRunner"
                )
        return self


class RadarArtifactVersion(RadarAgentSchema):
    artifact_version_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    artifact_type: str = Field(..., min_length=1)
    artifact_id: str = Field(..., min_length=1)
    version: int = Field(..., ge=1)
    report: RoleReportArtifact | None = None
    evidence_ledger: EvidenceLedger | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkflowRuntime(Protocol):
    """Lifecycle layer between API/CLI and OrchestratorAgent."""

    def create_task(
        self,
        *,
        subject_id: str,
        radar_device_id: str,
        role: str = "family",
        actor_id: str | None = None,
        authorization_id: str | None = None,
        scenario: str = "replay",
        idempotency_key: str | None = None,
    ) -> RadarAgentTask:
        ...

    def export_subject_data(
        self,
        subject_id: str,
        *,
        actor_id: str,
        role_binding_id: str,
        authorization_id: str,
    ) -> dict[str, Any]:
        ...

    def delete_subject_data(
        self,
        subject_id: str,
        *,
        actor_id: str,
        role_binding_id: str,
        authorization_id: str,
    ) -> dict[str, Any]:
        ...

    def list_role_reports(
        self,
        subject_id: str,
        *,
        actor_id: str,
        role_binding_id: str,
        authorization_id: str,
    ) -> list[RadarArtifactVersion]:
        ...

    def append_event(self, task_id: str, event: RadarTaskEvent) -> None:
        ...

    def save_artifact(
        self,
        task_id: str,
        artifact: RoleReportArtifact | EvidenceLedger,
    ) -> RadarArtifactVersion:
        ...

    def request_confirmation(
        self,
        task_id: str,
        request: HumanConfirmationRequest,
    ) -> HumanConfirmationRequest:
        ...

    def export_doctor_material(
        self,
        task_id: str,
        *,
        confirmation_id: str,
    ) -> RadarArtifactVersion:
        ...

    def revoke_confirmation(
        self,
        task_id: str,
        confirmation_id: str,
        *,
        actor_id: str,
        actor_role: str,
        reason: str,
    ) -> HumanConfirmationRequest:
        ...

    def complete_confirmation_action(
        self,
        task_id: str,
        confirmation_id: str,
        *,
        actor_id: str,
        actor_role: str,
        execution_ref: str,
        delivery_status: str | None = None,
    ) -> HumanConfirmationRequest:
        ...

    def forward_a2a_message(
        self,
        task_id: str,
        message: A2AMessage,
        *,
        approved_by_orchestrator: bool = True,
    ) -> A2AMessage:
        ...

    def approve_cross_task_share(
        self,
        source_task_id: str,
        target_task_id: str,
        *,
        sender: str,
        receiver: str,
        intent: str,
        shared_artifact_type: str,
        payload: dict[str, Any],
        evidence_refs: list[str] | None = None,
        approved_by_orchestrator: bool,
        collaboration_round: int = 1,
    ) -> A2AMessage:
        ...

    def get_task(self, task_id: str) -> RadarAgentTask:
        ...

    def list_artifacts(
        self,
        task_id: str,
        *,
        artifact_id: str | None = None,
    ) -> list[RadarArtifactVersion]:
        ...

    def list_confirmations(
        self,
        task_id: str,
    ) -> list[HumanConfirmationRequest]:
        ...


__all__ = [
    "RadarAgentTask",
    "RadarArtifactVersion",
    "RadarNodeStatus",
    "RadarTaskFailure",
    "RadarTaskEvent",
    "RadarTaskStatus",
    "WorkflowRuntime",
]
