from __future__ import annotations

from typing import Protocol

from sleepagent.radar_agent.schemas import (
    AgentResult,
    ContextPacket,
    EvidencePacket,
    RadarAgentName,
    RagContext,
    SafetyPolicy,
    TaskContext,
)


class RadarSubAgent(Protocol):
    name: RadarAgentName

    def run(self, context: ContextPacket) -> AgentResult:
        ...


__all__ = [
    "AgentResult",
    "ContextPacket",
    "EvidencePacket",
    "RadarAgentName",
    "RadarSubAgent",
    "RagContext",
    "SafetyPolicy",
    "TaskContext",
]
