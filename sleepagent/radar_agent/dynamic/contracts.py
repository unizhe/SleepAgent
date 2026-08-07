from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import Field, model_validator

from sleepagent.radar_agent.schemas import RadarAgentSchema


DYNAMIC_RUNTIME_CONTRACT_VERSION = "radar-dynamic.v1"


class GoalType(str, Enum):
    NIGHT_REVIEW = "night_review"
    TREND_COMPARISON = "trend_comparison"
    CHANGE_EXPLANATION = "change_explanation"
    DATA_QUALITY_DIAGNOSIS = "data_quality_diagnosis"
    DOCTOR_MATERIAL = "doctor_material"
    GROUNDED_QUESTION = "grounded_question"


class ExecutionMode(str, Enum):
    INTELLIGENT = "intelligent"
    SAFE_DEGRADED = "safe_degraded"
    LEGACY_FIXED = "legacy_fixed"


class CompletionStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class AgentKind(str, Enum):
    ORCHESTRATOR = "orchestrator"
    EVIDENCE_ANALYSIS = "evidence_analysis"
    CARE_PLANNING = "care_planning"
    SAFETY_REVIEW = "safety_review"
    DIALOGUE = "dialogue"


class PlanStepKind(str, Enum):
    CALL_AGENT = "call_agent"
    CALL_TOOL = "call_tool"
    ASK_USER = "ask_user"
    REQUEST_CONFIRMATION = "request_confirmation"
    COMPLETE = "complete"
    EVALUATE = "evaluate"
    # Source-compatible aliases for the first implementation. Serialized plans
    # use the explicit contract names above.
    AGENT = "call_agent"
    TOOL = "call_tool"
    USER_INPUT = "ask_user"


class InvocationOutcome(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    UNKNOWN_OUTCOME = "unknown_outcome"


class AgentInvocationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN_OUTCOME = "unknown_outcome"


class EvaluationDecision(str, Enum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    REPLAN = "replan"
    ASK_USER = "ask_user"
    REQUEST_CONFIRMATION = "request_confirmation"
    BLOCK = "block"
    COMPLETE_PARTIAL = "complete_partial"
    WAIT_FOR_USER_INPUT = "ask_user"
    FAIL = "block"


class UserGoal(RadarAgentSchema):
    """An explicit, bounded user intent. Replay scenario names are absent by design."""

    goal_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    requesting_actor_id: str = Field(..., min_length=1)
    goal_type: GoalType
    target_date: date | None = None
    range_start: date | None = None
    range_end: date | None = None
    question: str | None = Field(default=None, max_length=1200)
    source_artifact_id: str | None = None
    source_date: date | None = None
    focus: str | None = Field(default=None, max_length=500)
    resolved_timezone_name: str = Field(default="UTC", min_length=1, max_length=120)
    requested_role: Literal["elder", "family", "doctor", "system"]
    requested_outputs: list[Literal["summary", "trend", "explanation", "quality", "doctor_material", "answer"]] = Field(
        default_factory=list
    )
    allowed_action_scope: list[
        Literal["read_evidence", "prepare_artifact", "request_user_input", "request_confirmation"]
    ] = Field(default_factory=lambda: ["read_evidence", "prepare_artifact"])
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def require_exact_scope(self) -> "UserGoal":
        single_night = {
            GoalType.NIGHT_REVIEW,
            GoalType.DATA_QUALITY_DIAGNOSIS,
            GoalType.DOCTOR_MATERIAL,
        }
        range_goals = {GoalType.TREND_COMPARISON, GoalType.CHANGE_EXPLANATION}
        if self.goal_type in single_night and self.target_date is None:
            raise ValueError(f"{self.goal_type.value} requires target_date")
        if self.goal_type in range_goals:
            if self.range_start is None or self.range_end is None:
                raise ValueError(f"{self.goal_type.value} requires an exact date range")
            if self.range_end < self.range_start:
                raise ValueError("range_end must not precede range_start")
        if self.goal_type == GoalType.GROUNDED_QUESTION:
            if not self.question or not self.question.strip():
                raise ValueError("grounded_question requires question")
            if not self.source_artifact_id or self.source_date is None:
                raise ValueError(
                    "grounded_question requires an explicit source_artifact_id and source_date"
                )
        return self


class PlanStep(RadarAgentSchema):
    step_id: str = Field(..., min_length=1)
    ordinal: int = Field(..., ge=1)
    kind: PlanStepKind
    capability: str = Field(..., min_length=1)
    purpose: str = Field(default="", max_length=500)
    agent: AgentKind | None = None
    tool_name: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    input_bindings: dict[str, Any] = Field(default_factory=dict)
    required_evidence: list[str] = Field(default_factory=list)
    allowed_outputs: list[str] = Field(default_factory=list)
    required: bool = True
    run_if: Literal["always", "risk_review_required"] = "always"
    rationale_summary: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def bind_one_executor(self) -> "PlanStep":
        if self.kind == PlanStepKind.AGENT and self.agent is None:
            raise ValueError("agent step requires agent")
        if self.kind == PlanStepKind.TOOL and not self.tool_name:
            raise ValueError("tool step requires tool_name")
        if self.kind != PlanStepKind.AGENT and self.agent is not None:
            raise ValueError("only agent steps may name an agent")
        if self.kind != PlanStepKind.TOOL and self.tool_name is not None:
            raise ValueError("only tool steps may name a tool")
        if not self.purpose:
            self.purpose = self.rationale_summary or self.capability
        return self


class ExecutionPlan(RadarAgentSchema):
    plan_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    goal_id: str = Field(..., min_length=1)
    revision: int = Field(default=1, ge=1, le=3)
    objective: str = Field(default="", max_length=1000)
    assumptions: list[str] = Field(default_factory=list, max_length=12)
    steps: list[PlanStep] = Field(..., min_length=1)
    completion_criteria: list[str] = Field(default_factory=list, max_length=12)
    created_by_invocation_id: str | None = None
    supersedes_plan_id: str | None = None
    change_summary: str | None = Field(default=None, max_length=1000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_graph(self) -> "ExecutionPlan":
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("plan step ids must be unique")
        if [step.ordinal for step in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("plan ordinals must be contiguous and ordered")
        known: set[str] = set()
        for step in self.steps:
            unknown = set(step.depends_on) - known
            if unknown:
                raise ValueError(f"step {step.step_id} has forward or unknown dependencies")
            known.add(step.step_id)
        return self


class EvaluationOutcome(RadarAgentSchema):
    evaluation_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    plan_id: str = Field(..., min_length=1)
    decision: EvaluationDecision
    decision_summary: str = Field(..., min_length=1, max_length=1000)
    reason_codes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    unresolved_gaps: list[str] = Field(default_factory=list)
    next_authorized_action: str | None = Field(default=None, max_length=500)
    missing_capabilities: list[str] = Field(default_factory=list)
    safety_flags: list[str] = Field(default_factory=list)
    user_input_request_id: str | None = None
    created_by_invocation_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RuntimeBudget(RadarAgentSchema):
    task_id: str = Field(..., min_length=1)
    initial_plan_limit: int = Field(default=1, ge=1, le=1)
    replan_limit: int = Field(default=2, ge=0, le=2)
    capability_agent_call_limit: int = Field(default=6, ge=1, le=6)
    deterministic_tool_call_limit: int = Field(default=12, ge=1, le=12)
    total_model_call_limit: int = Field(default=16, ge=1, le=16)
    wall_clock_limit_seconds: int = Field(default=60, ge=1, le=90)
    initial_plan_count: int = Field(default=0, ge=0)
    replan_count: int = Field(default=0, ge=0)
    capability_agent_call_count: int = Field(default=0, ge=0)
    deterministic_tool_call_count: int = Field(default=0, ge=0)
    total_model_call_count: int = Field(default=0, ge=0)
    schema_repair_count: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    active_elapsed_seconds: float = Field(default=0, ge=0)
    active_segment_started_at: datetime | None = None
    clock_paused: bool = False
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def for_goal(cls, task_id: str, goal_type: GoalType) -> "RuntimeBudget":
        now = datetime.now(timezone.utc)
        return cls(
            task_id=task_id,
            wall_clock_limit_seconds=90 if goal_type == GoalType.DOCTOR_MATERIAL else 60,
            started_at=now,
            active_segment_started_at=now,
        )

    def consume(self, counter: Literal["initial_plan", "replan", "capability_agent_call", "deterministic_tool_call", "model_call", "schema_repair", "retry"]) -> "RuntimeBudget":
        limits = {
            "initial_plan": ("initial_plan_count", self.initial_plan_limit),
            "replan": ("replan_count", self.replan_limit),
            "capability_agent_call": ("capability_agent_call_count", self.capability_agent_call_limit),
            "deterministic_tool_call": ("deterministic_tool_call_count", self.deterministic_tool_call_limit),
            "model_call": ("total_model_call_count", self.total_model_call_limit),
        }
        updates: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
        if counter in limits:
            field_name, limit = limits[counter]
            new_value = getattr(self, field_name) + 1
            if new_value > limit:
                raise RuntimeBudgetExceeded(counter)
            updates[field_name] = new_value
        elif counter == "schema_repair":
            updates["schema_repair_count"] = self.schema_repair_count + 1
            updates["total_model_call_count"] = self.total_model_call_count + 1
            if updates["total_model_call_count"] > self.total_model_call_limit:
                raise RuntimeBudgetExceeded("model_call")
        else:
            updates["retry_count"] = self.retry_count + 1
            updates["total_model_call_count"] = self.total_model_call_count + 1
            if updates["total_model_call_count"] > self.total_model_call_limit:
                raise RuntimeBudgetExceeded("model_call")
        return self.model_copy(update=updates)

    def assert_time_available(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        active = self.active_elapsed_seconds
        if not self.clock_paused:
            segment_start = self.active_segment_started_at or self.started_at
            active += max(0, (current - segment_start).total_seconds())
        if active >= self.wall_clock_limit_seconds:
            raise RuntimeBudgetExceeded("wall_clock")

    def pause_clock(self, *, now: datetime | None = None) -> "RuntimeBudget":
        if self.clock_paused:
            return self
        current = now or datetime.now(timezone.utc)
        segment_start = self.active_segment_started_at or self.started_at
        return self.model_copy(
            update={
                "active_elapsed_seconds": self.active_elapsed_seconds
                + max(0, (current - segment_start).total_seconds()),
                "active_segment_started_at": None,
                "clock_paused": True,
                "updated_at": current,
            }
        )

    def resume_clock(self, *, now: datetime | None = None) -> "RuntimeBudget":
        if not self.clock_paused:
            return self
        current = now or datetime.now(timezone.utc)
        return self.model_copy(
            update={
                "active_segment_started_at": current,
                "clock_paused": False,
                "updated_at": current,
            }
        )


class RuntimeBudgetExceeded(RuntimeError):
    def __init__(self, budget_name: str) -> None:
        self.budget_name = budget_name
        super().__init__(f"dynamic runtime budget exhausted: {budget_name}")


class ModelInvocation(RadarAgentSchema):
    invocation_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    purpose: Literal["plan", "evaluate", "agent", "schema_repair", "retry"]
    agent: AgentKind
    model_provider: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    prompt_version: str = Field(..., min_length=1)
    context_packet_id: str = Field(..., min_length=1)
    client_idempotency_key: str = Field(..., min_length=1)
    attempt: int = Field(default=1, ge=1)
    outcome: InvocationOutcome = InvocationOutcome.PENDING
    provider_request_id: str | None = None
    input_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    output_schema: str | None = None
    error_code: str | None = None
    error_summary: str | None = Field(default=None, max_length=1000)
    latency_ms: int | None = Field(default=None, ge=0)
    input_token_count: int | None = Field(default=None, ge=0)
    output_token_count: int | None = Field(default=None, ge=0)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


class AgentInvocation(RadarAgentSchema):
    invocation_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    plan_id: str = Field(..., min_length=1)
    step_id: str = Field(..., min_length=1)
    agent: AgentKind
    agent_schema_version: str = "AgentWorkProduct.v1"
    context_packet_id: str | None = None
    call_reason: str | None = Field(default=None, max_length=500)
    allowed_tool_scope: list[str] = Field(default_factory=list)
    status: AgentInvocationStatus = AgentInvocationStatus.PENDING
    model_invocation_id: str | None = None
    parent_invocation_id: str | None = None
    model_provider: str | None = None
    model_id: str | None = None
    provider_request_id: str | None = None
    caused_by_a2a_message_id: str | None = None
    accepted_a2a_message_id: str | None = None
    output_artifact_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    validation_result: Literal["pending", "valid", "invalid"] = "pending"
    safe_summary: str | None = Field(default=None, max_length=1000)
    latency_ms: int | None = Field(default=None, ge=0)
    error_summary: str | None = Field(default=None, max_length=1000)
    fallback_state: str | None = Field(default=None, max_length=200)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


class DynamicToolDefinition(RadarAgentSchema):
    tool_name: str = Field(..., min_length=1)
    version: str = Field(default="1.0.0", min_length=1)
    purpose: str = Field(..., min_length=1, max_length=500)
    input_schema: str = Field(..., min_length=1)
    output_schema: str = Field(..., min_length=1)
    required_permission: str = Field(..., min_length=1)
    read_write_class: Literal["read", "write", "confirmation"]
    confirmation_policy: Literal["never", "before_execute", "approved_only"]
    timeout_seconds: float = Field(default=10, gt=0, le=120)
    idempotency_behavior: Literal[
        "read_retryable", "write_once", "provider_idempotent", "reconcile_before_retry"
    ]
    audit_policy: Literal["full_metadata", "safe_summary_only"] = "full_metadata"


class ToolInvocation(RadarAgentSchema):
    invocation_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    plan_id: str = Field(..., min_length=1)
    step_id: str = Field(..., min_length=1)
    tool_name: str = Field(..., min_length=1)
    tool_schema_version: str = "dynamic-tool.v1"
    input_schema: str = "DynamicToolInput.v1"
    output_schema: str = "DynamicToolOutput.v1"
    read_write_class: Literal["read", "write", "confirmation"] = "read"
    required_permission: str = "process_health_data"
    confirmation_policy: Literal["never", "before_execute", "approved_only"] = "never"
    timeout_seconds: float = Field(default=10, gt=0, le=120)
    idempotency_behavior: Literal[
        "read_retryable", "write_once", "provider_idempotent", "reconcile_before_retry"
    ] = "read_retryable"
    audit_policy: Literal["full_metadata", "safe_summary_only"] = "full_metadata"
    parent_invocation_id: str | None = None
    context_packet_id: str | None = None
    client_idempotency_key: str = Field(..., min_length=1)
    attempt: int = Field(default=1, ge=1)
    outcome: InvocationOutcome = InvocationOutcome.PENDING
    output_summary: dict[str, Any] = Field(default_factory=dict)
    safe_summary: str | None = Field(default=None, max_length=1000)
    evidence_refs: list[str] = Field(default_factory=list)
    validation_result: Literal["pending", "valid", "invalid"] = "pending"
    latency_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    fallback_state: str | None = Field(default=None, max_length=200)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


class DynamicStepRecord(RadarAgentSchema):
    task_id: str = Field(..., min_length=1)
    plan_id: str = Field(..., min_length=1)
    step: PlanStep
    status: Literal[
        "pending", "running", "waiting_for_user_input", "succeeded", "failed", "skipped"
    ] = "pending"
    output: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RuntimeCheckpoint(RadarAgentSchema):
    checkpoint_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    sequence: int = Field(..., ge=1)
    state: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class JobLease(RadarAgentSchema):
    task_id: str = Field(..., min_length=1)
    worker_id: str = Field(..., min_length=1)
    lease_token: str = Field(..., min_length=1)
    lease_expires_at: datetime
    heartbeat_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    attempt: int = Field(default=1, ge=1)


class UserInputRequest(RadarAgentSchema):
    request_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    question_id: str = Field(..., min_length=1)
    question_version: str = Field(..., min_length=1)
    question_text: str = Field(..., min_length=1, max_length=500)
    question_type: Literal["observable_fact", "simple_context", "symptom_self_report"]
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
    response_id: str = Field(..., min_length=1)
    request_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1, max_length=1000)
    answered_by_user_id: str = Field(..., min_length=1)
    answered_by_role: Literal["elder", "family", "doctor", "system"]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FactSnapshot(RadarAgentSchema):
    snapshot_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    source_refs: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def hash_must_match_facts(self) -> "FactSnapshot":
        canonical = json.dumps(
            self.facts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if self.sha256 != expected:
            raise ValueError("FactSnapshot hash does not match its canonical facts")
        return self

    @classmethod
    def create(cls, *, snapshot_id: str, task_id: str, source_refs: list[str], facts: dict[str, Any]) -> "FactSnapshot":
        canonical = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(
            snapshot_id=snapshot_id,
            task_id=task_id,
            source_refs=source_refs,
            facts=facts,
            sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )


class CompletionReceipt(RadarAgentSchema):
    receipt_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    goal_id: str = Field(..., min_length=1)
    execution_mode: ExecutionMode
    completion_status: CompletionStatus
    achieved_goal: bool = False
    accepted_claim_refs: list[str] = Field(default_factory=list)
    requested_artifact_refs: list[str] = Field(default_factory=list)
    missing_outputs: list[str] = Field(default_factory=list)
    unresolved_gaps: list[str] = Field(default_factory=list)
    actions_proposed: list[str] = Field(default_factory=list)
    actions_executed: list[str] = Field(default_factory=list)
    trace_refs: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    safe_next_step: str | None = Field(default=None, max_length=1000)
    fact_snapshot_id: str | None = None
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def stable_input_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "AgentInvocation",
    "AgentInvocationStatus",
    "AgentKind",
    "CompletionReceipt",
    "CompletionStatus",
    "DYNAMIC_RUNTIME_CONTRACT_VERSION",
    "DynamicStepRecord",
    "DynamicToolDefinition",
    "EvaluationDecision",
    "EvaluationOutcome",
    "ExecutionMode",
    "ExecutionPlan",
    "FactSnapshot",
    "GoalType",
    "InvocationOutcome",
    "JobLease",
    "ModelInvocation",
    "PlanStep",
    "PlanStepKind",
    "RuntimeBudget",
    "RuntimeBudgetExceeded",
    "RuntimeCheckpoint",
    "ToolInvocation",
    "UserGoal",
    "UserInputRequest",
    "UserInputResponse",
    "stable_input_sha256",
]
