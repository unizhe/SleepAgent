from sleepagent.radar_agent.runtime.contracts import (
    CANONICAL_AGENT_RUNTIME_CONTRACT_VERSION,
    CANONICAL_AGENT_RUNTIME_KIND,
    HISTORICAL_AGENT_RUNTIME_KINDS,
    RadarAgentTask,
    RadarArtifactVersion,
    RadarNodeStatus,
    RadarTaskEvent,
    RadarTaskFailure,
    RadarTaskStatus,
    UserInputRequest,
    UserInputResponse,
    WorkflowRuntime,
)


def __getattr__(name: str):
    if name in {
        "TRACE_SCHEMA_VERSION",
        "build_developer_trace",
        "sanitize_trace_value",
    }:
        from sleepagent.radar_agent.runtime import trace

        return getattr(trace, name)
    if name in {
        "BindingMismatch",
        "AuthorizationRequired",
        "IdempotencyConflict",
        "InvalidNodeTransition",
        "InvalidTaskTransition",
        "RoleAccessDenied",
        "TaskService",
    }:
        from sleepagent.radar_agent.runtime import service

        return getattr(service, name)
    raise AttributeError(name)

__all__ = [
    "CANONICAL_AGENT_RUNTIME_CONTRACT_VERSION",
    "CANONICAL_AGENT_RUNTIME_KIND",
    "HISTORICAL_AGENT_RUNTIME_KINDS",
    "TRACE_SCHEMA_VERSION",
    "build_developer_trace",
    "sanitize_trace_value",
    "RadarAgentTask",
    "RadarArtifactVersion",
    "RadarNodeStatus",
    "RadarTaskEvent",
    "RadarTaskFailure",
    "RadarTaskStatus",
    "UserInputRequest",
    "UserInputResponse",
    "AuthorizationRequired",
    "BindingMismatch",
    "IdempotencyConflict",
    "InvalidNodeTransition",
    "InvalidTaskTransition",
    "RoleAccessDenied",
    "TaskService",
    "WorkflowRuntime",
]
