from __future__ import annotations

from typing import Any
from uuid import uuid4

from .contracts import LLMToolRequest, ToolExecutionContext, ToolExecutionResult, ToolMetadata
from .orchestrator import OrchestratorToolExecutor


class WhitelistedMCPAdapter:
    """Small MCP-shaped adapter that never owns authorization or handlers."""

    def __init__(self, executor: OrchestratorToolExecutor) -> None:
        self.executor = executor

    def list_tools(self, context: ToolExecutionContext) -> list[ToolMetadata]:
        return [
            definition.metadata()
            for definition in self.executor.registry.definitions()
            if definition.name in context.allowed_tools
            and context.actor_role in definition.allowed_roles
            and definition.required_permissions <= context.permissions
        ]

    def call_tool(
        self,
        *,
        task_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
        request_id: str | None = None,
    ) -> ToolExecutionResult:
        request = LLMToolRequest(
            request_id=request_id or f"tool-request:{uuid4().hex}",
            task_id=task_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        return self.executor.execute(request, context)


__all__ = ["WhitelistedMCPAdapter"]
