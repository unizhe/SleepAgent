from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sleepagent.observability import redact_text
from sleepagent.radar_agent.persistence.history import (
    HistoricalRecord,
    HistoricalTaskRecord,
)

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
    if isinstance(task, HistoricalTaskRecord):
        return _historical_developer_trace(task_service, task)

    artifacts = task_service.list_artifacts(task_id)
    workflow = _latest_workflow_payload(artifacts)
    ledgers = [item.evidence_ledger for item in artifacts if item.evidence_ledger]
    claims = [claim for ledger in ledgers for claim in ledger.claims]
    return sanitize_trace_value(
        {
            "schema_version": TRACE_SCHEMA_VERSION,
            "task": _canonical_task_summary(task),
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
            "history": {},
        }
    )


def _historical_developer_trace(
    task_service: TaskService,
    task: HistoricalTaskRecord,
) -> dict[str, Any]:
    """Render retired-runtime records without loading executable contracts."""

    persisted = task_service.store.history.read_trace(task.task_id)
    historical = persisted.to_dict()
    return sanitize_trace_value(
        {
            "schema_version": TRACE_SCHEMA_VERSION,
            "task": _historical_task_summary(task),
            "events": [
                _historical_event_summary(event)
                for event in persisted.events
            ],
            "claims": [],
            "llm": [],
            "questionnaires": [],
            "alert_decision": {},
            "memory_candidates": [],
            "a2a": [item.to_dict() for item in persisted.a2a_messages],
            "conflicts": [item.to_dict() for item in persisted.conflicts],
            "confirmations": [
                item.to_dict() for item in persisted.confirmations
            ],
            "artifacts": [item.to_dict() for item in persisted.artifacts],
            "history": {
                key: value
                for key, value in historical.items()
                if key
                not in {
                    "task",
                    "events",
                    "a2a_messages",
                    "conflicts",
                    "confirmations",
                    "artifacts",
                }
            },
        }
    )


def _canonical_task_summary(task: Any) -> dict[str, Any]:
    failure = getattr(task, "failure", None)
    return {
        "task_id": task.task_id,
        "trace_id": task.trace_id,
        "scenario": task.scenario,
        "runtime_kind": task.runtime_kind,
        "runtime_contract_version": task.runtime_contract_version,
        "execution_mode": task.execution_mode,
        "completion_status": task.completion_status,
        "status": _enum_value(task.status),
        "retry_count": task.retry_count,
        "parent_task_id": task.parent_task_id,
        "failure": (
            {
                "error_code": failure.error_code,
                "summary": failure.message,
                "failed_node": failure.failed_node,
                "retryable": failure.retryable,
            }
            if failure
            else None
        ),
        "node_status": {
            node: _enum_value(status)
            for node, status in getattr(task, "node_status", {}).items()
        },
    }


def _historical_task_summary(task: HistoricalTaskRecord) -> dict[str, Any]:
    persisted = task.to_dict()
    failure = persisted.get("failure") or {}
    return {
        "task_id": task.task_id,
        "trace_id": task.trace_id,
        "scenario": task.scenario,
        "runtime_kind": task.runtime_kind,
        "runtime_contract_version": task.runtime_contract_version,
        "execution_mode": task.execution_mode,
        "completion_status": task.completion_status,
        "status": task.status,
        "retry_count": task.retry_count,
        "parent_task_id": task.parent_task_id,
        "failure": (
            {
                "error_code": failure.get("error_code"),
                "summary": failure.get("message"),
                "failed_node": failure.get("failed_node"),
                "retryable": failure.get("retryable"),
            }
            if failure
            else None
        ),
        "node_status": persisted.get("node_status") or {},
    }


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


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
    return {
        "sequence": event.sequence,
        "event_type": event.event_type,
        "summary": event.message,
        "created_at": event.created_at.isoformat(),
        "refs": _event_refs(event.payload),
    }


def _historical_event_summary(event: HistoricalRecord) -> dict[str, Any]:
    payload = event.to_dict()
    created_at = payload.get("created_at")
    return {
        "sequence": payload.get("sequence"),
        "event_type": payload.get("event_type"),
        "summary": payload.get("message"),
        "created_at": (
            created_at.isoformat()
            if hasattr(created_at, "isoformat")
            else created_at
        ),
        "refs": _event_refs(payload.get("payload", {})),
    }


def _event_refs(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    allowed_payload_keys = {
        "status", "node", "error_code", "failed_node", "retryable",
        "retry_count", "artifact_id", "version", "message_id", "sender",
        "receiver", "intent", "evidence_refs", "risk_level", "claim_id",
        "generated_by", "confidence", "role",
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
    return {key: value for key, value in payload.items() if key in allowed_payload_keys}


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
