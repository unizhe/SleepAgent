from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sleepagent.schemas.analysis import AnalysisRequest
from sleepagent.schemas.report import MockSleepReport
from sleepagent.schemas.sleep import SleepAnalysisResult


class StrictTaskSchema(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )


class TaskStatus(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ArtifactType(str, Enum):
    RISK_SUMMARY = "risk_summary"
    EVIDENCE_CHAIN = "evidence_chain"
    ELDER_REPORT = "elder_report"
    FAMILY_REPORT = "family_report"
    DOCTOR_REPORT = "doctor_report"
    TECHNICAL_REPORT = "technical_report"
    TREND_INTERPRETATION = "trend_interpretation"
    CARE_PLAN = "care_plan"


class ArtifactStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    REVISED = "revised"


class SafetyReviewStatus(str, Enum):
    NOT_REVIEWED = "not_reviewed"
    PASSED = "passed"
    BLOCKED = "blocked"


class ArtifactExportFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"
    CSV = "csv"


class AgentEventType(str, Enum):
    TASK_CREATED = "task_created"
    PLAN_CREATED = "plan_created"
    STEP_STARTED = "step_started"
    TOOL_CALLED = "tool_called"
    FINDING_CREATED = "finding_created"
    ARTIFACT_CREATED = "artifact_created"
    STEP_COMPLETED = "step_completed"
    TASK_COMPLETED = "task_completed"
    ERROR = "error"


class ToolCallStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class AgentPlanStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"


class NextActionType(str, Enum):
    CONFIRM = "confirm"
    NAVIGATE = "navigate"
    REVISE = "revise"
    EXPORT = "export"
    CARE_PLAN = "care_plan"
    CHAT = "chat"


class ToolCall(StrictTaskSchema):
    id: str = Field(..., min_length=1)
    tool_name: str = Field(..., alias="toolName", min_length=1)
    input_summary: str = Field(..., alias="inputSummary", min_length=1)
    output_summary: str = Field(..., alias="outputSummary", min_length=1)
    status: ToolCallStatus
    duration_ms: int = Field(..., alias="durationMs", ge=0)


class AgentPlanStep(StrictTaskSchema):
    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    agent: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    status: AgentPlanStepStatus = AgentPlanStepStatus.PENDING
    tool_calls: list[ToolCall] = Field(default_factory=list, alias="toolCalls")
    duration_ms: int = Field(default=0, alias="durationMs", ge=0)


class AgentEvent(StrictTaskSchema):
    id: str = Field(..., min_length=1)
    type: AgentEventType
    step_id: str | None = Field(default=None, alias="stepId", min_length=1)
    title: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] | None = None


class Artifact(StrictTaskSchema):
    id: str = Field(..., min_length=1)
    task_id: str | None = Field(default=None, alias="taskId", min_length=1)
    subject_id: str | None = Field(default=None, alias="subjectId", min_length=1)
    record_id: str | None = Field(default=None, alias="recordId", min_length=1)
    type: ArtifactType
    title: str = Field(..., min_length=1)
    status: ArtifactStatus
    content: str = Field(..., min_length=1)
    created_by_step_id: str = Field(..., alias="createdByStepId", min_length=1)
    current_version_id: str | None = Field(
        default=None,
        alias="currentVersionId",
        min_length=1,
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="createdAt",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="updatedAt",
    )


class ArtifactVersion(StrictTaskSchema):
    id: str = Field(..., min_length=1)
    artifact_id: str = Field(..., alias="artifactId", min_length=1)
    version_number: int = Field(..., alias="versionNumber", ge=1)
    content: str = Field(..., min_length=1)
    revision_instruction: str | None = Field(
        default=None,
        alias="revisionInstruction",
        min_length=1,
    )
    created_by: str = Field(default="system", alias="createdBy", min_length=1)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="createdAt",
    )
    safety_review_status: SafetyReviewStatus = Field(
        default=SafetyReviewStatus.NOT_REVIEWED,
        alias="safetyReviewStatus",
    )
    blocked_reasons: list[str] = Field(default_factory=list, alias="blockedReasons")
    reviewed_at: datetime | None = Field(default=None, alias="reviewedAt")
    reviewed_by: str | None = Field(default=None, alias="reviewedBy", min_length=1)


class ArtifactReviseRequest(StrictTaskSchema):
    revision_instruction: str = Field(..., alias="revisionInstruction", min_length=1)
    content: str = Field(..., min_length=1)
    created_by: str = Field(default="user", alias="createdBy", min_length=1)


class ArtifactExportRequest(StrictTaskSchema):
    format: ArtifactExportFormat = ArtifactExportFormat.MARKDOWN


class ArtifactExportResult(StrictTaskSchema):
    artifact_id: str = Field(..., alias="artifactId", min_length=1)
    format: ArtifactExportFormat
    filename: str = Field(..., min_length=1)
    media_type: str = Field(..., alias="mediaType", min_length=1)
    content: str = Field(..., min_length=1)
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        alias="generatedAt",
    )


class NextAction(StrictTaskSchema):
    id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    action_type: NextActionType = Field(..., alias="actionType")
    target: str | None = Field(default=None, min_length=1)
    requires_confirmation: bool = Field(default=False, alias="requiresConfirmation")


class TaskCreateRequest(StrictTaskSchema):
    title: str = Field(default="真实睡眠分析任务", min_length=1)
    user_goal: str = Field(
        default="基于本地 SHHS PSG 数据运行 SleepAgent v1 分析任务。",
        alias="userGoal",
        min_length=1,
    )
    record_id: str = Field(default="shhs1-200001", alias="recordId", min_length=1)
    patient_id: str | None = Field(default=None, alias="patientId", min_length=1)
    analysis_request: AnalysisRequest | None = Field(
        default=None,
        alias="analysisRequest",
    )
    use_deepseek_report: bool = Field(default=False, alias="useDeepseekReport")
    user_question: str | None = Field(default=None, alias="userQuestion", min_length=1)


class SleepAgentTask(StrictTaskSchema):
    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    user_goal: str = Field(..., alias="userGoal", min_length=1)
    status: TaskStatus
    patient_id: str = Field(..., alias="patientId", min_length=1)
    record_id: str = Field(..., alias="recordId", min_length=1)
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    analysis_request: AnalysisRequest = Field(alias="analysisRequest")
    use_deepseek_report: bool = Field(default=False, alias="useDeepseekReport")
    user_question: str | None = Field(default=None, alias="userQuestion", min_length=1)
    plan: list[AgentPlanStep] = Field(default_factory=list)
    events: list[AgentEvent] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    next_actions: list[NextAction] = Field(default_factory=list, alias="nextActions")
    errors: list[str] = Field(default_factory=list)
    analysis_result: SleepAnalysisResult | None = Field(
        default=None,
        alias="analysisResult",
    )
    report_result: MockSleepReport | None = Field(default=None, alias="reportResult")


class TaskConfirmRequest(StrictTaskSchema):
    run_synchronously: bool = Field(default=True, alias="runSynchronously")
