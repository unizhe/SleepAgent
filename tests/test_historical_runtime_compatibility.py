from __future__ import annotations

import ast
import inspect
import json
import sqlite3
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from sleepagent.radar_agent.persistence.history import (
    HistoricalRecord,
    HistoricalRuntimeReadError,
    HistoricalRuntimeReader,
)
from sleepagent.radar_agent.persistence.migrations import apply_sqlite_migration


NOW = "2026-08-09T01:02:03+00:00"


def _json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _historical_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    # Build the immutable, already-applied schema, then populate it exclusively
    # with raw SQL.  No retired DTO or persistence writer participates.
    apply_sqlite_migration(connection)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        INSERT INTO radar_subjects (
          subject_id, display_name, timezone_name, subject_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "history-subject",
            "Historical Subject",
            "UTC",
            _json({"subject_id": "history-subject", "future_subject_field": 1}),
            NOW,
            NOW,
        ),
    )
    _insert_task(
        connection,
        task_id="history-dynamic",
        runtime_kind="dynamic_goal",
        contract_version="radar-dynamic.v1",
        # Deliberately omit runtime metadata: pre-migration task_json rows did
        # not contain the columns later added by migration 002.
        task_payload={
            "task_id": "history-dynamic",
            "trace_id": "trace:history-dynamic",
            "subject_id": "history-subject",
            "future_task_field": {"nested": [1, 2, {"kept": True}]},
        },
    )
    _insert_task(
        connection,
        task_id="history-fixed",
        runtime_kind="legacy_fixed",
        contract_version="radar-legacy.v1",
        task_payload={
            "task_id": "history-fixed",
            "trace_id": "trace:history-fixed",
            "subject_id": "history-subject",
            "legacy_node_state": {"report": "succeeded"},
        },
    )
    _insert_task(
        connection,
        task_id="canonical-product",
        runtime_kind="product_episode",
        contract_version="product-episode.v1",
        task_payload={
            "task_id": "canonical-product",
            "runtime_kind": "product_episode",
            "runtime_contract_version": "product-episode.v1",
        },
    )
    connection.execute(
        """
        INSERT INTO radar_task_events (
          event_id, task_id, trace_id, sequence, event_type, event_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "history-event-1",
            "history-dynamic",
            "trace-dynamic",
            1,
            "historical.step.completed",
            _json(
                {
                    "event_id": "history-event-1",
                    "task_id": "history-dynamic",
                    "sequence": 1,
                    "event_type": "historical.step.completed",
                    "payload": {"future_event_key": "preserved"},
                }
            ),
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO radar_dynamic_goals (
          goal_id, task_id, goal_type, goal_json, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            "history-goal",
            "history-dynamic",
            "night_review",
            _json({"goal_id": "history-goal", "task_id": "history-dynamic"}),
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO radar_dynamic_plan_revisions (
          plan_id, task_id, goal_id, revision, supersedes_plan_id,
          plan_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "history-plan",
            "history-dynamic",
            "history-goal",
            1,
            None,
            _json(
                {
                    "plan_id": "history-plan",
                    "task_id": "history-dynamic",
                    "goal_id": "history-goal",
                    "revision": 1,
                    "steps": [
                        {
                            "step_id": "history-step",
                            "ordinal": 1,
                            "kind": "call_agent",
                            "agent": "evidence_analysis",
                            "future_step_key": ["kept"],
                        }
                    ],
                    "future_plan_key": {"version": 99},
                }
            ),
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO radar_model_invocations (
          invocation_id, task_id, purpose, agent, client_idempotency_key,
          attempt, outcome, provider_request_id, invocation_json,
          started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "history-model",
            "history-dynamic",
            "plan",
            "orchestrator",
            "history-model-key",
            1,
            "succeeded",
            "provider-history",
            _json(
                {
                    "invocation_id": "history-model",
                    "task_id": "history-dynamic",
                    "purpose": "plan",
                    "agent": "orchestrator",
                    "future_model_key": {"tokens": [3, 5]},
                }
            ),
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO radar_agent_invocations (
          invocation_id, task_id, plan_id, step_id, agent, status,
          caused_by_a2a_message_id, invocation_json, started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "history-capability",
            "history-dynamic",
            "history-plan",
            "history-step",
            "evidence_analysis",
            "succeeded",
            None,
            _json(
                {
                    "invocation_id": "history-capability",
                    "task_id": "history-dynamic",
                    "plan_id": "history-plan",
                    "step_id": "history-step",
                    "agent": "evidence_analysis",
                    "future_capability_key": True,
                }
            ),
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO radar_tool_invocations (
          invocation_id, task_id, plan_id, step_id, tool_name,
          client_idempotency_key, attempt, outcome, invocation_json,
          started_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "history-tool",
            "history-dynamic",
            "history-plan",
            "history-step",
            "trend_analysis",
            "history-tool-key",
            1,
            "succeeded",
            _json(
                {
                    "invocation_id": "history-tool",
                    "task_id": "history-dynamic",
                    "tool_name": "trend_analysis",
                    "future_tool_key": {"coverage": 0.91},
                }
            ),
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO radar_user_input_requests (
          request_id, task_id, question_id, status, request_json,
          response_json, created_at, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "history-question",
            "history-dynamic",
            "q-old-observation",
            "answered",
            _json(
                {
                    "request_id": "history-question",
                    "task_id": "history-dynamic",
                    "question_id": "q-old-observation",
                    "status": "answered",
                    "future_question_key": ["kept"],
                }
            ),
            _json({"answer": "historical answer"}),
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO radar_runtime_budgets (task_id, budget_json, updated_at)
        VALUES (?, ?, ?)
        """,
        (
            "history-dynamic",
            _json(
                {
                    "task_id": "history-dynamic",
                    "total_model_call_count": 4,
                    "future_budget_key": {"unit": "calls"},
                }
            ),
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO radar_completion_receipts (
          receipt_id, task_id, execution_mode, completion_status,
          receipt_json, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "history-receipt",
            "history-dynamic",
            "intelligent",
            "partial",
            _json(
                {
                    "receipt_id": "history-receipt",
                    "task_id": "history-dynamic",
                    "execution_mode": "intelligent",
                    "completion_status": "partial",
                    "future_receipt_key": {"reason": "historical"},
                }
            ),
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO radar_human_confirmations (
          confirmation_id, task_id, action_type, requested_role, status,
          confirmation_json, created_at, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "history-confirmation",
            "history-dynamic",
            "share_report",
            "family",
            "approved",
            _json(
                {
                    "confirmation_id": "history-confirmation",
                    "task_id": "history-dynamic",
                    "status": "approved",
                    "future_confirmation_key": {"preserved": True},
                }
            ),
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO radar_task_artifact_versions (
          artifact_version_id, artifact_id, task_id, artifact_type,
          version_number, artifact_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "history-artifact-version",
            "history-artifact",
            "history-dynamic",
            "workflow_run",
            1,
            _json(
                {
                    "artifact_version_id": "history-artifact-version",
                    "artifact_id": "history-artifact",
                    "task_id": "history-dynamic",
                    "future_artifact_key": ["preserved"],
                }
            ),
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO radar_a2a_messages (
          message_id, task_id, target_task_id, sender, receiver, intent,
          risk_level, collaboration_round, routed_by, shared_artifact_type,
          message_status, message_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "history-message",
            "history-dynamic",
            None,
            "evidence_analysis",
            "orchestrator",
            "review",
            "low",
            1,
            "orchestrator",
            None,
            "delivered",
            _json(
                {
                    "message_id": "history-message",
                    "task_id": "history-dynamic",
                    "future_message_key": {"preserved": True},
                }
            ),
            NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO radar_conflict_records (
          conflict_id, task_id, final_status, requires_human_confirmation,
          conflict_json, decided_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "history-conflict",
            "history-dynamic",
            "resolved",
            0,
            _json(
                {
                    "conflict_id": "history-conflict",
                    "task_id": "history-dynamic",
                    "future_conflict_key": {"preserved": True},
                }
            ),
            NOW,
        ),
    )
    connection.commit()
    return connection


def _insert_task(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    runtime_kind: str,
    contract_version: str,
    task_payload: dict[str, object],
) -> None:
    connection.execute(
        """
        INSERT INTO radar_tasks (
          task_id, trace_id, subject_id, radar_device_id, role, scenario,
          status, task_json, created_at, updated_at, runtime_kind,
          runtime_contract_version, task_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            f"trace:{task_id}",
            "history-subject",
            "history-device",
            "family",
            "historical",
            "completed",
            _json(task_payload),
            NOW,
            NOW,
            runtime_kind,
            contract_version,
            1,
        ),
    )


def test_reader_decodes_complete_historical_trace_without_mutating_storage() -> None:
    connection = _historical_connection()
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    changes_before = connection.total_changes

    trace = HistoricalRuntimeReader(connection, dialect="sqlite").read_trace(
        "history-dynamic"
    )

    assert connection.total_changes == changes_before
    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert trace.task.runtime_kind == "dynamic_goal"
    assert trace.task.runtime_contract_version == "radar-dynamic.v1"
    assert trace.task.future_task_field == {
        "nested": (1, 2, MappingProxyType({"kept": True}))
    }
    assert trace.events[0]["payload"] == MappingProxyType(
        {"future_event_key": "preserved"}
    )
    assert trace.confirmations[0]["future_confirmation_key"] == MappingProxyType(
        {"preserved": True}
    )
    assert trace.artifacts[0]["future_artifact_key"] == ("preserved",)
    assert trace.a2a_messages[0]["future_message_key"] == MappingProxyType(
        {"preserved": True}
    )
    assert trace.conflicts[0]["future_conflict_key"] == MappingProxyType(
        {"preserved": True}
    )
    assert trace.plans[0]["future_plan_key"] == MappingProxyType({"version": 99})
    assert trace.model_invocations[0]["future_model_key"] == MappingProxyType(
        {"tokens": (3, 5)}
    )
    assert trace.agent_invocations[0]["future_capability_key"] is True
    assert trace.tool_invocations[0]["future_tool_key"] == MappingProxyType(
        {"coverage": 0.91}
    )
    assert trace.user_input_requests[0]["future_question_key"] == ("kept",)
    assert trace.budget is not None
    assert trace.budget["future_budget_key"] == MappingProxyType({"unit": "calls"})
    assert trace.completion_receipt is not None
    assert trace.completion_receipt["future_receipt_key"] == MappingProxyType(
        {"reason": "historical"}
    )
    with pytest.raises(TypeError):
        trace.task.future_task_field["nested"][2]["kept"] = False
    with pytest.raises(ValidationError):
        trace.task.status = "running"

    detached = trace.model_dump(mode="json")
    assert detached["task"]["future_task_field"]["nested"][2] == {"kept": True}
    detached["task"]["future_task_field"]["nested"][2]["kept"] = False
    assert trace.task.future_task_field == {
        "nested": (1, 2, MappingProxyType({"kept": True}))
    }


def test_reader_handles_fixed_history_and_rejects_canonical_tasks() -> None:
    reader = HistoricalRuntimeReader(_historical_connection(), dialect="sqlite")

    fixed = reader.read_trace("history-fixed")
    assert fixed.task.runtime_kind == "legacy_fixed"
    assert fixed.task.runtime_contract_version == "radar-legacy.v1"
    assert fixed.task.legacy_node_state == MappingProxyType(
        {"report": "succeeded"}
    )
    assert fixed.plans == ()
    assert fixed.budget is None

    with pytest.raises(HistoricalRuntimeReadError, match="retired runtimes"):
        reader.read_task("canonical-product")
    with pytest.raises(KeyError, match="historical task not found"):
        reader.read_task("missing-task")


def test_historical_records_and_reader_expose_no_mutation_runtime_surface() -> None:
    record = HistoricalRecord.decode(
        _json({"nested": {"values": [1, 2]}, "future": "preserved"}),
        record_name="test record",
    )
    with pytest.raises(TypeError):
        record["nested"]["values"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        record._values["future"] = "changed"  # type: ignore[index]

    public_reader_methods = {
        name
        for name, value in vars(HistoricalRuntimeReader).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }
    assert public_reader_methods == {
        "read_task",
        "read_events",
        "read_confirmations",
        "read_artifacts",
        "read_a2a_messages",
        "read_conflicts",
        "read_plans",
        "read_model_invocations",
        "read_agent_invocations",
        "read_tool_invocations",
        "read_user_input_requests",
        "read_runtime_budget",
        "read_completion_receipt",
        "read_trace",
    }

    module = inspect.getmodule(HistoricalRuntimeReader)
    assert module is not None
    source = inspect.getsource(module)
    imported_modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)
    for forbidden_import in (
        "radar_agent.dynamic",
        "radar_agent.agents",
        "radar_agent.orchestrator",
    ):
        assert not any(forbidden_import in name for name in imported_modules)
