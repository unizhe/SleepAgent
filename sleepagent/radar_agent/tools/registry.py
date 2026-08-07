from __future__ import annotations

from collections.abc import Mapping

from .contracts import (
    ConfirmationPolicy,
    ToolAuditRule,
    ToolDefinition,
    ToolHandler,
    ToolPermission,
    ToolStateEffect,
)
from .schemas import (
    AlertRuleInput,
    AlertRuleOutput,
    ExternalActionInput,
    ExternalActionOutput,
    NightSummaryInput,
    NightSummaryOutput,
    OptionalContextInput,
    OptionalContextOutput,
    QualityAssessmentInput,
    RadarReadInput,
    RadarReadOutput,
    ReportGenerationInput,
    ReportGenerationOutput,
    TrendCalculationInput,
    TrendCalculationOutput,
)


ALL_ROLES = frozenset({"elder", "family", "doctor", "system"})
CARE_ROLES = frozenset({"family", "doctor", "system"})


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, definition: ToolDefinition, handler: ToolHandler | None = None) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"duplicate tool definition: {definition.name}")
        self._definitions[definition.name] = definition
        if handler is not None:
            self._handlers[definition.name] = handler

    def definition(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def handler(self, name: str) -> ToolHandler | None:
        return self._handlers.get(name)

    def definitions(self) -> list[ToolDefinition]:
        return [self._definitions[name] for name in sorted(self._definitions)]


def build_default_registry(handlers: Mapping[str, ToolHandler] | None = None) -> ToolRegistry:
    handlers = handlers or {}
    registry = ToolRegistry()
    definitions = [
        _tool(
            "radar.read",
            "Read canonical radar data through a provider adapter.",
            RadarReadInput,
            RadarReadOutput,
            ToolPermission.RADAR_READ,
            ALL_ROLES,
            ToolStateEffect.READ,
        ),
        _tool(
            "quality.assess",
            "Run the radar data-quality gate.",
            QualityAssessmentInput,
            NightSummaryOutput,
            ToolPermission.ANALYSIS_RUN,
            ALL_ROLES,
        ),
        _tool(
            "night_summary.generate",
            "Generate a canonical quality-gated night summary.",
            NightSummaryInput,
            NightSummaryOutput,
            ToolPermission.ANALYSIS_RUN,
            ALL_ROLES,
        ),
        _tool(
            "trend.calculate",
            "Calculate 7/30/90-day canonical trends.",
            TrendCalculationInput,
            TrendCalculationOutput,
            ToolPermission.ANALYSIS_RUN,
            ALL_ROLES,
        ),
        _tool(
            "alert_rules.evaluate",
            "Produce alert candidates without sending external alerts.",
            AlertRuleInput,
            AlertRuleOutput,
            ToolPermission.ANALYSIS_RUN,
            ALL_ROLES,
        ),
        _tool(
            "report.generate",
            "Generate role reports from an Evidence Ledger.",
            ReportGenerationInput,
            ReportGenerationOutput,
            ToolPermission.REPORT_GENERATE,
            ALL_ROLES,
        ),
    ]
    for source in ("weather", "room_temperature", "calendar", "medication_diet"):
        definitions.append(
            _tool(
                f"context.{source}.read",
                f"Read optional {source} context from mock or manual input only.",
                OptionalContextInput,
                OptionalContextOutput,
                ToolPermission.EXTERNAL_CONTEXT_READ,
                ALL_ROLES,
                ToolStateEffect.NONE,
                optional=True,
            )
        )
    for name, description, permission in (
        ("family.notify", "Send a family notification.", ToolPermission.EXTERNAL_ACTION),
        ("doctor_material.export", "Export doctor-facing material.", ToolPermission.EXTERNAL_ACTION),
        ("alert.send", "Send an external alert.", ToolPermission.EXTERNAL_ACTION),
        ("memory.write", "Write an approved long-term memory summary.", ToolPermission.MEMORY_WRITE),
    ):
        definitions.append(
            ToolDefinition(
                name=name,
                description=description,
                input_model=ExternalActionInput,
                output_model=ExternalActionOutput,
                required_permissions=frozenset({permission}),
                allowed_roles=CARE_ROLES,
                confirmation=ConfirmationPolicy.REQUIRED,
                confirmation_action=name,
                state_effect=ToolStateEffect.WRITE,
                audit=ToolAuditRule(
                    include_result=True,
                    redact_input_fields=["content"],
                ),
            )
        )
    for definition in definitions:
        registry.register(definition, handlers.get(definition.name))
    return registry


def _tool(
    name: str,
    description: str,
    input_model,
    output_model,
    permission: ToolPermission,
    roles: frozenset[str],
    state_effect: ToolStateEffect = ToolStateEffect.NONE,
    *,
    optional: bool = False,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_model=input_model,
        output_model=output_model,
        required_permissions=frozenset({permission}),
        allowed_roles=roles,
        state_effect=state_effect,
        audit=ToolAuditRule(redact_input_fields=["values"] if optional else []),
        optional=optional,
    )


__all__ = ["ToolRegistry", "build_default_registry"]
