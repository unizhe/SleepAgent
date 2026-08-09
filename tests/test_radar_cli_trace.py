from __future__ import annotations

import io
import json
import sqlite3

from sleepagent.radar_agent.api.http import (
    RADAR_AGENT_DEV_MODE_ENV,
    RadarApiRuntime,
    RadarTaskCreateRequest,
)
from sleepagent.radar_agent.cli import (
    EXIT_INVALID_STATE,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_TASK_FAILED,
    main,
)
from sleepagent.radar_agent.runtime import (
    RadarAgentTask,
    RadarTaskStatus,
    build_developer_trace,
)
from sleepagent.radar_agent.persistence import RadarSubject
from sleepagent.radar_agent.provider import ReplayRadarProvider
from sleepagent.radar_agent.schemas import A2AMessage


def _runtime() -> RadarApiRuntime:
    return RadarApiRuntime(sqlite3.connect(":memory:", check_same_thread=False))


def test_run_demo_json_executes_shared_runtime_and_emits_trace() -> None:
    runtime = _runtime()
    stdout = io.StringIO()

    exit_code = main(
        ["run-demo", "--scenario", "normal_night", "--format", "json"],
        runtime=runtime,
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    persisted = runtime.service.get_task(payload["task"]["task_id"])
    assert exit_code == EXIT_OK
    assert payload["schema_version"] == "developer-trace.v1"
    assert payload["task"]["trace_id"] == persisted.trace_id
    assert payload["task"]["status"] == "completed"
    assert payload["task"]["runtime_kind"] == "product_episode"
    assert any(
        event["event_type"] == "tool.completed"
        for event in payload["events"]
    )
    assert any(
        item["artifact_type"] == "product_episode_result"
        for item in payload["artifacts"]
    )
    assert all("content" not in artifact for artifact in payload["artifacts"])


def test_run_demo_jsonl_and_pretty_expose_summary_views_only() -> None:
    runtime = _runtime()
    jsonl_out = io.StringIO()
    pretty_out = io.StringIO()

    assert main(
        ["run-demo", "--scenario", "frequent_out_of_bed", "--format", "jsonl"],
        runtime=runtime,
        stdout=jsonl_out,
    ) == EXIT_OK
    lines = [json.loads(line) for line in jsonl_out.getvalue().splitlines()]
    task_id = lines[0]["task_id"]
    assert lines[0]["type"] == "trace"
    assert {line["type"] for line in lines} >= {
        "trace", "event", "artifact"
    }

    runtime.service.forward_a2a_message(
        task_id,
        A2AMessage(
            message_id="a2a-cli-summary",
            task_id=task_id,
            sender="trend",
            receiver="risk_signal",
            intent="review_out_of_bed_trend",
            evidence_refs=["claim:out-of-bed-trend"],
        ),
    )

    assert main(
        ["inspect-task", task_id, "--format", "pretty"],
        runtime=runtime,
        stdout=pretty_out,
    ) == EXIT_OK
    rendered = pretty_out.getvalue()
    assert "Node progress" in rendered
    assert "Claims" in rendered
    assert "A2A" in rendered
    assert "a2a-cli-summary" in rendered
    assert "LLM / fallback" in rendered
    assert "Questionnaires" in rendered
    assert "Memory candidates" in rendered
    assert "Human confirmations" in rendered
    assert "Artifact versions" in rendered
    assert runtime.service.get_task(task_id).trace_id in rendered
    assert "snapshots" not in rendered
    assert "night_report" not in rendered


def test_inspect_task_exit_codes_are_stable() -> None:
    runtime = _runtime()
    stderr = io.StringIO()

    assert main(
        ["inspect-task", "task-missing"],
        runtime=runtime,
        stderr=stderr,
    ) == EXIT_NOT_FOUND

    runtime.store.save_subject(
        RadarSubject(
            subject_id="historical-subject",
            display_name="Historical Subject",
        )
    )
    device = ReplayRadarProvider().list_devices()[0].model_copy(
        update={"bound_subject_id": "historical-subject"}
    )
    runtime.store.save_device(device)
    task = RadarAgentTask(
        task_id="historical-cli-task",
        trace_id="historical-cli-trace",
        subject_id="historical-subject",
        radar_device_id=device.radar_device_id,
        runtime_kind="legacy_fixed",
        runtime_contract_version="radar-legacy.v1",
        node_status={"legacy-node": "pending"},
    )
    runtime.store.save_task(task)
    assert main(
        ["inspect-task", task.task_id, "--retry"],
        runtime=runtime,
        stderr=io.StringIO(),
    ) == EXIT_INVALID_STATE


def test_failed_task_can_retry_in_place_and_rerun_as_child() -> None:
    runtime = _runtime()
    original = runtime.create_task(
        RadarTaskCreateRequest(
            scenario="normal_night",
        ),
        idempotency_key=None,
    )
    runtime.service.transition_task(original.task_id, RadarTaskStatus.RUNNING)
    runtime.service.fail_task(
        original.task_id,
        error_code="injected_failure",
        message="deterministic CLI retry test",
        retryable=True,
    )

    assert main(
        ["inspect-task", original.task_id, "--format", "json"],
        runtime=runtime,
        stdout=io.StringIO(),
    ) == EXIT_TASK_FAILED

    retry_out = io.StringIO()
    assert main(
        ["inspect-task", original.task_id, "--retry", "--format", "json"],
        runtime=runtime,
        stdout=retry_out,
    ) == EXIT_OK
    retried = json.loads(retry_out.getvalue())
    assert retried["task"]["task_id"] == original.task_id
    assert retried["task"]["retry_count"] == 1
    assert any(event["event_type"] == "task.retried" for event in retried["events"])

    failed_for_rerun = runtime.create_task(
        RadarTaskCreateRequest(
            scenario="normal_night",
        ),
        idempotency_key=None,
    )
    runtime.service.transition_task(failed_for_rerun.task_id, RadarTaskStatus.RUNNING)
    runtime.service.fail_task(
        failed_for_rerun.task_id,
        error_code="injected_failure",
        message="deterministic CLI rerun test",
        retryable=True,
    )
    rerun_out = io.StringIO()
    assert main(
        ["inspect-task", failed_for_rerun.task_id, "--rerun", "--format", "json"],
        runtime=runtime,
        stdout=rerun_out,
    ) == EXIT_OK
    rerun = json.loads(rerun_out.getvalue())
    assert rerun["task"]["task_id"] != failed_for_rerun.task_id
    assert rerun["task"]["parent_task_id"] == failed_for_rerun.task_id


def test_trace_filters_secrets_raw_payloads_and_report_bodies() -> None:
    runtime = _runtime()
    task = runtime.create_task(
        RadarTaskCreateRequest(),
        idempotency_key=None,
    )
    runtime.service.emit_event(
        task.task_id,
        event_type="debug.provider",
        message="Bearer top-secret api_key=abc123 password=hunter2",
        payload={
            "api_key": "abc123",
            "raw_payload": {"heart_rate": 61},
            "content": "private report body",
            "evidence_refs": ["evidence:allowed-ref"],
        },
    )

    trace = build_developer_trace(runtime.service, task.task_id)
    serialized = json.dumps(trace, ensure_ascii=False)
    assert "top-secret" not in serialized
    assert "abc123" not in serialized
    assert "hunter2" not in serialized
    assert "private report body" not in serialized
    assert "heart_rate" not in serialized
    assert "evidence:allowed-ref" in serialized
    assert "[redacted]" in serialized


def test_run_goal_executes_canonical_product_episode(
    monkeypatch,
) -> None:
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "true")
    runtime = _runtime()
    stdout = io.StringIO()

    exit_code = main(
        [
            "run-goal",
            "--goal-type",
            "night_review",
            "--date",
            "2026-07-09",
            "--scenario",
            "normal_night",
            "--format",
            "json",
        ],
        runtime=runtime,
        stdout=stdout,
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == EXIT_OK
    assert payload["task"]["runtime_kind"] == "product_episode"
    assert payload["task"]["runtime_contract_version"] == "product-episode.v1"
    assert any(
        item["artifact_type"] == "product_episode_result"
        for item in payload["artifacts"]
    )
    assert not payload.get("dynamic")


def test_inspect_product_decision_trace_has_no_dynamic_runtime_sections(
    monkeypatch,
) -> None:
    monkeypatch.setenv(RADAR_AGENT_DEV_MODE_ENV, "true")
    runtime = _runtime()
    run_out = io.StringIO()
    assert main(
        [
            "run-goal",
            "--goal-type",
            "night_review",
            "--date",
            "2026-07-09",
            "--format",
            "json",
        ],
        runtime=runtime,
        stdout=run_out,
    ) == EXIT_OK
    task_id = json.loads(run_out.getvalue())["task"]["task_id"]
    trace_out = io.StringIO()
    assert main(
        [
            "inspect-task",
            task_id,
            "--decision-trace",
            "--format",
            "jsonl",
        ],
        runtime=runtime,
        stdout=trace_out,
    ) == EXIT_OK
    types = {
        json.loads(line)["type"] for line in trace_out.getvalue().splitlines()
    }
    assert "trace" in types
    assert "event" in types
    assert "artifact" in types
    assert not any(item.startswith("dynamic_") for item in types)
