from __future__ import annotations

from pydantic import Field

from sleepagent.radar_agent.schemas import RadarAgentName, RadarAgentSchema


class SkillSpec(RadarAgentSchema):
    """Versioned, testable capability contract; never a monolithic prompt."""

    skill_id: str = Field(..., min_length=1)
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    applicable_agents: list[RadarAgentName] = Field(min_length=1)
    scope: str = Field(..., min_length=1)
    output_schema: dict = Field(default_factory=dict)
    rules: list[str] = Field(min_length=1)
    forbidden_actions: list[str] = Field(default_factory=list)


__all__ = ["SkillSpec"]
