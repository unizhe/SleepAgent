from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field, model_validator

from sleepagent.radar_agent.schemas import (
    RadarAgentSchema,
    RiskLevel,
)


class RadarSubject(RadarAgentSchema):
    subject_id: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    timezone_name: str = "UTC"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RadarUserRoleBinding(RadarAgentSchema):
    role_binding_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    role: Literal["elder", "family", "doctor", "system"]
    display_name: str = Field(..., min_length=1)
    permissions: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RadarDataAuthorization(RadarAgentSchema):
    authorization_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    granted_by_user_id: str = Field(..., min_length=1)
    granted_by_role: Literal["elder", "family"]
    scopes: list[
        Literal[
            "process_radar_summary",
            "process_questionnaire",
            "process_supplementary_document",
            "export_data",
            "delete_data",
        ]
    ] = Field(default_factory=list)
    status: Literal["active", "revoked", "expired"] = "active"
    granted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None

    @model_validator(mode="after")
    def authorization_is_explicit_and_traceable(self) -> "RadarDataAuthorization":
        if not self.scopes:
            raise ValueError("data authorization requires at least one scope")
        if self.status == "revoked" and (
            self.revoked_at is None or not self.revoked_by
        ):
            raise ValueError("revoked authorization requires actor and timestamp")
        return self


class RadarAlertRecord(RadarAgentSchema):
    alert_id: str = Field(..., min_length=1)
    task_id: str | None = None
    subject_id: str | None = None
    risk_level: RiskLevel = RiskLevel.INFO
    status: Literal["candidate", "pending_confirmation", "sent", "resolved"] = (
        "candidate"
    )
    title: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None


class RadarAuditLogEntry(RadarAgentSchema):
    audit_id: str = Field(..., min_length=1)
    task_id: str | None = None
    actor: str = Field(..., min_length=1)
    action: str = Field(..., min_length=1)
    target_ref: str | None = None
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RadarMemorySummary(RadarAgentSchema):
    memory_summary_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    task_id: str | None = None
    memory_type: Literal["trend", "preference", "care_event"]
    summary: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    source_candidate_id: str | None = None
    confirmation_id: str | None = None
    privacy_reviewed: bool = False
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RadarReportArtifactVersion(RadarAgentSchema):
    artifact_version_id: str = Field(..., min_length=1)
    artifact_id: str = Field(..., min_length=1)
    version_number: int = Field(..., ge=1)
    content: str = Field(..., min_length=1)
    source_claim_ids: list[str] = Field(default_factory=list)
    prompt_version: str = Field(..., min_length=1)
    model_provider: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    generation_mode: Literal["template", "llm", "fallback"] = "template"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def generated_reports_keep_source_claims(self) -> "RadarReportArtifactVersion":
        if self.generation_mode in {"llm", "fallback"} and not self.source_claim_ids:
            raise ValueError("generated report versions require source_claim_ids.")
        return self


class VectorDocument(RadarAgentSchema):
    document_id: str = Field(..., min_length=1)
    collection: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None


class ObjectBlobReference(RadarAgentSchema):
    object_id: str = Field(..., min_length=1)
    bucket: str = Field(..., min_length=1)
    key: str = Field(..., min_length=1)
    content_type: str = "application/octet-stream"
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ObjectBlobReference",
    "RadarAlertRecord",
    "RadarAuditLogEntry",
    "RadarDataAuthorization",
    "RadarMemorySummary",
    "RadarReportArtifactVersion",
    "RadarSubject",
    "RadarUserRoleBinding",
    "VectorDocument",
]
