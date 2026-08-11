from sleepagent.product_runtime.task_runtime.contracts import (
    CANONICAL_AGENT_RUNTIME_CONTRACT_VERSION,
    CANONICAL_AGENT_RUNTIME_KIND,
    RadarAgentTask,
    RadarArtifactVersion,
    RadarTaskEvent,
    RadarTaskFailure,
    RadarTaskStatus,
    UserInputRequest,
    UserInputResponse,
)


def __getattr__(name: str):
    if name in {
        "TRACE_SCHEMA_VERSION",
        "build_developer_trace",
        "sanitize_trace_value",
    }:
        from sleepagent.product_runtime.task_runtime import trace

        return getattr(trace, name)
    if name in {
        "BindingMismatch",
        "AuthorizationRequired",
        "IdempotencyConflict",
        "InvalidTaskTransition",
        "RoleAccessDenied",
        "TaskService",
    }:
        from sleepagent.product_runtime.task_runtime import service

        return getattr(service, name)
    raise AttributeError(name)

__all__ = [
    "CANONICAL_AGENT_RUNTIME_CONTRACT_VERSION",
    "CANONICAL_AGENT_RUNTIME_KIND",
    "TRACE_SCHEMA_VERSION",
    "build_developer_trace",
    "sanitize_trace_value",
    "RadarAgentTask",
    "RadarArtifactVersion",
    "RadarTaskEvent",
    "RadarTaskFailure",
    "RadarTaskStatus",
    "UserInputRequest",
    "UserInputResponse",
    "AuthorizationRequired",
    "BindingMismatch",
    "IdempotencyConflict",
    "InvalidTaskTransition",
    "RoleAccessDenied",
    "TaskService",
]
