from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from .contracts import (
    ConfirmationPolicy,
    LLMToolRequest,
    ToolAuditEvent,
    ToolAuthorizationError,
    ToolExecutionContext,
    ToolExecutionError,
    ToolExecutionResult,
)
from .registry import ToolRegistry


AuditSink = Callable[[ToolAuditEvent], None]
ConfirmationVerifier = Callable[[Any], bool]


def task_service_audit_sink(task_service) -> AuditSink:
    """Persist tool decisions through the existing WorkflowRuntime audit ledger."""

    def sink(event: ToolAuditEvent) -> None:
        task_service.record_audit(
            event.task_id,
            actor=event.actor_id,
            action=f"tool_{event.decision}",
            target_ref=event.tool_name,
            summary=event.reason_code or f"Tool {event.decision}.",
            payload=event.model_dump(mode="json"),
        )

    return sink


def task_service_confirmation_verifier(task_service) -> ConfirmationVerifier:
    """Verify a confirmation against the persisted runtime record, failing closed."""

    def verify(confirmation) -> bool:
        try:
            persisted = task_service.store.get_confirmation(confirmation.confirmation_id)
        except (KeyError, ValueError):
            return False
        return bool(
            persisted.task_id == confirmation.task_id
            and persisted.action_type == confirmation.action_type
            and persisted.status == "approved"
            and persisted.resolved_by == confirmation.confirmed_by
        )

    return verify


class OrchestratorToolExecutor:
    """The only component allowed to turn an LLM request into a tool execution."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        audit_sink: AuditSink | None = None,
        confirmation_verifier: ConfirmationVerifier | None = None,
    ) -> None:
        self.registry = registry
        self.audit_sink = audit_sink or (lambda event: None)
        self.confirmation_verifier = confirmation_verifier or (lambda confirmation: False)

    def execute(
        self,
        request: LLMToolRequest,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        definition = self.registry.definition(request.tool_name)
        self._audit(request, context, "requested", definition=definition)
        try:
            if definition is None:
                self._deny("tool_not_whitelisted", "requested tool is not in the whitelist")
            if request.task_id != context.task_id:
                self._deny("task_scope_mismatch", "tool request is outside the current task")
            if request.tool_name not in context.allowed_tools:
                self._deny("tool_not_allowed_in_context", "orchestrator context does not allow this tool")
            if context.actor_role not in definition.allowed_roles:
                self._deny("role_not_allowed", "actor role is not allowed to use this tool")
            missing = definition.required_permissions - context.permissions
            if missing:
                self._deny(
                    "missing_permission",
                    "missing tool permission: " + ", ".join(sorted(item.value for item in missing)),
                )
            if definition.confirmation == ConfirmationPolicy.REQUIRED:
                confirmation = context.confirmation
                if confirmation is None:
                    self._deny("confirmation_required", "approved human confirmation is required")
                if confirmation.task_id != context.task_id:
                    self._deny("confirmation_task_mismatch", "confirmation belongs to another task")
                if confirmation.action_type != definition.confirmation_action:
                    self._deny("confirmation_action_mismatch", "confirmation is for another action")
                if not self.confirmation_verifier(confirmation):
                    self._deny(
                        "confirmation_not_verified",
                        "confirmation is not approved in the authoritative runtime store",
                    )
            try:
                validated_input = definition.input_model.model_validate(request.arguments)
            except ValidationError as exc:
                raise ToolAuthorizationError("invalid_input", str(exc)) from exc
            handler = self.registry.handler(request.tool_name)
            if handler is None:
                raise ToolExecutionError(f"no executor is configured for {request.tool_name}")
            raw_output = handler(validated_input, context)
            validated_output = definition.output_model.model_validate(raw_output)
            result = ToolExecutionResult(
                request_id=request.request_id,
                task_id=request.task_id,
                tool_name=request.tool_name,
                output=validated_output.model_dump(mode="json"),
                state_effect=definition.state_effect,
            )
            self._audit(
                request,
                context,
                "completed",
                definition=definition,
                output=result.output if definition.audit.include_result else {},
            )
            return result
        except ToolAuthorizationError as exc:
            self._audit(request, context, "denied", definition=definition, reason_code=exc.code)
            raise
        except Exception as exc:
            self._audit(
                request,
                context,
                "failed",
                definition=definition,
                reason_code=exc.__class__.__name__,
            )
            raise

    @staticmethod
    def _deny(code: str, message: str) -> None:
        raise ToolAuthorizationError(code, message)

    def _audit(
        self,
        request: LLMToolRequest,
        context: ToolExecutionContext,
        decision: str,
        *,
        definition=None,
        reason_code: str | None = None,
        output: dict[str, Any] | None = None,
    ) -> None:
        rule = definition.audit if definition is not None else None
        if rule is not None:
            if decision == "requested" and not rule.log_requested:
                return
            if decision == "denied" and not rule.log_denied:
                return
            if decision == "completed" and not rule.log_completed:
                return
        arguments = (
            dict(request.arguments)
            if definition is not None
            else {"argument_keys": sorted(request.arguments)}
        )
        for field in rule.redact_input_fields if rule is not None else []:
            if field in arguments:
                arguments[field] = "[REDACTED]"
        self.audit_sink(
            ToolAuditEvent(
                task_id=context.task_id,
                trace_id=context.trace_id,
                request_id=request.request_id,
                tool_name=request.tool_name,
                actor_id=context.actor_id,
                decision=decision,
                reason_code=reason_code,
                input_summary=arguments,
                output_summary=output or {},
                state_effect=definition.state_effect if definition is not None else "none",
            )
        )


__all__ = [
    "AuditSink",
    "ConfirmationVerifier",
    "OrchestratorToolExecutor",
    "task_service_audit_sink",
    "task_service_confirmation_verifier",
]
