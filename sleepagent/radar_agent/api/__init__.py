from sleepagent.radar_agent.api.routes import (
    RADAR_AGENT_API_PREFIX,
    RADAR_AGENT_ROUTES,
    DYNAMIC_RADAR_AGENT_ROUTES,
    RadarAgentRoute,
    full_route_paths,
    all_route_paths,
)


def __getattr__(name: str):
    if name in {
        "RADAR_AGENT_API_KEY_ENV",
        "RADAR_AGENT_DATABASE_URL_ENV",
        "RADAR_AGENT_LLM_RETRY_ENV",
        "RADAR_AGENT_LLM_TIMEOUT_SECONDS_ENV",
        "RADAR_AGENT_SQLITE_PATH_ENV",
        "RadarApiRuntime",
        "RadarChatRequest",
        "RadarChatResponse",
        "RadarTaskCreateRequest",
        "RadarTaskDetail",
        "reset_radar_api_runtime_for_tests",
        "router",
    }:
        from sleepagent.radar_agent.api import http

        return getattr(http, name)
    raise AttributeError(name)

__all__ = [
    "RADAR_AGENT_API_PREFIX",
    "RADAR_AGENT_ROUTES",
    "DYNAMIC_RADAR_AGENT_ROUTES",
    "RadarAgentRoute",
    "full_route_paths",
    "all_route_paths",
    "RADAR_AGENT_API_KEY_ENV",
    "RADAR_AGENT_DATABASE_URL_ENV",
    "RADAR_AGENT_LLM_RETRY_ENV",
    "RADAR_AGENT_LLM_TIMEOUT_SECONDS_ENV",
    "RADAR_AGENT_SQLITE_PATH_ENV",
    "RadarApiRuntime",
    "RadarChatRequest",
    "RadarChatResponse",
    "RadarTaskCreateRequest",
    "RadarTaskDetail",
    "reset_radar_api_runtime_for_tests",
    "router",
]
