from __future__ import annotations

from dataclasses import dataclass
from typing import Final


RADAR_AGENT_BOUNDARY_VERSION: Final = "radar_agent.boundary.v1"
RADAR_AGENT_API_PREFIX: Final = "/radar-agent"
RADAR_AGENT_FRONTEND_ENTRYPOINT: Final = "frontend/app/page.tsx"

CANONICAL_SUBPACKAGES: Final[tuple[str, ...]] = (
    "provider",
    "schemas",
    "runtime",
    "orchestrator",
    "agents",
    "evidence",
    "a2a",
    "reports",
    "rag",
    "memory",
    "llm",
    "prompts",
    "skills",
    "questionnaire",
    "persistence",
    "api",
    "cli",
)

@dataclass(frozen=True)
class RadarAgentBoundary:
    """Frozen v1 product and code boundary for the radar-first mainline."""

    namespace: str = "sleepagent.radar_agent"
    device_layer_namespace: str = "sleepagent.product_device"
    api_prefix: str = RADAR_AGENT_API_PREFIX
    frontend_entrypoint: str = RADAR_AGENT_FRONTEND_ENTRYPOINT
    subpackages: tuple[str, ...] = CANONICAL_SUBPACKAGES
    product_positioning: str = "radar_first_care_coordination"


DEFAULT_RADAR_AGENT_BOUNDARY: Final = RadarAgentBoundary()


__all__ = [
    "CANONICAL_SUBPACKAGES",
    "DEFAULT_RADAR_AGENT_BOUNDARY",
    "RADAR_AGENT_API_PREFIX",
    "RADAR_AGENT_BOUNDARY_VERSION",
    "RADAR_AGENT_FRONTEND_ENTRYPOINT",
    "RadarAgentBoundary",
]
