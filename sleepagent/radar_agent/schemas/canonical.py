from __future__ import annotations

import json
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sleepagent.product_device.schemas import (
    RadarDevice as ProductRadarDevice,
    RadarSleepReport as ProductRadarSleepReport,
    RadarSourceMetadata as ProductRadarSourceMetadata,
    RadarVitalSnapshot as ProductRadarVitalSnapshot,
    RawVendorEvent as ProductRawVendorEvent,
)


class StandardTerminologyMapping(BaseModel):
    """Reserved interoperability fields for later FHIR/LOINC/SNOMED mapping."""

    model_config = ConfigDict(extra="forbid")

    fhir_resource: str | None = None
    fhir_profile: str | None = None
    loinc_codes: list[str] = Field(default_factory=list)
    snomed_codes: list[str] = Field(default_factory=list)
    local_codes: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class RadarAgentSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    standard_mappings: StandardTerminologyMapping = Field(
        default_factory=StandardTerminologyMapping,
        description="Reserved FHIR/LOINC/SNOMED/local-code mapping fields.",
    )


class ReviewStatus(str, Enum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    REJECTED = "rejected"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class RiskLevel(str, Enum):
    INFO = "info"
    WATCH = "watch"
    ESCALATE = "escalate"
    URGENT_BOUNDARY = "urgent_boundary"
    UNCERTAIN = "uncertain"


class RadarDeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class RadarDataQualityStatus(str, Enum):
    GOOD = "good"
    PARTIAL = "partial"
    UNUSABLE = "unusable"


class RadarBedPresence(str, Enum):
    IN_BED = "in_bed"
    OUT_OF_BED = "out_of_bed"
    UNKNOWN = "unknown"


class RadarAgentName(str, Enum):
    ORCHESTRATOR = "orchestrator"
    RADAR_DATA = "radar_data"
    TREND = "trend"
    RISK_SIGNAL = "risk_signal"
    RAG = "rag"
    REPORT = "report"
    DIALOGUE = "dialogue"
    ALERT_CARE = "alert_care"
    MEMORY = "memory"


class RadarRawEvent(RadarAgentSchema):
    """Raw vendor event retained only for trace and replay validation."""

    raw_event_id: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event_timestamp: datetime | None = None
    vendor_device_id: str | None = None
    vendor_device_name: str | None = None
    vendor_home_id: str | None = None
    idempotency_key: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    data_payload: dict[str, Any] = Field(default_factory=dict)
    replay_scenario: str | None = None
    data_use: Literal["trace", "replay"] = "trace"
    retained_for: list[Literal["trace", "replay_validation"]] = Field(
        default_factory=lambda: ["trace", "replay_validation"]
    )


class RadarDevice(RadarAgentSchema):
    radar_device_id: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    provider: str = Field(..., min_length=1)
    status: RadarDeviceStatus = RadarDeviceStatus.UNKNOWN
    vendor_device_id: str | None = None
    vendor_device_name: str | None = None
    vendor_home_id: str | None = None
    bound_subject_id: str | None = None
    timezone_name: str = "UTC"
    firmware_version: str | None = None
    source_raw_event_ids: list[str] = Field(default_factory=list)
    registered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RadarVitalSnapshot(RadarAgentSchema):
    snapshot_id: str = Field(..., min_length=1)
    radar_device_id: str = Field(..., min_length=1)
    subject_id: str | None = None
    measured_at: datetime
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    heart_rate_bpm: int | None = Field(default=None, ge=0, le=240)
    breath_rate_bpm: int | None = Field(default=None, ge=0, le=80)
    body_movement: float | None = Field(default=None, ge=0)
    bed_presence: RadarBedPresence = RadarBedPresence.UNKNOWN
    invalid_reading_flags: list[str] = Field(default_factory=list)
    data_quality_flags: list[str] = Field(default_factory=list)
    source_raw_event_ids: list[str] = Field(default_factory=list)


class RadarNightSummary(RadarAgentSchema):
    radar_device_id: str = Field(..., min_length=1)
    subject_id: str | None = None
    night_of: date
    timezone_name: str = "UTC"
    night_boundary_start_at: datetime | None = None
    night_boundary_end_at: datetime | None = None
    device_status: RadarDeviceStatus = RadarDeviceStatus.UNKNOWN
    sleep_start_at: datetime | None = None
    sleep_end_at: datetime | None = None
    total_sleep_minutes: float | None = Field(default=None, ge=0)
    sleep_score: float | None = Field(default=None, ge=0, le=100)
    out_of_bed_count: int = Field(default=0, ge=0)
    movement_count: int = Field(default=0, ge=0)
    data_coverage_ratio: float = Field(default=0, ge=0, le=1)
    data_quality_status: RadarDataQualityStatus = RadarDataQualityStatus.GOOD
    confidence_label: Literal[
        "normal",
        "low_confidence",
        "not_interpretable",
    ] = "normal"
    health_conclusion_allowed: bool = True
    invalid_reading_count: int = Field(default=0, ge=0)
    abnormal_reading_count: int = Field(default=0, ge=0)
    missing_intervals: list[str] = Field(default_factory=list)
    out_of_bed_intervals: list[str] = Field(default_factory=list)
    not_in_bed_intervals: list[str] = Field(default_factory=list)
    quality_reasons: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    explainable_metrics: dict[str, Any] = Field(default_factory=dict)
    source_snapshot_ids: list[str] = Field(default_factory=list)
    source_raw_event_ids: list[str] = Field(default_factory=list)
    source_report_ref: str | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_sleep_time_order(self) -> "RadarNightSummary":
        if (
            self.sleep_start_at is not None
            and self.sleep_end_at is not None
            and self.sleep_end_at <= self.sleep_start_at
        ):
            raise ValueError("sleep_end_at must be after sleep_start_at.")
        return self


class QuestionnaireEntry(RadarAgentSchema):
    entry_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    role: Literal["elder", "family", "doctor"]
    question_id: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)
    answer_type: Literal["choice", "scale", "short_text"] = "choice"
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: Literal["micro_questionnaire", "sleep_diary", "doctor_note"] = (
        "micro_questionnaire"
    )
    question_source: Literal["bank", "skill", "legacy"] = "legacy"
    source_id: str | None = None
    source_version: str | None = None
    policy_id: str | None = None
    policy_version: str | None = None
    trigger: str | None = None
    prompt_text: str | None = None
    evidence_ref: str | None = None

    @model_validator(mode="after")
    def versioned_questions_need_provenance(self) -> "QuestionnaireEntry":
        if self.question_source in {"bank", "skill"}:
            required = {
                "source_id": self.source_id,
                "source_version": self.source_version,
                "policy_id": self.policy_id,
                "policy_version": self.policy_version,
                "trigger": self.trigger,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(
                    "versioned questionnaire entries require provenance: "
                    + ", ".join(missing)
                )
        return self


class QuestionnaireQuestion(RadarAgentSchema):
    question_id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1, max_length=120)
    answer_type: Literal["choice", "scale", "short_text"] = "choice"
    options: list[str] = Field(default_factory=list, max_length=7)
    applicable_roles: list[Literal["elder", "family", "doctor"]] = Field(
        default_factory=lambda: ["elder", "family", "doctor"]
    )
    role_text: dict[Literal["elder", "family", "doctor"], str] = Field(
        default_factory=dict
    )
    triggers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def choices_need_options_and_short_role_text(self) -> "QuestionnaireQuestion":
        if self.answer_type in {"choice", "scale"} and not self.options:
            raise ValueError("choice/scale questionnaire questions require options.")
        if any(len(text) > 120 for text in self.role_text.values()):
            raise ValueError("role-adapted questionnaire text must stay short.")
        return self


class QuestionnaireBank(RadarAgentSchema):
    bank_id: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    questions: dict[str, str | QuestionnaireQuestion] = Field(default_factory=dict)
    reviewed: bool = False

    @model_validator(mode="after")
    def reviewed_banks_need_questions(self) -> "QuestionnaireBank":
        if self.reviewed and not self.questions:
            raise ValueError("reviewed questionnaire banks require questions.")
        for question_id, question in self.questions.items():
            if isinstance(question, QuestionnaireQuestion) and question.question_id != question_id:
                raise ValueError("question map keys must match QuestionnaireQuestion.question_id.")
        return self


class QuestionnairePolicy(RadarAgentSchema):
    policy_id: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    trigger: str = Field(..., min_length=1)
    allowed_question_ids: list[str] = Field(default_factory=list)
    applicable_roles: list[Literal["elder", "family", "doctor"]] = Field(
        default_factory=list
    )
    max_questions_per_turn: int = Field(default=3, ge=1, le=3)
    cooldown_hours: int = Field(default=24, ge=0)
    enabled: bool = True


class SupplementaryDocument(RadarAgentSchema):
    document_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    document_type: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    summary: str = ""
    source_uri: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    review_status: ReviewStatus = ReviewStatus.DRAFT
    caveats: list[str] = Field(default_factory=list)


class EvidenceClaim(RadarAgentSchema):
    claim_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    source_kind: Literal[
        "canonical_observation",
        "user_report",
        "authorized_observer_report",
        "reviewed_knowledge",
        "trend_tool",
        "confirmed_memory",
        "accepted_ledger",
        "data_quality",
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    risk_level: RiskLevel = RiskLevel.INFO
    uncertainty: str | None = None
    caveats: list[str] = Field(default_factory=list)
    generated_by: str = Field(..., min_length=1)
    review_status: ReviewStatus = ReviewStatus.DRAFT

    @model_validator(mode="after")
    def reviewed_claims_need_evidence(self) -> "EvidenceClaim":
        if self.review_status == ReviewStatus.REVIEWED and not self.evidence_refs:
            raise ValueError("reviewed evidence claims require evidence_refs.")
        return self


class EvidenceLedger(RadarAgentSchema):
    ledger_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    raw_evidence_refs: list[str] = Field(default_factory=list)
    canonical_evidence_refs: list[str] = Field(default_factory=list)
    derived_metrics: dict[str, Any] = Field(default_factory=dict)
    questionnaire_entries: list[QuestionnaireEntry] = Field(default_factory=list)
    supplementary_documents: list[SupplementaryDocument] = Field(default_factory=list)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    uncertainty: str | None = None
    caveats: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.DRAFT
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def block_direct_raw_event_reads(self) -> "EvidenceLedger":
        _assert_no_direct_raw_event(
            {
                "derived_metrics": self.derived_metrics,
                "questionnaire_entries": self.questionnaire_entries,
                "supplementary_documents": self.supplementary_documents,
                "claims": self.claims,
            },
            owner="EvidenceLedger",
        )
        return self


class A2AMessage(RadarAgentSchema):
    message_id: str = Field(..., min_length=1)
    sender: str = Field(..., min_length=1)
    receiver: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    target_task_id: str | None = None
    intent: str = Field(..., min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    requested_action: str | None = None
    request_type: Literal["evidence", "critique", "revision"] = "critique"
    caused_by_invocation_id: str | None = None
    target_invocation_id: str | None = None
    expected_output_schema: str = "AgentWorkProduct.v1"
    resolution_status: Literal[
        "pending", "accepted", "changed", "rejected", "unresolved"
    ] = "pending"
    resolution_summary: str | None = Field(default=None, max_length=1000)
    risk_level: RiskLevel = RiskLevel.INFO
    requires_approval: bool = False
    collaboration_round: int = Field(default=1, ge=1, le=2)
    routed_by: str = "orchestrator"
    shared_artifact_type: Literal[
        "trend_summary",
        "pattern",
        "suggestion",
    ] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    message_status: Literal["queued", "delivered", "handled", "rejected"] = "queued"
    source_agent_invocation_id: str | None = None
    target_agent_invocation_id: str | None = None
    accepted_by_orchestrator_at: datetime | None = None
    handled_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_a2a_boundaries(self) -> "A2AMessage":
        if self.routed_by != RadarAgentName.ORCHESTRATOR.value:
            raise ValueError("A2A messages must be routed by the Orchestrator.")
        _assert_no_direct_raw_event(self.payload, owner="A2AMessage")
        _assert_no_forbidden_a2a_payload(self.payload)
        if self.target_task_id and self.target_task_id != self.task_id:
            if self.shared_artifact_type is None:
                raise ValueError(
                    "cross-task A2A messages require a shared_artifact_type."
                )
            if not self.requires_approval:
                raise ValueError("cross-task A2A messages require approval.")
        if (
            self.message_status == "handled"
            and self.source_agent_invocation_id is not None
            and (self.target_agent_invocation_id is None or self.handled_at is None)
        ):
            raise ValueError(
                "dynamic handled A2A requires the actual target Agent invocation link"
            )
        if (
            self.caused_by_invocation_id is not None
            and self.source_agent_invocation_id is not None
            and self.caused_by_invocation_id != self.source_agent_invocation_id
        ):
            raise ValueError("A2A causal source invocation fields must agree")
        if (
            self.target_invocation_id is not None
            and self.target_agent_invocation_id is not None
            and self.target_invocation_id != self.target_agent_invocation_id
        ):
            raise ValueError("A2A target invocation fields must agree")
        return self

    def is_real_dynamic_collaboration(self) -> bool:
        return bool(
            self.message_status == "handled"
            and self.source_agent_invocation_id
            and self.target_agent_invocation_id
            and self.accepted_by_orchestrator_at
            and self.handled_at
        )


class ConflictRecord(RadarAgentSchema):
    conflict_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    sources: list[str] = Field(default_factory=list)
    summary: str = Field(..., min_length=1)
    decision: str = Field(..., min_length=1)
    final_status: Literal["accepted", "downgraded", "rejected", "uncertain"]
    requires_human_confirmation: bool = False
    evidence_refs: list[str] = Field(default_factory=list)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def conflict_records_need_sources_and_decision(self) -> "ConflictRecord":
        if not self.sources:
            raise ValueError("conflict records require sources.")
        if not self.decision.strip():
            raise ValueError("conflict records require a decision rationale.")
        return self


class RoleReportArtifact(RadarAgentSchema):
    schema_version: str = "radar-role-report.v1"
    artifact_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    role: Literal["elder", "family", "doctor"]
    title: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    source_ledger_id: str | None = None
    risk_level: RiskLevel = RiskLevel.INFO
    claim_ids: list[str] = Field(default_factory=list)
    facts: list[EvidenceClaim] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    trend_highlights: list[str] = Field(default_factory=list)
    anomaly_highlights: list[str] = Field(default_factory=list)
    confirmation_actions: list[str] = Field(default_factory=list)
    data_quality: dict[str, Any] = Field(default_factory=dict)
    questionnaire_entries: list[QuestionnaireEntry] = Field(default_factory=list)
    structured_summary: dict[str, Any] = Field(default_factory=dict)
    caveats: list[str] = Field(default_factory=list)
    safety_notices: list[str] = Field(default_factory=list)
    prompt_version: str = "role-report-template.v1"
    model_provider: str = "deterministic-template"
    model_id: str = "template"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    generation_mode: Literal["template", "llm", "fallback"] = "template"

    @model_validator(mode="after")
    def generated_report_keeps_one_fact_snapshot(self) -> "RoleReportArtifact":
        if self.source_ledger_id:
            required_notices = {
                "本报告由 AI 辅助整理，内容来自结构化证据。",
                "本报告仅用于睡眠健康观察。",
                "本报告不构成临床诊断或医疗建议。",
                "本项目不宣称 HIPAA/FDA、医疗器械或临床诊断合规。",
            }
            if not required_notices.issubset(set(self.safety_notices)):
                raise ValueError(
                    "generated reports require AI-assisted, observation, "
                    "non-diagnostic, and non-compliance notices."
                )
            if any(claim.task_id != self.task_id for claim in self.facts):
                raise ValueError("role report facts must belong to the same task.")
            if self.claim_ids != [claim.claim_id for claim in self.facts]:
                raise ValueError(
                    "role report claim_ids must match its Evidence Ledger fact snapshot."
                )
            fact_refs = {
                ref for claim in self.facts for ref in claim.evidence_refs
            }
            if not fact_refs.issubset(set(self.evidence_refs)):
                raise ValueError(
                    "role report evidence_refs must include every fact evidence ref."
                )
            if not set(self.evidence_refs).issubset(set(self.source_refs)):
                raise ValueError(
                    "role report source_refs must include every displayed evidence ref."
                )
            if self.structured_summary:
                if (
                    self.structured_summary.get("source_ledger_id")
                    != self.source_ledger_id
                    or self.structured_summary.get("risk_level")
                    != self.risk_level.value
                    or self.structured_summary.get("claim_ids") != self.claim_ids
                ):
                    raise ValueError(
                        "role report structured summary must match Ledger, risk, and claims."
                    )
            if self.role == "doctor":
                expected_chain = [
                    claim.model_dump(mode="json") for claim in self.facts
                ]
                expected_questionnaires = [
                    entry.model_dump(mode="json")
                    for entry in self.questionnaire_entries
                ]
                if (
                    self.structured_summary.get("evidence_chain") != expected_chain
                    or self.structured_summary.get("data_quality") != self.data_quality
                    or self.structured_summary.get("questionnaire_entries")
                    != expected_questionnaires
                    or self.structured_summary.get("source_refs") != self.source_refs
                    or self.structured_summary.get("caveats") != self.caveats
                    or self.structured_summary.get("safety_notices")
                    != self.safety_notices
                    or self.structured_summary.get("non_diagnostic_boundary") is not True
                ):
                    raise ValueError(
                        "doctor report must preserve its complete structured evidence package."
                    )
        serialized = json.dumps(
            {
                "title": self.title,
                "content": self.content,
                "caveats": self.caveats,
                "safety_notices": self.safety_notices,
            },
            ensure_ascii=False,
        ).lower()
        forbidden_compliance_claims = (
            "hipaa compliant",
            "fda approved",
            "符合 hipaa",
            "通过 fda",
            "医疗器械认证",
            "临床诊断合规系统",
        )
        if any(claim in serialized for claim in forbidden_compliance_claims):
            raise ValueError("reports cannot claim medical or regulatory compliance")
        return self


class HumanConfirmationRequest(RadarAgentSchema):
    confirmation_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    action_type: str = Field(..., min_length=1)
    requested_role: Literal["elder", "family", "doctor", "system"]
    allowed_roles: list[Literal["elder", "family", "doctor", "system"]] = Field(
        default_factory=list
    )
    reason: str = Field(..., min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    status: Literal["pending", "approved", "rejected", "expired", "revoked"] = (
        "pending"
    )
    confirmation_kind: Literal[
        "approval", "delivery_record", "doctor_annotation"
    ] = "approval"
    blocks_daily_flow: bool = True
    idempotency_key: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    revocation_reason: str | None = None
    execution_status: Literal["not_started", "completed", "failed"] = "not_started"
    execution_ref: str | None = None
    executed_at: datetime | None = None
    delivery_status: Literal[
        "not_applicable", "pending", "delivered", "failed"
    ] = "not_applicable"
    delivered_at: datetime | None = None

    @model_validator(mode="after")
    def validate_resolution_time(self) -> "HumanConfirmationRequest":
        if self.resolved_at is not None and self.resolved_at < self.created_at:
            raise ValueError("resolved_at cannot be before created_at.")
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("revoked_at cannot be before created_at.")
        if self.status in {"approved", "rejected", "expired"} and (
            self.resolved_at is None or not self.resolved_by
        ):
            raise ValueError("resolved confirmations require actor and timestamp.")
        if self.status == "revoked" and (
            self.revoked_at is None or not self.revoked_by
        ):
            raise ValueError("revoked confirmations require actor and timestamp.")
        if self.execution_status == "completed" and (
            not self.execution_ref or self.executed_at is None
        ):
            raise ValueError("completed confirmation action requires ref and timestamp.")
        if self.delivery_status == "delivered" and self.delivered_at is None:
            raise ValueError("delivered confirmation requires delivered_at.")
        if not self.allowed_roles:
            self.allowed_roles = [self.requested_role]
        return self


class MemoryCandidate(RadarAgentSchema):
    candidate_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    memory_type: Literal["trend", "preference", "care_event"]
    summary: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    privacy_tags: list[str] = Field(default_factory=list)
    authorization_refs: list[str] = Field(default_factory=list)
    confirmation_action: str = "write_long_term_memory"
    requires_confirmation: bool = True
    approved: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TaskContext(RadarAgentSchema):
    task_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    role: Literal["elder", "family", "doctor", "system"] = "family"
    stage: str = "created"
    purpose: Literal[
        "orchestration",
        "analysis",
        "report",
        "chat",
        "risk",
        "alert",
        "rag",
        "memory",
    ] = "orchestration"
    allowed_actions: list[str] = Field(default_factory=list)


class EvidencePacket(RadarAgentSchema):
    evidence_refs: list[str] = Field(default_factory=list)
    vital_snapshots: list[RadarVitalSnapshot] = Field(default_factory=list)
    night_summaries: list[RadarNightSummary] = Field(default_factory=list)
    questionnaire_entries: list[QuestionnaireEntry] = Field(default_factory=list)
    supplementary_documents: list[SupplementaryDocument] = Field(default_factory=list)
    data_quality: dict[str, Any] = Field(default_factory=dict)
    claim_refs: list[str] = Field(default_factory=list)
    evidence_ledger: EvidenceLedger | None = None

    @model_validator(mode="after")
    def block_direct_raw_event_reads(self) -> "EvidencePacket":
        _assert_no_direct_raw_event(self.data_quality, owner="EvidencePacket")
        return self


class RagContext(RadarAgentSchema):
    chunk_ids: list[str] = Field(default_factory=list)
    citation_ids: list[str] = Field(default_factory=list)
    snippets: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    source_metadata: list[dict[str, Any]] = Field(default_factory=list)


class SafetyPolicy(RadarAgentSchema):
    forbidden_outputs: list[str] = Field(default_factory=list)
    requires_confirmation_for: list[str] = Field(default_factory=list)
    max_risk_level: RiskLevel = RiskLevel.ESCALATE


class ContextPacket(RadarAgentSchema):
    context_packet_id: str | None = None
    version: str = "1.0.0"
    task_context: TaskContext
    evidence_packet: EvidencePacket = Field(default_factory=EvidencePacket)
    memory_snippets: list[str] = Field(default_factory=list)
    rag_context: RagContext = Field(default_factory=RagContext)
    safety_policy: SafetyPolicy = Field(default_factory=SafetyPolicy)
    a2a_messages: list[A2AMessage] = Field(default_factory=list)

    @model_validator(mode="after")
    def block_direct_raw_event_reads(self) -> "ContextPacket":
        _assert_no_direct_raw_event(
            {
                "evidence_packet": self.evidence_packet,
                "memory_snippets": self.memory_snippets,
                "rag_context": self.rag_context,
                "a2a_messages": self.a2a_messages,
            },
            owner=f"ContextPacket:{self.task_context.purpose}",
        )
        return self


class AgentResult(RadarAgentSchema):
    agent_name: RadarAgentName
    skill_version: str = "1.0.0"
    prompt_version: str = "1.0.0"
    claims: list[EvidenceClaim] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    uncertainties: list[str] = Field(default_factory=list)
    next_requests: list[A2AMessage] = Field(default_factory=list)
    safety_flags: list[str] = Field(default_factory=list)
    candidate_actions: list[str] = Field(default_factory=list)
    output_payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def block_direct_raw_event_reads(self) -> "AgentResult":
        _assert_no_direct_raw_event(self.output_payload, owner="AgentResult")
        return self


def radar_raw_event_from_product_event(event: ProductRawVendorEvent) -> RadarRawEvent:
    return RadarRawEvent(
        raw_event_id=event.raw_event_id,
        provider=event.vendor,
        event_type=event.event_type,
        received_at=event.received_at,
        event_timestamp=event.event_timestamp,
        vendor_device_id=event.device_id,
        vendor_device_name=event.device_name,
        vendor_home_id=event.home_id,
        idempotency_key=event.message_id,
        raw_payload=event.raw_payload,
        data_payload=event.data_payload,
    )


def radar_device_from_product_device(device: ProductRadarDevice) -> RadarDevice:
    return RadarDevice(
        radar_device_id=device.radar_device_id,
        display_name=device.display_name,
        provider=device.provider,
        status=RadarDeviceStatus(device.status.value),
        vendor_device_id=device.vendor_device_id,
        vendor_device_name=device.vendor_device_name,
        vendor_home_id=device.vendor_home_id,
        timezone_name=device.timezone_name,
        source_raw_event_ids=_source_raw_event_ids(device.source_metadata),
        registered_at=device.registered_at,
        updated_at=device.updated_at,
    )


def radar_vital_snapshot_from_product_snapshot(
    snapshot: ProductRadarVitalSnapshot,
) -> RadarVitalSnapshot:
    source_ids = _source_raw_event_ids(snapshot.source_metadata)
    snapshot_id = source_ids[0] if source_ids else _build_snapshot_id(snapshot)
    return RadarVitalSnapshot(
        snapshot_id=snapshot_id,
        radar_device_id=snapshot.radar_device_id,
        measured_at=snapshot.measured_at,
        received_at=snapshot.received_at,
        heart_rate_bpm=snapshot.heart_rate_bpm,
        breath_rate_bpm=snapshot.breath_rate_bpm,
        body_movement=snapshot.body_movement,
        bed_presence=RadarBedPresence(snapshot.bed_presence.value),
        invalid_reading_flags=snapshot.invalid_reading_flags,
        source_raw_event_ids=source_ids,
    )


def radar_night_summary_from_product_report(
    report: ProductRadarSleepReport,
    *,
    subject_id: str | None = None,
) -> RadarNightSummary:
    return RadarNightSummary(
        radar_device_id=report.radar_device_id,
        subject_id=subject_id,
        night_of=report.report_date,
        sleep_start_at=report.sleep_start_at,
        sleep_end_at=report.sleep_end_at,
        total_sleep_minutes=report.total_sleep_minutes,
        sleep_score=report.sleep_score,
        out_of_bed_count=report.getup_count or 0,
        movement_count=report.movement_count or 0,
        data_coverage_ratio=1.0,
        source_raw_event_ids=_source_raw_event_ids(report.source_metadata),
        source_report_ref=f"product-sleep-report:{report.radar_device_id}:{report.report_date.isoformat()}",
    )


def _source_raw_event_ids(metadata: ProductRadarSourceMetadata | None) -> list[str]:
    if metadata is None or not metadata.raw_event_id:
        return []
    return [metadata.raw_event_id]


def _build_snapshot_id(snapshot: ProductRadarVitalSnapshot) -> str:
    timestamp = snapshot.measured_at.isoformat()
    return f"snapshot:{snapshot.radar_device_id}:{timestamp}"


def _assert_no_direct_raw_event(value: Any, *, owner: str) -> None:
    if _contains_direct_raw_event(value):
        raise ValueError(
            f"{owner} cannot directly contain RadarRawEvent or raw vendor payloads; "
            "use canonical schemas and raw_evidence_refs instead."
        )


def _contains_direct_raw_event(value: Any) -> bool:
    if isinstance(value, RadarRawEvent):
        return True
    if isinstance(value, BaseModel):
        if value.__class__.__name__ in {"RadarRawEvent", "RawVendorEvent"}:
            return True
        return _contains_direct_raw_event(value.model_dump(mode="python"))
    if isinstance(value, dict):
        keys = set(value)
        if {"raw_event_id", "raw_payload"}.issubset(keys):
            return True
        return any(_contains_direct_raw_event(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_direct_raw_event(item) for item in value)
    return False


_FORBIDDEN_A2A_KEYS = {
    "raw_radar_stream",
    "raw_radar_frames",
    "raw_payload",
    "full_conversation",
    "conversation_history",
    "conversation_transcript",
    "family_privacy",
    "home_address",
    "phone_number",
    "identity_card",
    "private_note",
    "pii",
}

_FORBIDDEN_A2A_TEXT = (
    "raw_radar_stream",
    "raw payload",
    "raw_payload",
    "full conversation",
    "full_conversation",
    "conversation transcript",
    "家庭隐私",
    "完整对话",
    "原始雷达",
    "home address",
    "phone number",
    "identity card",
)


def _assert_no_forbidden_a2a_payload(value: Any) -> None:
    if _contains_forbidden_a2a_payload(value):
        raise ValueError(
            "A2A messages cannot share raw radar streams, full conversations, "
            "family privacy, or other sensitive payloads."
        )


def _contains_forbidden_a2a_payload(value: Any) -> bool:
    if isinstance(value, BaseModel):
        return _contains_forbidden_a2a_payload(value.model_dump(mode="python"))
    if isinstance(value, dict):
        if {str(key).lower() for key in value}.intersection(_FORBIDDEN_A2A_KEYS):
            return True
        return any(_contains_forbidden_a2a_payload(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_forbidden_a2a_payload(item) for item in value)
    if isinstance(value, str):
        lowered = value.lower()
        return any(marker in lowered for marker in _FORBIDDEN_A2A_TEXT)
    return False


__all__ = [
    "A2AMessage",
    "AgentResult",
    "ConflictRecord",
    "ContextPacket",
    "EvidenceClaim",
    "EvidenceLedger",
    "EvidencePacket",
    "HumanConfirmationRequest",
    "MemoryCandidate",
    "QuestionnaireBank",
    "QuestionnaireEntry",
    "QuestionnairePolicy",
    "QuestionnaireQuestion",
    "RadarAgentName",
    "RadarAgentSchema",
    "RadarBedPresence",
    "RadarDataQualityStatus",
    "RadarDevice",
    "RadarDeviceStatus",
    "RadarNightSummary",
    "RadarRawEvent",
    "RadarVitalSnapshot",
    "RagContext",
    "ReviewStatus",
    "RiskLevel",
    "RoleReportArtifact",
    "SafetyPolicy",
    "StandardTerminologyMapping",
    "SupplementaryDocument",
    "TaskContext",
    "radar_device_from_product_device",
    "radar_night_summary_from_product_report",
    "radar_raw_event_from_product_event",
    "radar_vital_snapshot_from_product_snapshot",
]
