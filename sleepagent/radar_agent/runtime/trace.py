from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sleepagent.observability import redact_text

from .contracts import RadarArtifactVersion, RadarTaskEvent

if TYPE_CHECKING:
    from .service import TaskService


TRACE_SCHEMA_VERSION = "developer-trace.v1"
_SENSITIVE_KEY = re.compile(
    r"(authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|"
    r"client[-_]?secret|secret|password|signature|cookie|raw_payload|"
    r"request_body|response_body|messages|content|user_message)",
    re.IGNORECASE,
)


def build_developer_trace(task_service: TaskService, task_id: str) -> dict[str, Any]:
    """Build a minimized trace from the same persisted source used by API/SSE."""

    task = task_service.get_task(task_id)
    artifacts = task_service.list_artifacts(task_id)
    workflow = _latest_workflow_payload(artifacts)
    ledgers = [item.evidence_ledger for item in artifacts if item.evidence_ledger]
    claims = [claim for ledger in ledgers for claim in ledger.claims]
    return sanitize_trace_value(
        {
            "schema_version": TRACE_SCHEMA_VERSION,
            "task": {
                "task_id": task.task_id,
                "trace_id": task.trace_id,
                "scenario": task.scenario,
                "runtime_kind": task.runtime_kind,
                "runtime_contract_version": task.runtime_contract_version,
                "execution_mode": task.execution_mode,
                "completion_status": task.completion_status,
                "status": task.status.value,
                "retry_count": task.retry_count,
                "parent_task_id": task.parent_task_id,
                "failure": (
                    {
                        "error_code": task.failure.error_code,
                        "summary": task.failure.message,
                        "failed_node": task.failure.failed_node,
                        "retryable": task.failure.retryable,
                    }
                    if task.failure
                    else None
                ),
                "node_status": {
                    node: status.value for node, status in task.node_status.items()
                },
            },
            "events": [
                _event_summary(event) for event in task_service.list_events(task_id)
            ],
            "claims": [
                {
                    "claim_id": claim.claim_id,
                    "summary": claim.text,
                    "generated_by": claim.generated_by,
                    "risk_level": claim.risk_level.value,
                    "confidence": claim.confidence,
                    "review_status": claim.review_status.value,
                    "evidence_refs": claim.evidence_refs,
                    "caveats": claim.caveats,
                }
                for claim in _unique_by_id(claims, "claim_id")
            ],
            "a2a": [
                {
                    "message_id": item.message_id,
                    "sender": item.sender,
                    "receiver": item.receiver,
                    "intent": item.intent,
                    "requested_action": item.requested_action,
                    "request_type": item.request_type,
                    "risk_level": item.risk_level.value,
                    "message_status": item.message_status,
                    "resolution_status": item.resolution_status,
                    "resolution_summary": item.resolution_summary,
                    "source_agent_invocation_id": item.source_agent_invocation_id,
                    "target_agent_invocation_id": item.target_agent_invocation_id,
                    "expected_output_schema": item.expected_output_schema,
                    "evidence_refs": item.evidence_refs,
                }
                for item in task_service.list_a2a_messages(task_id)
            ],
            "conflicts": [
                {
                    "conflict_id": item.conflict_id,
                    "sources": item.sources,
                    "summary": item.summary,
                    "decision": item.decision,
                    "final_status": item.final_status,
                    "requires_human_confirmation": item.requires_human_confirmation,
                    "evidence_refs": item.evidence_refs,
                }
                for item in task_service.list_conflicts(task_id)
            ],
            "llm": _llm_summaries(artifacts),
            "questionnaires": [
                {
                    "question_id": item.get("question_id"),
                    "summary": item.get("prompt_text"),
                    "question_source": item.get("question_source"),
                    "source_id": item.get("source_id"),
                    "source_version": item.get("source_version"),
                    "policy_id": item.get("policy_id"),
                    "policy_version": item.get("policy_version"),
                }
                for item in workflow.get("questionnaire_candidates", [])
            ],
            "alert_decision": {
                key: value
                for key, value in workflow.get("alert_decision", {}).items()
                if key
                in {
                    "risk_level",
                    "candidate_actions",
                    "automatic_actions",
                    "external_action_executed",
                    "urgent_safety_notice_displayed",
                }
            },
            "memory_candidates": workflow.get("memory_candidates", []),
            "confirmations": [
                {
                    "confirmation_id": item.confirmation_id,
                    "action_type": item.action_type,
                    "requested_role": item.requested_role,
                    "status": item.status,
                    "reason_summary": item.reason,
                    "evidence_refs": item.evidence_refs,
                    "execution_status": item.execution_status,
                    "execution_ref": item.execution_ref,
                }
                for item in task_service.list_confirmations(task_id)
            ],
            "artifacts": [_artifact_summary(item) for item in artifacts],
            "dynamic": _dynamic_trace(task_service, task),
        }
    )


def _dynamic_trace(task_service: TaskService, task: Any) -> dict[str, Any]:
    if task.runtime_kind != "dynamic_goal":
        return {}
    store = task_service.store
    try:
        budget = store.get_runtime_budget(task.task_id).model_dump(mode="json")
    except KeyError:
        budget = None
    try:
        receipt = store.get_completion_receipt(task.task_id).model_dump(mode="json")
    except KeyError:
        receipt = None
    return {
        "plans": [
            plan.model_dump(mode="json")
            for plan in store.list_execution_plans(task.task_id)
        ],
        "model_invocations": [
            item.model_dump(mode="json")
            for item in store.list_model_invocations(task.task_id)
        ],
        "agent_invocations": [
            item.model_dump(mode="json")
            for item in store.list_agent_invocations(task.task_id)
        ],
        "tool_invocations": [
            item.model_dump(mode="json")
            for item in store.list_tool_invocations(task.task_id)
        ],
        "user_input_requests": [
            item.model_dump(mode="json")
            for item in store.list_user_input_requests(task.task_id)
        ],
        "budget": budget,
        "completion_receipt": receipt,
    }


def sanitize_trace_value(value: Any, *, depth: int = 0) -> Any:
    """Redact secrets/raw bodies while preserving trace and evidence reference IDs."""

    if depth > 8:
        return "[max-depth]"
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            clean[key] = (
                "[redacted]"
                if _SENSITIVE_KEY.search(key)
                else sanitize_trace_value(item, depth=depth + 1)
            )
        return clean
    if isinstance(value, (list, tuple)):
        return [sanitize_trace_value(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _event_summary(event: RadarTaskEvent) -> dict[str, Any]:
    allowed_payload_keys = {
        "status", "node", "error_code", "failed_node", "retryable",
        "retry_count", "artifact_id", "version", "message_id", "sender",
        "receiver", "intent", "evidence_refs", "risk_level", "claim_id",
        "generated_by", "confidence", "conflict_id", "sources",
        "final_status", "requires_human_confirmation", "role",
        "generation_mode", "model_provider", "model_id", "prompt_version",
        "output_schema_status", "fallback_reason", "confirmation_id",
        "action_type", "actor_role", "memory_summary_id", "memory_type",
        "agent_name", "skill_version", "claim_count", "safety_flags",
        "question_id", "question_source", "source_id", "source_version",
        "policy_id", "policy_version",
        "goal_type", "execution_mode", "completion_status", "revision",
        "capabilities", "agent", "capability", "uncertainty_count",
        "tool_name", "decision", "decision_summary", "checkpoint_kind",
        "question_type", "why_needed", "reason_code", "skip_reason",
        "reused_from_plan_id", "resolution_status", "changed_fields",
        "source_agent_invocation_id", "target_agent_invocation_id",
        "causal_source_agent_invocation_id", "artifact_version_id",
        "request_type",
    }
    return {
        "sequence": event.sequence,
        "event_type": event.event_type,
        "summary": event.message,
        "created_at": event.created_at.isoformat(),
        "refs": {
            key: value for key, value in event.payload.items() if key in allowed_payload_keys
        },
    }


def _artifact_summary(item: RadarArtifactVersion) -> dict[str, Any]:
    report = item.report
    return {
        "artifact_version_id": item.artifact_version_id,
        "artifact_id": item.artifact_id,
        "artifact_type": item.artifact_type,
        "version": item.version,
        "source_refs": item.source_refs,
        "role": report.role if report else None,
        "generation_mode": report.generation_mode if report else None,
        "prompt_version": report.prompt_version if report else item.metadata.get("prompt_version"),
        "model_provider": report.model_provider if report else item.metadata.get("model_provider"),
        "model_id": report.model_id if report else item.metadata.get("model_id"),
        "created_at": item.created_at.isoformat(),
    }


def _llm_summaries(artifacts: list[RadarArtifactVersion]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for item in artifacts:
        if item.report is None:
            continue
        invocation = item.metadata.get("llm_invocation") or {}
        summaries.append(
            {
                "artifact_id": item.artifact_id,
                "role": item.report.role,
                "generation_mode": item.report.generation_mode,
                "model_provider": item.report.model_provider,
                "model_id": item.report.model_id,
                "prompt_version": item.report.prompt_version,
                "output_schema_status": invocation.get("output_schema_status"),
                "fallback_used": invocation.get(
                    "fallback_used", item.report.generation_mode == "fallback"
                ),
                "fallback_reason": invocation.get("fallback_reason"),
            }
        )
    return summaries


def _latest_workflow_payload(
    artifacts: list[RadarArtifactVersion],
) -> dict[str, Any]:
    values = [item for item in artifacts if item.artifact_type == "workflow_run"]
    if not values:
        return {}
    latest = max(values, key=lambda item: (item.version, item.created_at))
    return latest.payload


def _unique_by_id(items: list[Any], field: str) -> list[Any]:
    unique: dict[str, Any] = {}
    for item in items:
        unique[getattr(item, field)] = item
    return list(unique.values())


__all__ = ["TRACE_SCHEMA_VERSION", "build_developer_trace", "sanitize_trace_value"]
