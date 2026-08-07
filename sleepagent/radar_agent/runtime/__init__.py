from sleepagent.radar_agent.runtime.contracts import (
    RadarAgentTask,
    RadarArtifactVersion,
    RadarNodeStatus,
    RadarTaskEvent,
    RadarTaskFailure,
    RadarTaskStatus,
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
    "TRACE_SCHEMA_VERSION",
    "build_developer_trace",
    "sanitize_trace_value",
    "RadarAgentTask",
    "RadarArtifactVersion",
    "RadarNodeStatus",
    "RadarTaskEvent",
    "RadarTaskFailure",
    "RadarTaskStatus",
    "AuthorizationRequired",
    "BindingMismatch",
    "IdempotencyConflict",
    "InvalidNodeTransition",
    "InvalidTaskTransition",
    "RoleAccessDenied",
    "TaskService",
    "WorkflowRuntime",
]
