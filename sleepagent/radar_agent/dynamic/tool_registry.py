from __future__ import annotations

from sleepagent.radar_agent.dynamic.contracts import DynamicToolDefinition


def _tool(
    name: str,
    purpose: str,
    *,
    read_write: str = "read",
    confirmation: str = "never",
    idempotency: str = "read_retryable",
    permission: str = "process_health_data",
    timeout: float = 10,
) -> DynamicToolDefinition:
    stem = "".join(part.title() for part in name.replace("_", ".").split("."))
    return DynamicToolDefinition(
        tool_name=name,
        purpose=purpose,
        input_schema=f"{stem}Input.v1",
        output_schema=f"{stem}Output.v1",
        required_permission=permission,
        read_write_class=read_write,
        confirmation_policy=confirmation,
        timeout_seconds=timeout,
        idempotency_behavior=idempotency,
    )


DYNAMIC_TOOL_DEFINITIONS = {
    item.tool_name: item
    for item in (
        _tool("radar.read", "Read canonical snapshots and the exact dated night summary."),
        _tool("quality.assess", "Apply the deterministic device and data-quality gate."),
        _tool("history.query", "Read only the explicit artifact or authorized date range."),
        _tool("trend.calculate", "Calculate canonical 7/30/90-day trend windows."),
        _tool("urgent_boundary.evaluate", "Apply deterministic risk and urgent-text boundaries."),
        _tool("rag.retrieve_reviewed", "Retrieve reviewed, role-scoped seed knowledge only."),
        _tool("questionnaire.select", "Select one reviewed versioned observable-fact question."),
        _tool(
            "questionnaire.capture",
            "Capture a reviewed self-report answer without overwriting device evidence.",
            read_write="write",
            idempotency="write_once",
            permission="process_questionnaire",
        ),
        _tool("evidence.ledger_validate", "Validate claims, citations, caveats, and review status."),
        _tool(
            "evidence.ledger_commit",
            "Commit one accepted Ledger and immutable FactSnapshot.",
            read_write="write",
            idempotency="write_once",
        ),
        _tool(
            "role_artifact.render",
            "Render only the requested role artifact from the accepted FactSnapshot.",
            read_write="write",
            idempotency="write_once",
        ),
        _tool(
            "doctor_material.render",
            "Render the structured doctor material from the accepted FactSnapshot.",
            read_write="write",
            idempotency="write_once",
        ),
        _tool(
            "confirmation.request",
            "Create the human confirmation required before doctor-material export.",
            read_write="confirmation",
            confirmation="before_execute",
            idempotency="write_once",
        ),
        _tool(
            "confirmed_action.execute",
            "Execute an approved doctor-material export exactly once and reconcile retries.",
            read_write="write",
            confirmation="approved_only",
            idempotency="reconcile_before_retry",
            timeout=30,
        ),
    )
}

ALLOWED_DYNAMIC_TOOLS = frozenset(DYNAMIC_TOOL_DEFINITIONS)


def dynamic_tool_manifest() -> list[dict[str, object]]:
    return [
        definition.model_dump(mode="json")
        for definition in sorted(
            DYNAMIC_TOOL_DEFINITIONS.values(), key=lambda item: item.tool_name
        )
    ]


__all__ = [
    "ALLOWED_DYNAMIC_TOOLS",
    "DYNAMIC_TOOL_DEFINITIONS",
    "dynamic_tool_manifest",
]
