"""Test-only driver for retired Radar workflow regression coverage."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from sleepagent.product_device.provider import (
    SUPPORTED_REPLAY_SCENARIOS,
    ReplayRadarProvider,
)
from sleepagent.product_runtime.task_runtime import (
    InvalidTaskTransition,
    RadarTaskStatus,
    UserInputResponse,
    build_developer_trace,
)


RADAR_AGENT_CLI_COMMANDS = ("run-demo", "run-goal", "answer-input", "inspect-task")
EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_NOT_FOUND = 3
EXIT_INVALID_STATE = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_demo = subparsers.add_parser("run-demo")
    run_demo.add_argument(
        "--scenario",
        choices=SUPPORTED_REPLAY_SCENARIOS,
        default="normal_night",
    )
    run_demo.add_argument("--format", choices=("pretty", "json", "jsonl"), default="pretty")

    run_goal = subparsers.add_parser("run-goal")
    run_goal.add_argument(
        "--goal-type",
        required=True,
        choices=(
            "night_review",
            "trend_comparison",
            "change_explanation",
            "data_quality_diagnosis",
            "doctor_material",
            "grounded_question",
        ),
    )
    run_goal.add_argument("--date")
    run_goal.add_argument("--start")
    run_goal.add_argument("--end")
    run_goal.add_argument("--question")
    run_goal.add_argument("--source-artifact")
    run_goal.add_argument("--source-date")
    run_goal.add_argument("--focus")
    run_goal.add_argument("--role", choices=("elder", "family", "doctor"), default="family")
    run_goal.add_argument("--actor-id", default="cli-user")
    run_goal.add_argument(
        "--scenario",
        choices=SUPPORTED_REPLAY_SCENARIOS,
        default="normal_night",
        help="development data adapter only; never a planning input",
    )
    run_goal.add_argument("--format", choices=("pretty", "json", "jsonl"), default="pretty")

    answer_input = subparsers.add_parser("answer-input")
    answer_input.add_argument("task_id")
    answer_input.add_argument("request_id")
    answer_input.add_argument("answer")
    answer_input.add_argument("--actor-id", default="cli-user")
    answer_input.add_argument("--role", choices=("elder", "family", "doctor"), default="family")
    answer_input.add_argument("--format", choices=("pretty", "json", "jsonl"), default="pretty")

    inspect_task = subparsers.add_parser("inspect-task")
    inspect_task.add_argument("task_id")
    inspect_task.add_argument("--format", choices=("pretty", "json", "jsonl"), default="pretty")
    inspect_task.add_argument(
        "--decision-trace",
        action="store_true",
        help="show the persisted Plan/Execute/Evaluate causal trace",
    )
    retry_mode = inspect_task.add_mutually_exclusive_group()
    retry_mode.add_argument(
        "--retry",
        action="store_true",
        help="retry the failed task in place before inspection",
    )
    retry_mode.add_argument(
        "--rerun",
        action="store_true",
        help="create and execute a fresh child task from the failed task",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime: Any,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    try:
        if args.command == "run-demo":
            return _run_demo(
                runtime,
                scenario_id=args.scenario,
                output_format=args.format,
                stdout=out,
            )
        if args.command == "run-goal":
            return _run_goal(runtime, args=args, stdout=out)
        if args.command == "answer-input":
            return _answer_input(runtime, args=args, stdout=out)
        return _inspect_task(
            runtime,
            task_id=args.task_id,
            output_format=args.format,
            retry=args.retry,
            rerun=args.rerun,
            decision_trace=args.decision_trace,
            stdout=out,
        )
    except KeyError as exc:
        print(str(exc), file=err)
        return EXIT_NOT_FOUND
    except InvalidTaskTransition as exc:
        print(str(exc), file=err)
        return EXIT_INVALID_STATE


def _run_demo(
    runtime: Any,
    *,
    scenario_id: str,
    output_format: str,
    stdout: TextIO,
) -> int:
    from sleepagent.product_api.diagnostics.http import RadarTaskCreateRequest

    task = runtime.create_task(
        RadarTaskCreateRequest(
            scenario=scenario_id,
            provider_input={"cli": True},
        ),
        idempotency_key=None,
    )
    try:
        runtime.run_task(task.task_id)
    except Exception:
        trace = build_developer_trace(runtime.service, task.task_id)
        _write_trace(trace, output_format=output_format, stdout=stdout)
        return EXIT_TASK_FAILED
    trace = build_developer_trace(runtime.service, task.task_id)
    _write_trace(trace, output_format=output_format, stdout=stdout)
    return _exit_for_trace(trace)


def _inspect_task(
    runtime: Any,
    *,
    task_id: str,
    output_format: str,
    retry: bool,
    rerun: bool,
    decision_trace: bool,
    stdout: TextIO,
) -> int:
    selected_task_id = task_id
    task = runtime.service.get_task(task_id)
    if retry:
        runtime.service.retry_failed_task(task_id)
        try:
            runtime.run_task(task_id)
        except Exception:
            trace = build_developer_trace(runtime.service, task_id)
            _write_trace(trace, output_format=output_format, stdout=stdout)
            return EXIT_TASK_FAILED
    elif rerun:
        rerun_task = runtime.service.rerun_failed_task(task_id)
        selected_task_id = rerun_task.task_id
        try:
            runtime.run_task(selected_task_id)
        except Exception:
            trace = build_developer_trace(runtime.service, selected_task_id)
            _write_trace(trace, output_format=output_format, stdout=stdout)
            return EXIT_TASK_FAILED
    trace = build_developer_trace(runtime.service, selected_task_id)
    if decision_trace and not trace.get("history"):
        trace["decision_trace_note"] = (
            "canonical Product Episode decisions are recorded in Agent/tool events"
        )
    _write_trace(trace, output_format=output_format, stdout=stdout)
    return _exit_for_trace(trace)


def _run_goal(runtime: Any, *, args: Any, stdout: TextIO) -> int:
    from sleepagent.product_api.diagnostics.http import RadarTaskCreateRequest

    payload = RadarTaskCreateRequest(
        goal_type=args.goal_type,
        target_date=args.date,
        range_start=args.start,
        range_end=args.end,
        question=args.question,
        source_artifact_id=args.source_artifact,
        source_date=args.source_date,
        focus=args.focus,
        role=args.role,
        actor_id=args.actor_id,
        scenario=args.scenario,
        provider_input={"cli": True},
    )
    task = runtime.create_task(payload, idempotency_key=None)
    runtime.run_task(task.task_id)
    trace = build_developer_trace(runtime.service, task.task_id)
    _write_trace(trace, output_format=args.format, stdout=stdout)
    return _exit_for_trace(trace)


def _answer_input(runtime: Any, *, args: Any, stdout: TextIO) -> int:
    task = runtime.service.get_task(args.task_id)
    request = runtime.store.get_user_input_request(args.request_id)
    if request.task_id != task.task_id:
        raise InvalidTaskTransition("user input request belongs to another task")
    runtime.store.save_user_input_response(
        UserInputResponse(
            response_id=f"response:{request.request_id}",
            request_id=request.request_id,
            task_id=task.task_id,
            answer=args.answer,
            answered_by_user_id=args.actor_id,
            answered_by_role=args.role,
        )
    )
    runtime.resume_product_after_user_input(
        task.task_id,
        request=request,
        answer=args.answer,
    )
    trace = build_developer_trace(runtime.service, task.task_id)
    _write_trace(trace, output_format=args.format, stdout=stdout)
    return _exit_for_trace(trace)


def build_demo_payload(scenario_id: str) -> dict[str, object]:
    """Keep the deterministic provider fixture available for adapter tests."""

    from sleepagent.simulation.replay import get_replay_scenario

    scenario = get_replay_scenario(scenario_id)
    provider = ReplayRadarProvider(scenario=scenario_id)
    device = provider.list_devices()[0]
    snapshots = provider.pull_snapshots(device.radar_device_id)
    night_report = provider.pull_night_report(device.radar_device_id)
    health = provider.health_check()
    return {
        "scenario": scenario.model_dump(mode="json"),
        "provider_health": health.model_dump(mode="json"),
        "canonical": {
            "device": device.model_dump(mode="json"),
            "snapshots": [snapshot.model_dump(mode="json") for snapshot in snapshots],
            "night_report": night_report.model_dump(mode="json") if night_report else None,
        },
        "expected": scenario.expected.model_dump(mode="json"),
    }


def _write_trace(trace: dict[str, Any], *, output_format: str, stdout: TextIO) -> None:
    if output_format == "json":
        print(json.dumps(trace, ensure_ascii=False, indent=2, sort_keys=True), file=stdout)
        return
    if output_format == "jsonl":
        task = trace["task"]
        print(
            json.dumps(
                {
                    "type": "trace",
                    "schema_version": trace["schema_version"],
                    "task_id": task["task_id"],
                    "trace_id": task["trace_id"],
                    "status": task["status"],
                },
                ensure_ascii=False,
            ),
            file=stdout,
        )
        for section in (
            "events",
            "claims",
            "llm",
            "questionnaires",
            "memory_candidates",
            "confirmations",
            "artifacts",
        ):
            for item in trace[section]:
                print(
                    json.dumps({"type": section.rstrip("s"), "payload": item}, ensure_ascii=False),
                    file=stdout,
                )
        if trace.get("alert_decision"):
            print(
                json.dumps(
                    {
                        "type": "alert_decision",
                        "payload": trace["alert_decision"],
                    },
                    ensure_ascii=False,
                ),
                file=stdout,
            )
        return
    _write_pretty(trace, stdout=stdout)


def _write_pretty(trace: dict[str, Any], *, stdout: TextIO) -> None:
    task = trace["task"]
    print(f"Task: {task['task_id']}", file=stdout)
    print(f"Trace: {task['trace_id']}", file=stdout)
    print(f"Scenario: {task['scenario']}  Status: {task['status']}", file=stdout)
    if task["parent_task_id"]:
        print(f"Rerun of: {task['parent_task_id']}", file=stdout)

    node_status = task.get("node_status") or {}
    if node_status:
        print("\nNode progress", file=stdout)
        for node, status in node_status.items():
            print(f"  {status:>24}  {node}", file=stdout)
    alert = trace.get("alert_decision", {})
    print("\nAlert decision", file=stdout)
    if alert:
        print(
            "  risk="
            f"{alert.get('risk_level', '-')} "
            "automatic="
            f"{','.join(alert.get('automatic_actions', [])) or '-'} "
            "candidates="
            f"{','.join(alert.get('candidate_actions', [])) or '-'}",
            file=stdout,
        )
    else:
        print("  -", file=stdout)

    labels = (
        ("claims", "Claims", "claim_id", "summary"),
        ("llm", "LLM / fallback", "artifact_id", "generation_mode"),
        ("questionnaires", "Questionnaires", "question_id", "summary"),
        ("memory_candidates", "Memory candidates", "candidate_id", "memory_type"),
        ("confirmations", "Human confirmations", "confirmation_id", "action_type"),
        ("artifacts", "Artifact versions", "artifact_version_id", "artifact_type"),
    )
    for key, title, id_key, summary_key in labels:
        print(f"\n{title}", file=stdout)
        if not trace[key]:
            print("  -", file=stdout)
            continue
        for item in trace[key]:
            refs = item.get("evidence_refs") or item.get("source_refs") or []
            suffix = f" refs={','.join(refs)}" if refs else ""
            print(f"  {item[id_key]}  {item.get(summary_key) or '-'}{suffix}", file=stdout)


def _exit_for_trace(trace: dict[str, Any]) -> int:
    return (
        EXIT_TASK_FAILED
        if trace["task"]["status"] == RadarTaskStatus.FAILED.value
        else EXIT_OK
    )


__all__ = [
    "EXIT_INVALID_STATE",
    "EXIT_NOT_FOUND",
    "EXIT_OK",
    "EXIT_TASK_FAILED",
    "RADAR_AGENT_CLI_COMMANDS",
    "build_demo_payload",
    "build_parser",
    "main",
]
