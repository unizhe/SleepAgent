from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolPermission(str, Enum):
    RADAR_READ = "radar:read"
    ANALYSIS_RUN = "analysis:run"
    REPORT_GENERATE = "report:generate"
    EXTERNAL_CONTEXT_READ = "external_context:read"
    EXTERNAL_ACTION = "external_action:execute"
    MEMORY_WRITE = "memory:write"


class ToolStateEffect(str, Enum):
    NONE = "none"
    READ = "read"
    WRITE = "write"


class ConfirmationPolicy(str, Enum):
    NEVER = "never"
    REQUIRED = "required"


class ToolAuditRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_requested: bool = True
    log_denied: bool = True
    log_completed: bool = True
    include_result: bool = False
    redact_input_fields: list[str] = Field(default_factory=list)


class ToolMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    required_permissions: list[ToolPermission] = Field(default_factory=list)
    allowed_roles: list[Literal["elder", "family", "doctor", "system"]]
    confirmation: ConfirmationPolicy = ConfirmationPolicy.NEVER
    confirmation_action: str | None = None
    state_effect: ToolStateEffect = ToolStateEffect.NONE
    audit: ToolAuditRule = Field(default_factory=ToolAuditRule)
    optional: bool = False


ToolHandler = Callable[[BaseModel, "ToolExecutionContext"], BaseModel | dict[str, Any]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    required_permissions: frozenset[ToolPermission]
    allowed_roles: frozenset[str]
    confirmation: ConfirmationPolicy = ConfirmationPolicy.NEVER
    confirmation_action: str | None = None
    state_effect: ToolStateEffect = ToolStateEffect.NONE
    audit: ToolAuditRule = field(default_factory=ToolAuditRule)
    optional: bool = False

    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
            output_schema=self.output_model.model_json_schema(),
            required_permissions=sorted(self.required_permissions, key=str),
            allowed_roles=sorted(self.allowed_roles),
            confirmation=self.confirmation,
            confirmation_action=self.confirmation_action,
            state_effect=self.state_effect,
            audit=self.audit,
            optional=self.optional,
        )


class LLMToolRequest(BaseModel):
    """Passive request emitted by an LLM; it has no execution capability."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    tool_name: str = Field(..., min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    requested_by: Literal["llm"] = "llm"


class ToolConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    action_type: str = Field(..., min_length=1)
    status: Literal["approved"] = "approved"
    confirmed_by: str = Field(..., min_length=1)


class ToolExecutionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    task_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    actor_id: str = Field(..., min_length=1)
    actor_role: Literal["elder", "family", "doctor", "system"]
    permissions: set[ToolPermission] = Field(default_factory=set)
    allowed_tools: set[str] = Field(default_factory=set)
    confirmation: ToolConfirmation | None = None


class ToolExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    task_id: str
    tool_name: str
    output: dict[str, Any]
    state_effect: ToolStateEffect


class ToolAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    trace_id: str
    request_id: str
    tool_name: str
    actor_id: str
    decision: Literal["requested", "denied", "completed", "failed"]
    reason_code: str | None = None
    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    state_effect: ToolStateEffect = ToolStateEffect.NONE
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ToolAuthorizationError(PermissionError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ToolExecutionError(RuntimeError):
    pass


__all__ = [
    "ConfirmationPolicy",
    "LLMToolRequest",
    "ToolAuditEvent",
    "ToolAuditRule",
    "ToolAuthorizationError",
    "ToolConfirmation",
    "ToolDefinition",
    "ToolExecutionContext",
    "ToolExecutionError",
    "ToolExecutionResult",
    "ToolHandler",
    "ToolMetadata",
    "ToolPermission",
    "ToolStateEffect",
]
