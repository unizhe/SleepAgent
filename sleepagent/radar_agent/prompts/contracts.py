from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import Field

from sleepagent.radar_agent.schemas import RadarAgentSchema


class PromptLayer(str, Enum):
    GLOBAL_SAFETY_POLICY = "global_safety_policy"
    AGENT_SKILL_PROMPT = "agent_skill_prompt"
    TASK_CONTEXT_PACKET = "task_context_packet"


class PromptSpec(RadarAgentSchema):
    prompt_id: str = Field(..., min_length=1)
    version: str = Field(..., pattern=r"^\d+\.\d+\.\d+$")
    layer: PromptLayer
    scope: str = Field(..., min_length=1)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    template: str = Field(..., min_length=1)


class PromptBundle(RadarAgentSchema):
    global_policy: PromptSpec
    agent_skill: PromptSpec
    task_context: PromptSpec
    messages: list[dict[str, str]] = Field(min_length=3, max_length=3)


__all__ = ["PromptBundle", "PromptLayer", "PromptSpec"]
