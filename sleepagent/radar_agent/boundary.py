from __future__ import annotations

from dataclasses import dataclass
from typing import Final


RADAR_AGENT_BOUNDARY_VERSION: Final = "radar_agent.boundary.v2"
RADAR_AGENT_API_PREFIX: Final = "/radar-agent"
RADAR_AGENT_FRONTEND_ENTRYPOINT: Final = "frontend/app/page.tsx"
PRODUCTION_AGENT_NAMESPACE: Final = "sleepagent.radar_agent.product_agent"

CANONICAL_SUBPACKAGES: Final[tuple[str, ...]] = (
    "provider",
    "schemas",
    "runtime",
    "product_agent",
    "evidence",
    "reports",
    "rag",
    "memory",
    "llm",
    "questionnaire",
    "persistence",
    "api",
)

@dataclass(frozen=True)
class RadarAgentBoundary:
    """Frozen product boundary for the four-role Agent mainline."""

    namespace: str = "sleepagent.radar_agent"
    production_agent_namespace: str = PRODUCTION_AGENT_NAMESPACE
    device_layer_namespace: str = "sleepagent.product_device"
    api_prefix: str = RADAR_AGENT_API_PREFIX
    frontend_entrypoint: str = RADAR_AGENT_FRONTEND_ENTRYPOINT
    subpackages: tuple[str, ...] = CANONICAL_SUBPACKAGES
    product_positioning: str = "radar_first_care_coordination"


DEFAULT_RADAR_AGENT_BOUNDARY: Final = RadarAgentBoundary()


__all__ = [
    "CANONICAL_SUBPACKAGES",
    "DEFAULT_RADAR_AGENT_BOUNDARY",
    "PRODUCTION_AGENT_NAMESPACE",
    "RADAR_AGENT_API_PREFIX",
    "RADAR_AGENT_BOUNDARY_VERSION",
    "RADAR_AGENT_FRONTEND_ENTRYPOINT",
    "RadarAgentBoundary",
]
