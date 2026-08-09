from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sleepagent.radar_agent.boundary import RADAR_AGENT_API_PREFIX


@dataclass(frozen=True)
class RadarAgentRoute:
    method: str
    path: str
    purpose: str


RADAR_AGENT_ROUTES: Final[tuple[RadarAgentRoute, ...]] = (
    RadarAgentRoute(
        "POST",
        "/subjects/{subject_id}/authorizations",
        "grant scoped radar/questionnaire/document processing authorization",
    ),
    RadarAgentRoute(
        "POST",
        "/subjects/{subject_id}/authorizations/{authorization_id}/revoke",
        "revoke data processing authorization",
    ),
    RadarAgentRoute(
        "GET", "/subjects/{subject_id}/export", "export authorized minimized subject data"
    ),
    RadarAgentRoute(
        "GET",
        "/subjects/{subject_id}/reports",
        "read role-filtered reports under active authorization",
    ),
    RadarAgentRoute(
        "DELETE", "/subjects/{subject_id}/data", "delete authorized subject data"
    ),
    RadarAgentRoute("POST", "/tasks", "create canonical Product Episode task"),
    RadarAgentRoute("POST", "/tasks/{task_id}/run", "run ProductEpisodeRunner"),
    RadarAgentRoute("GET", "/tasks/{task_id}", "read task state and artifacts"),
    RadarAgentRoute("GET", "/tasks/{task_id}/events", "read historical events"),
    RadarAgentRoute("GET", "/tasks/{task_id}/stream", "stream task events over SSE"),
    RadarAgentRoute("POST", "/tasks/{task_id}/confirm", "resolve human confirmation"),
    RadarAgentRoute(
        "POST",
        "/tasks/{task_id}/confirmations/{confirmation_id}/revoke",
        "revoke a pending or unexecuted approved confirmation",
    ),
    RadarAgentRoute("POST", "/chat", "explain an existing task from ledger context"),
)

PRODUCT_AGENT_TASK_ROUTES: Final[tuple[RadarAgentRoute, ...]] = (
    RadarAgentRoute("GET", "/tasks", "list active or historical Product Episode tasks"),
    RadarAgentRoute(
        "POST", "/tasks/{task_id}/user-input", "answer a reviewed paused-task question"
    ),
    RadarAgentRoute(
        "GET", "/tasks/{task_id}/decision-trace", "read the user-safe decision journal"
    ),
    RadarAgentRoute(
        "GET", "/tasks/{task_id}/developer-trace", "read a development-gated invocation trace"
    ),
)


def full_route_paths() -> tuple[str, ...]:
    return tuple(f"{RADAR_AGENT_API_PREFIX}{route.path}" for route in RADAR_AGENT_ROUTES)


def all_route_paths() -> tuple[str, ...]:
    return tuple(
        f"{RADAR_AGENT_API_PREFIX}{route.path}"
        for route in (*RADAR_AGENT_ROUTES, *PRODUCT_AGENT_TASK_ROUTES)
    )


__all__ = [
    "RADAR_AGENT_API_PREFIX",
    "RADAR_AGENT_ROUTES",
    "PRODUCT_AGENT_TASK_ROUTES",
    "RadarAgentRoute",
    "full_route_paths",
    "all_route_paths",
]
