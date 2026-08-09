from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import Field

from sleepagent.radar_agent.schemas import (
    EvidenceLedger,
    RadarAgentSchema,
    RoleReportArtifact,
)


CANONICAL_AGENT_RUNTIME_KIND = "product_episode"
CANONICAL_AGENT_RUNTIME_CONTRACT_VERSION = "product-episode.v1"


class RadarTaskStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_FOR_USER_INPUT = "waiting_for_user_input"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
    runtime_kind: Literal["product_episode"] = CANONICAL_AGENT_RUNTIME_KIND
    runtime_contract_version: Literal[
        "product-episode.v1"
    ] = CANONICAL_AGENT_RUNTIME_CONTRACT_VERSION
    execution_mode: Literal[
        "intelligent",
        "safe_degraded",
        "deterministic_only",
    ] | None = None
    completion_status: Literal[
        "complete", "partial", "blocked"
    ] | None = None
    goal_payload: dict[str, Any] | None = None
    task_version: int = Field(default=1, ge=1)
    status: RadarTaskStatus = RadarTaskStatus.CREATED
    retry_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=3, ge=0)
    idempotency_key: str | None = None
    failure: RadarTaskFailure | None = None
    parent_task_id: str | None = None
    last_event_sequence: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def decode_persisted_json(
        cls,
        value: str | bytes | bytearray,
    ) -> "RadarAgentTask":
        """Decode canonical rows written before retired state fields were removed."""

        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("persisted canonical task must be a JSON object")
        inert_compatibility_fields = {
            "node_status": {},
            "current_plan_id": None,
            "pending_user_input_request_id": None,
        }
        for field_name, inert_value in inert_compatibility_fields.items():
            if field_name not in payload:
                continue
            if payload[field_name] != inert_value:
                raise ValueError(
                    f"persisted canonical task contains active retired state: "
                    f"{field_name}"
                )
            payload.pop(field_name)
        return cls.model_validate(payload)


class UserInputRequest(RadarAgentSchema):
    """Runtime-neutral persisted request used by a canonical Product Episode."""

    request_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    question_id: str = Field(..., min_length=1)
    question_version: str = Field(..., min_length=1)
    question_text: str = Field(..., min_length=1, max_length=500)
    question_type: Literal[
        "observable_fact", "simple_context", "symptom_self_report"
    ]
    target_role: Literal["elder", "family", "doctor", "system"]
    answer_options: list[str] = Field(default_factory=list)
    why_needed: str = Field(..., min_length=1, max_length=500)
    decision_scope: str = Field(..., min_length=1, max_length=500)
    blocks_task: bool = True
    status: Literal["pending", "answered", "declined", "cancelled"] = "pending"
    asked_by_agent_invocation_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    cooldown_until: datetime | None = None
    resolved_at: datetime | None = None


class UserInputResponse(RadarAgentSchema):
    """Runtime-neutral persisted answer accepted by a Product Episode."""

    response_id: str = Field(..., min_length=1)
    request_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1, max_length=1000)
    answered_by_user_id: str = Field(..., min_length=1)
    answered_by_role: Literal["elder", "family", "doctor", "system"]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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


__all__ = [
    "CANONICAL_AGENT_RUNTIME_CONTRACT_VERSION",
    "CANONICAL_AGENT_RUNTIME_KIND",
    "RadarAgentTask",
    "RadarArtifactVersion",
    "RadarTaskFailure",
    "RadarTaskEvent",
    "RadarTaskStatus",
    "UserInputRequest",
    "UserInputResponse",
]
