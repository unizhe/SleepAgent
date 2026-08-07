from __future__ import annotations

import json
import io
import sqlite3
from pathlib import Path

import pytest

from sleepagent.radar_agent.api.http import (
    RadarApiRuntime,
    RadarTaskCreateRequest,
    _format_sse_event,
)
from sleepagent.radar_agent.orchestrator import WORKFLOW_NODE_ORDER
from sleepagent.radar_agent.cli import EXIT_OK, main as cli_main
from sleepagent.radar_agent.replay import (
    ReplayWorkflowGolden,
    get_replay_scenario,
    load_workflow_goldens,
    replay_scenario_ids,
)
from sleepagent.radar_agent.runtime import RadarNodeStatus, build_developer_trace


GOLDENS = load_workflow_goldens()


def test_workflow_golden_catalog_is_fixed_and_covers_exactly_seven_scenarios() -> None:
    assert GOLDENS.version == "radar-workflow-goldens.v1"
    assert [item.scenario_id for item in GOLDENS.scenarios] == list(
        replay_scenario_ids()
    )
    assert len(GOLDENS.scenarios) == 7


@pytest.mark.parametrize("golden", GOLDENS.scenarios, ids=lambda item: item.scenario_id)
def test_each_replay_scenario_matches_complete_persisted_workflow_golden(
    golden: ReplayWorkflowGolden,
) -> None:
    runtime = RadarApiRuntime(
        sqlite3.connect(":memory:", check_same_thread=False)
    )
    task = runtime.create_task(
        RadarTaskCreateRequest(
            runtime_kind="legacy_fixed",
            scenario=golden.scenario_id,
        ),
        idempotency_key=f"golden:{golden.scenario_id}",
    )
    detail = runtime.run_task(task.task_id)
    persisted = runtime.service.get_task(task.task_id)
    events = runtime.service.list_events(task.task_id)
    artifacts = runtime.service.list_artifacts(task.task_id)
    audits = runtime.store.list_audit_logs(task.task_id)
    workflow = next(
        item for item in artifacts if item.artifact_type == "workflow_run"
    )
    ledger = next(
        item.evidence_ledger
        for item in artifacts
        if item.evidence_ledger is not None
    )
    reports = [item.report for item in artifacts if item.report is not None]

    assert persisted.status.value == "completed"
    assert all(
        persisted.node_status[node.value] == RadarNodeStatus.SUCCEEDED
        for node in WORKFLOW_NODE_ORDER
    )
    assert workflow.payload["terminal_node"] == GOLDENS.common.terminal_node
    assert [item["agent_name"] for item in workflow.payload["agents"]] == (
        GOLDENS.common.agent_names
    )
    assert [item["intent"] for item in workflow.payload["a2a"]] == (
        GOLDENS.common.a2a_intents
    )
    assert all(
        item["message_status"] == "handled"
        for item in workflow.payload["a2a"]
    )

    assert ledger is not None
    assert ledger.derived_metrics["data_quality_status"] == (
        golden.canonical_data_quality
    )
    assert ledger.derived_metrics["risk_level"] == golden.risk_level
    assert len(ledger.claims) == golden.claim_count
    assert all(claim.evidence_refs for claim in ledger.claims)
    assert all(claim.confidence >= 0 for claim in ledger.claims)
    assert all(claim.caveats for claim in ledger.claims)
    assert ledger.caveats
    assert ledger.review_status.value == golden.review_status

    assert workflow.payload["rag"]["reviewed_seed_only"] is True
    assert workflow.payload["rag"]["evidence_refs"]
    assert all(
        ref.startswith("seed:")
        for ref in workflow.payload["rag"]["evidence_refs"]
    )
    assert [item.question_id for item in detail.questionnaire_candidates] == (
        golden.questionnaire_ids
    )
    assert all(
        item.question_source in {"bank", "skill"}
        and item.source_version
        and item.policy_version
        for item in detail.questionnaire_candidates
    )

    assert [item.action_type for item in detail.confirmations] == (
        golden.confirmation_actions
    )
    assert all(item.status == "pending" for item in detail.confirmations)
    assert all(item.execution_status == "not_started" for item in detail.confirmations)
    assert workflow.payload["alert_decision"]["automatic_actions"] == (
        golden.automatic_actions
    )
    assert workflow.payload["alert_decision"]["external_action_executed"] is False
    assert len(workflow.payload["memory_candidates"]) == (
        golden.memory_candidate_count
    )
    assert workflow.payload["memory_rejected_inputs"] == (
        golden.memory_rejected_inputs
    )
    assert len(workflow.payload["conflicts"]) == golden.conflict_count

    assert sorted(report.role for report in reports) == sorted(
        GOLDENS.common.report_roles
    )
    assert {report.generation_mode for report in reports} == {
        GOLDENS.common.report_generation_mode
    }
    assert len({tuple(report.claim_ids) for report in reports}) == 1
    assert len({report.risk_level.value for report in reports}) == 1
    assert len(
        {
            json.dumps(
                [fact.model_dump(mode="json") for fact in report.facts],
                ensure_ascii=False,
                sort_keys=True,
            )
            for report in reports
        }
    ) == 1
    assert len({report.content for report in reports}) == 3
    assert all(report.caveats and report.safety_notices for report in reports)

    assert [item.artifact_type for item in artifacts] == (
        GOLDENS.common.artifact_types
    )
    assert all(item.version == 1 for item in artifacts)
    audit_actions = {item.action for item in audits}
    assert set(GOLDENS.common.required_audit_actions) <= audit_actions
    if golden.confirmation_actions:
        assert "confirmation_requested" in audit_actions
    assert all(item.payload.get("trace_id") == task.trace_id for item in audits)

    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    event_types = {event.event_type for event in events}
    assert {
        "agent.completed",
        "a2a.message",
        "claim.created",
        "artifact.version_created",
        "llm.fallback",
        "task.completed",
    } <= event_types
    if golden.questionnaire_ids:
        assert "questionnaire.candidate" in event_types
    if golden.conflict_count:
        assert "conflict.resolved" in event_types
    sse = "".join(_format_sse_event(event) for event in events)
    assert f"id: {events[-1].sequence}" in sse
    assert f'"trace_id": "{task.trace_id}"' in sse

    trace = build_developer_trace(runtime.service, task.task_id)
    assert trace["task"]["trace_id"] == task.trace_id
    assert [item["intent"] for item in trace["a2a"]] == (
        GOLDENS.common.a2a_intents
    )
    assert {item["generation_mode"] for item in trace["llm"]} == {"fallback"}
    serialized_trace = json.dumps(trace, ensure_ascii=False)
    assert all(report.content not in serialized_trace for report in reports)

    scenario = get_replay_scenario(golden.scenario_id)
    assert detail.risk_level == scenario.expected.risk_level.value
    assert [item.question_id for item in detail.questionnaire_candidates] == (
        scenario.expected.questionnaire_candidates
    )
    assert [item.action_type for item in detail.confirmations] == (
        scenario.expected.confirmation_candidates
    )

    if golden.scenario_id == "device_or_data_quality_issue":
        assert not workflow.payload["alert_decision"]["candidate_actions"]
        assert not workflow.payload["memory_candidates"]
        assert all(claim.risk_level.value != "escalate" for claim in ledger.claims)
    if golden.scenario_id == "urgent_boundary_text_input":
        assert workflow.payload["risk_decision"][
            "should_stop_sleep_trend_explanation"
        ] is True
        assert workflow.payload["alert_decision"][
            "urgent_safety_notice_displayed"
        ] is True


def test_frontend_reads_runtime_questionnaires_instead_of_expected_catalog() -> None:
    project_root = Path(__file__).resolve().parents[1]
    workspace = (
        project_root / "frontend/components/radar/RadarWorkspace.tsx"
    ).read_text(encoding="utf-8")

    assert "detail.questionnaire_candidates" in workspace
    assert "scenario.expected.questionnaire_candidates" not in workspace


@pytest.mark.parametrize("golden", GOLDENS.scenarios, ids=lambda item: item.scenario_id)
def test_each_replay_scenario_has_machine_readable_cli_trace(
    golden: ReplayWorkflowGolden,
) -> None:
    runtime = RadarApiRuntime(
        sqlite3.connect(":memory:", check_same_thread=False)
    )
    stdout = io.StringIO()

    assert cli_main(
        ["run-demo", "--scenario", golden.scenario_id, "--format", "jsonl"],
        runtime=runtime,
        stdout=stdout,
    ) == EXIT_OK

    rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert rows[0]["type"] == "trace"
    assert rows[0]["status"] == "completed"
    row_types = {row["type"] for row in rows}
    assert {
        "event",
        "claim",
        "a2a",
        "llm",
        "alert_decision",
        "artifact",
    } <= row_types
    if golden.questionnaire_ids:
        assert "questionnaire" in row_types
    if golden.memory_candidate_count:
        assert "memory_candidate" in row_types
