from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Sequence, TextIO

from sleepagent.radar_agent.dynamic.contracts import UserInputResponse
from sleepagent.radar_agent.replay import get_replay_scenario, replay_scenario_ids
from sleepagent.radar_agent.runtime import RadarTaskStatus


LLM_API_KEY_ENV = "SLEEPAGENT_RADAR_AGENT_LLM_API_KEY"
DEV_MODE_ENV = "SLEEPAGENT_RADAR_AGENT_DEV_MODE"
REQUIRE_PROVIDER_ID_ENV = "SLEEPAGENT_RADAR_AGENT_REQUIRE_PROVIDER_REQUEST_ID"
DEFAULT_RECEIPT_PATH = "dynamic-agent-acceptance-receipt.json"
EXIT_ACCEPTED = 0
EXIT_UNAVAILABLE = 2
EXIT_REJECTED = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dynamic-agent-acceptance")
    parser.add_argument("--output", default=DEFAULT_RECEIPT_PATH)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    api_key = os.getenv(LLM_API_KEY_ENV, "").strip()
    if not api_key or api_key.startswith("<"):
        print(
            f"UNAVAILABLE: {LLM_API_KEY_ENV} is not configured; no intelligent-mode claim was made.",
            file=out,
        )
        return EXIT_UNAVAILABLE
    if os.getenv(DEV_MODE_ENV, "false").lower() != "true":
        print(
            f"UNAVAILABLE: set {DEV_MODE_ENV}=true to use the staging replay data adapter.",
            file=out,
        )
        return EXIT_UNAVAILABLE

    try:
        receipt = run_real_model_acceptance()
        _assert_release_proof(receipt)
    except Exception as exc:
        print(f"REJECTED: {exc.__class__.__name__}: {exc}", file=err)
        return EXIT_REJECTED

    output = Path(args.output)
    output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"ACCEPTED: sanitized receipt written to {output}", file=out)
    return EXIT_ACCEPTED


def run_real_model_acceptance() -> dict[str, Any]:
    # Importing here keeps ordinary unit tests and degraded deployments independent
    # from the API adapter and prevents this staging runner from becoming a runtime
    # dependency of the Orchestrator.
    from sleepagent.radar_agent.api.http import RadarApiRuntime, RadarTaskCreateRequest

    runtime = RadarApiRuntime(
        sqlite3.connect(":memory:", check_same_thread=False)
    )
    cases = [
        ("baseline", "normal_night", "night_review"),
        ("quality_pivot", "device_or_data_quality_issue", "data_quality_diagnosis"),
        ("trend_pause", "worsening_trend", "trend_comparison"),
        ("conflict_review", "escalate_candidate", "change_explanation"),
        ("doctor_gate", "normal_night", "doctor_material"),
    ]
    task_receipts: list[dict[str, Any]] = []
    baseline_task_id: str | None = None
    baseline_artifact_id: str | None = None
    baseline_date = None

    for label, scenario_id, goal_type in cases:
        scenario = get_replay_scenario(scenario_id)
        night = scenario.deterministic_input.night_report.night_of
        kwargs: dict[str, Any] = {"target_date": night}
        if goal_type in {"trend_comparison", "change_explanation"}:
            kwargs = {"range_start": night - timedelta(days=6), "range_end": night}
        payload = RadarTaskCreateRequest(
            runtime_kind="dynamic_goal",
            goal_type=goal_type,
            role="family",
            actor_id="staging-family-user",
            scenario=scenario_id,
            **kwargs,
        )
        task = runtime.create_task(payload, idempotency_key=f"acceptance:{label}")
        runtime.service.transition_task(task.task_id, RadarTaskStatus.RUNNING)
        result = runtime._dynamic_runner(task).run(task.task_id)
        result = _resume_reviewed_input_if_needed(runtime, task.task_id, result)
        task_receipts.append(_sanitized_task_receipt(runtime, task.task_id, label))
        if label == "baseline":
            baseline_task_id = task.task_id
            baseline_date = night
            versions = runtime.service.list_artifacts(task.task_id)
            baseline_artifact_id = next(
                (
                    item.artifact_id
                    for item in versions
                    if item.artifact_type == "role_report:family"
                ),
                None,
            )

    if not baseline_task_id or not baseline_artifact_id or baseline_date is None:
        raise AssertionError("baseline did not publish an explicit source artifact")
    grounded = RadarTaskCreateRequest(
        runtime_kind="dynamic_goal",
        goal_type="grounded_question",
        question="这份所选日期的报告说明了什么？",
        source_artifact_id=baseline_artifact_id,
        source_date=baseline_date,
        role="family",
        actor_id="staging-family-user",
        scenario="normal_night",
    )
    grounded_task = runtime.create_task(
        grounded, idempotency_key="acceptance:grounded_lookup"
    )
    runtime.service.transition_task(grounded_task.task_id, RadarTaskStatus.RUNNING)
    result = runtime._dynamic_runner(grounded_task).run(grounded_task.task_id)
    _resume_reviewed_input_if_needed(runtime, grounded_task.task_id, result)
    task_receipts.append(
        _sanitized_task_receipt(runtime, grounded_task.task_id, "grounded_lookup")
    )
    return {
        "schema_version": "dynamic-agent-acceptance.v1",
        "provider_request_ids_required": os.getenv(
            REQUIRE_PROVIDER_ID_ENV, "false"
        ).lower()
        == "true",
        "tasks": task_receipts,
    }


def _resume_reviewed_input_if_needed(runtime: Any, task_id: str, result: Any) -> Any:
    task = runtime.service.get_task(task_id)
    if result is not None or task.status != RadarTaskStatus.WAITING_FOR_USER_INPUT:
        return result
    request = runtime.store.get_user_input_request(
        task.pending_user_input_request_id
    )
    if not request.answer_options:
        raise AssertionError("staging pause did not provide reviewed answer options")
    runtime.store.save_user_input_response(
        UserInputResponse(
            response_id=f"acceptance-response:{request.request_id}",
            request_id=request.request_id,
            task_id=task_id,
            answer=request.answer_options[0],
            answered_by_user_id="staging-family-user",
            answered_by_role="family",
        )
    )
    return runtime._dynamic_runner(task).run(task_id)


def _sanitized_task_receipt(runtime: Any, task_id: str, label: str) -> dict[str, Any]:
    task = runtime.service.get_task(task_id)
    plans = runtime.store.list_execution_plans(task_id)
    models = runtime.store.list_model_invocations(task_id)
    agents = runtime.store.list_agent_invocations(task_id)
    messages = runtime.store.list_a2a_messages(task_id)
    receipt = runtime.store.get_completion_receipt(task_id)
    try:
        budget = runtime.store.get_runtime_budget(task_id).model_dump(mode="json")
    except KeyError:
        budget = None
    return {
        "case": label,
        "task_id": task_id,
        "execution_mode": task.execution_mode,
        "completion_status": task.completion_status,
        "plan_graphs": [
            [step.capability for step in plan.steps] for plan in plans
        ],
        "model_invocations": [
            {
                "invocation_id": item.invocation_id,
                "purpose": item.purpose,
                "agent": item.agent.value,
                "provider": item.model_provider,
                "model": item.model_id,
                "provider_request_id": item.provider_request_id,
                "outcome": item.outcome.value,
                "attempt": item.attempt,
            }
            for item in models
        ],
        "agent_invocations": [
            {
                "invocation_id": item.invocation_id,
                "agent": item.agent.value,
                "status": item.status.value,
                "caused_by_a2a_message_id": item.caused_by_a2a_message_id,
            }
            for item in agents
        ],
        "causal_a2a": [
            {
                "message_id": item.message_id,
                "request_type": item.request_type,
                "source_invocation_id": item.source_agent_invocation_id,
                "target_invocation_id": item.target_agent_invocation_id,
                "resolution_status": item.resolution_status,
            }
            for item in messages
            if item.is_real_dynamic_collaboration()
        ],
        "tool_invocations": [
            {
                "tool_name": item.tool_name,
                "tool_schema_version": item.tool_schema_version,
                "outcome": item.outcome.value,
                "confirmation_policy": item.confirmation_policy,
            }
            for item in runtime.store.list_tool_invocations(task_id)
        ],
        "user_input_requests": [
            {
                "question_id": item.question_id,
                "question_type": item.question_type,
                "status": item.status,
            }
            for item in runtime.store.list_user_input_requests(task_id)
        ],
        "fact_snapshot_id": receipt.fact_snapshot_id,
        "runtime_budget": budget,
        "confirmations": [
            {
                "action_type": item.action_type,
                "status": item.status,
                "execution_status": item.execution_status,
            }
            for item in runtime.service.list_confirmations(task_id)
        ],
        "actions_executed": receipt.actions_executed,
    }


def _assert_release_proof(receipt: dict[str, Any]) -> None:
    tasks = receipt["tasks"]
    if len(tasks) != 6:
        raise AssertionError("all six adaptive staging cases must run")
    if any(item["execution_mode"] != "intelligent" for item in tasks):
        raise AssertionError("every staging case must complete in real intelligent mode")
    if any(not item["model_invocations"] for item in tasks):
        raise AssertionError("each case must contain real structured model calls")
    if len({tuple(item["plan_graphs"][0]) for item in tasks}) < 3:
        raise AssertionError("staging goals did not produce materially different graphs")
    conflict = next(item for item in tasks if item["case"] == "conflict_review")
    if not conflict["causal_a2a"]:
        raise AssertionError("conflict case lacks a causally linked target Agent invocation")
    quality = next(item for item in tasks if item["case"] == "quality_pivot")
    trend = next(item for item in tasks if item["case"] == "trend_pause")
    if not quality["user_input_requests"]:
        raise AssertionError("poor-quality case did not request a reviewed observable fact")
    if not trend["user_input_requests"]:
        raise AssertionError("worsening-trend case did not pause for a reviewed fact")
    if len(trend["plan_graphs"]) < 2:
        raise AssertionError(
            "worsening-trend case did not replan after the reviewed answer"
        )
    if any(item["runtime_budget"] is None for item in tasks):
        raise AssertionError("staging receipt is missing persisted runtime budgets")
    if any(not item["tool_invocations"] for item in tasks):
        raise AssertionError("staging receipt is missing audited tool invocations")
    if receipt["provider_request_ids_required"] and any(
        call["provider_request_id"] is None
        for item in tasks
        for call in item["model_invocations"]
        if call["outcome"] == "succeeded"
    ):
        raise AssertionError("configured provider did not return required request IDs")
    serialized = json.dumps(receipt, ensure_ascii=False)
    leaked = [value for value in replay_scenario_ids() if value in serialized]
    if leaked:
        raise AssertionError(f"scenario identifiers leaked into sanitized receipt: {leaked}")


__all__ = [
    "EXIT_ACCEPTED",
    "EXIT_REJECTED",
    "EXIT_UNAVAILABLE",
    "main",
    "run_real_model_acceptance",
]


if __name__ == "__main__":
    raise SystemExit(main())
