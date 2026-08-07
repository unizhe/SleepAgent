from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from sleepagent.radar_agent.schemas import (
    EvidenceLedger,
    RadarDevice,
    RadarNightSummary,
    RadarVitalSnapshot,
    RiskLevel,
    RoleReportArtifact,
)


class ToolSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RadarReadInput(ToolSchema):
    radar_device_id: str = Field(..., min_length=1)
    night_of: date | None = None


class RadarReadOutput(ToolSchema):
    device: RadarDevice
    snapshots: list[RadarVitalSnapshot] = Field(default_factory=list)
    provider_night_summary: RadarNightSummary | None = None


class QualityAssessmentInput(ToolSchema):
    device: RadarDevice
    snapshots: list[RadarVitalSnapshot] = Field(default_factory=list)
    provider_night_summary: RadarNightSummary | None = None
    night_of: date | None = None


class NightSummaryInput(QualityAssessmentInput):
    pass


class NightSummaryOutput(ToolSchema):
    night_summary: RadarNightSummary


class TrendCalculationInput(ToolSchema):
    task_id: str = Field(..., min_length=1)
    summaries: list[RadarNightSummary] = Field(default_factory=list)


class TrendCalculationOutput(ToolSchema):
    trend_result: dict[str, Any]


class AlertRuleInput(ToolSchema):
    task_id: str = Field(..., min_length=1)
    risk_level: RiskLevel
    evidence_refs: list[str] = Field(default_factory=list)


class AlertRuleOutput(ToolSchema):
    risk_level: RiskLevel
    candidate_actions: list[str] = Field(default_factory=list)
    confirmation_actions: list[str] = Field(default_factory=list)
    external_action_executed: bool = False


class ReportGenerationInput(ToolSchema):
    task_id: str = Field(..., min_length=1)
    ledger: EvidenceLedger


class ReportGenerationOutput(ToolSchema):
    reports: list[RoleReportArtifact] = Field(default_factory=list)


class OptionalContextInput(ToolSchema):
    mode: Literal["mock", "manual"]
    values: dict[str, Any] = Field(default_factory=dict)


class OptionalContextOutput(ToolSchema):
    source: Literal["weather", "room_temperature", "calendar", "medication_diet"]
    mode: Literal["mock", "manual"]
    values: dict[str, Any]
    live_connector_used: bool = False


class ExternalActionInput(ToolSchema):
    subject_id: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)


class ExternalActionOutput(ToolSchema):
    action_ref: str = Field(..., min_length=1)
    status: Literal["recorded", "sent", "exported", "written"]


__all__ = [name for name in globals() if name.endswith(("Input", "Output"))]
