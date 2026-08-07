from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import Field, model_validator

from sleepagent.radar_agent.persistence.models import RadarMemorySummary
from sleepagent.radar_agent.schemas import (
    HumanConfirmationRequest,
    MemoryCandidate,
    RadarAgentSchema,
    RadarNightSummary,
    RoleReportArtifact,
)


class ShortTermDialogueTurn(RadarAgentSchema):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1)
    sensitive: bool = False
    consented_for_long_term: bool = False


class ShortTermMemoryContext(RadarAgentSchema):
    """Task-local memory. This model is never accepted by long-term persistence."""

    task_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    latest_night_summary: RadarNightSummary | None = None
    dialogue_turns: list[ShortTermDialogueTurn] = Field(default_factory=list)
    current_report_artifact: RoleReportArtifact | None = None
    expires_with_task: bool = True

    @model_validator(mode="after")
    def remains_short_lived(self) -> "ShortTermMemoryContext":
        if not self.expires_with_task:
            raise ValueError("short-term memory must expire with the current task")
        return self


TrendMemoryMetricName = Literal[
    "sleep_minutes",
    "in_bed_minutes",
    "out_of_bed_count",
    "movement_count",
    "breath_rate_bpm",
    "heart_rate_bpm",
    "data_coverage_ratio",
]


class TrendWindowMemory(RadarAgentSchema):
    window_days: Literal[7, 30, 90]
    status: Literal["computed", "insufficient_data", "not_interpretable"]
    metrics: dict[TrendMemoryMetricName, float | int | None] = Field(
        default_factory=dict
    )
    evidence_refs: list[str] = Field(default_factory=list)


class LongTermTrendMemory(RadarAgentSchema):
    windows: dict[Literal["7", "30", "90"], TrendWindowMemory]
    risk_level: str | None = None
    risk_signal_change: str = "insufficient_history"

    @model_validator(mode="after")
    def contains_all_windows(self) -> "LongTermTrendMemory":
        if set(self.windows) != {"7", "30", "90"}:
            raise ValueError("long-term trend memory requires 7/30/90-day windows")
        return self


class UserPreferenceMemory(RadarAgentSchema):
    expression_style: str | None = None
    family_focus: list[str] = Field(default_factory=list)
    doctor_report_format: str | None = None


class CareEventMemory(RadarAgentSchema):
    event_type: Literal[
        "watch_reminder",
        "care_plan",
        "family_viewed",
        "doctor_followup",
    ]
    status: Literal[
        "candidate",
        "confirmed",
        "enabled",
        "viewed",
        "recommended",
        "completed",
    ]
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reference_id: str | None = None


class MemoryPrivacyDecision(RadarAgentSchema):
    allowed: bool
    reasons: list[str] = Field(default_factory=list)
    sanitized_payload: dict[str, Any] = Field(default_factory=dict)


class MemoryWriteDecision(RadarAgentSchema):
    candidate_id: str
    task_id: str
    approved_for_write: bool = False
    reasons: list[str] = Field(default_factory=list)
    confirmation_id: str | None = None
    candidate: MemoryCandidate | None = None
    memory: RadarMemorySummary | None = None
    decided_by: Literal["orchestrator"] = "orchestrator"


class MemoryCandidateStore(Protocol):
    def propose(self, candidate: MemoryCandidate) -> MemoryCandidate:
        ...

    def list_for_subject(self, subject_id: str) -> list[MemoryCandidate]:
        ...


class MemoryWritePolicy(Protocol):
    def decide(
        self,
        candidate: MemoryCandidate,
        confirmation: HumanConfirmationRequest | None,
        *,
        actor: str,
    ) -> MemoryWriteDecision:
        ...


__all__ = [
    "CareEventMemory",
    "LongTermTrendMemory",
    "MemoryCandidate",
    "MemoryCandidateStore",
    "MemoryPrivacyDecision",
    "MemoryWriteDecision",
    "MemoryWritePolicy",
    "ShortTermDialogueTurn",
    "ShortTermMemoryContext",
    "TrendWindowMemory",
    "TrendMemoryMetricName",
    "UserPreferenceMemory",
]
