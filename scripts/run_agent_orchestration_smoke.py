from __future__ import annotations

import argparse
import json
import os
from typing import Any

import httpx
from pydantic import ValidationError

from sleepagent.schemas import SleepAgentOrchestrationResult


DEFAULT_AGENT_API_BASE_URL = os.getenv(
    "SLEEPAGENT_API_BASE_URL",
    "http://127.0.0.1:18000",
)


def build_agent_orchestration_payload(
    *,
    record_id: str,
    subject_id: str,
    duration_hours: float,
    seed: int,
    abnormal_event_rate_per_hour: float,
    user_question: str | None,
    history_summary: str | None,
    use_deepseek_report: bool,
    use_langgraph: bool,
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "subject_id": subject_id,
        "duration_hours": duration_hours,
        "seed": seed,
        "abnormal_event_rate_per_hour": abnormal_event_rate_per_hour,
        "user_question": user_question,
        "dialogue_context": (
            {"history_summary": history_summary} if history_summary else None
        ),
        "use_deepseek_report": use_deepseek_report,
        "use_langgraph": use_langgraph,
    }


def run_agent_orchestration_smoke(
    *,
    api_base_url: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    url = f"{api_base_url.rstrip('/')}/agent/orchestrate"
    client = http_client or httpx.Client(timeout=timeout_seconds)
    try:
        response = client.post(url, json=payload)
        response.raise_for_status()
        result = SleepAgentOrchestrationResult.model_validate(response.json())
    finally:
        if http_client is None:
            client.close()

    report_fields = sorted(result.report.model_dump(mode="json").keys())
    return {
        "endpoint": url,
        "method": "POST",
        "record_id": result.analysis.metadata.record_id,
        "subject_id": result.analysis.metadata.patient.subject_id,
        "risk_level": result.analysis.risk_level.value,
        "orchestration_mode": result.orchestration_mode.value,
        "step_names": [step.step_name.value for step in result.steps],
        "dialogue_present": result.dialogue is not None,
        "dialogue_safety_flags": (
            result.dialogue.safety_flags if result.dialogue is not None else []
        ),
        "report_contract_fields": report_fields,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a POST smoke request against /agent/orchestrate."
    )
    parser.add_argument("--api-base-url", default=DEFAULT_AGENT_API_BASE_URL)
    parser.add_argument("--record-id", default="agent-smoke-record")
    parser.add_argument("--subject-id", default="agent-smoke-subject")
    parser.add_argument("--duration-hours", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=51)
    parser.add_argument("--abnormal-event-rate-per-hour", type=float, default=6.0)
    parser.add_argument("--user-question", default="AHI 是什么？")
    parser.add_argument("--history-summary", default=None)
    parser.add_argument("--use-deepseek-report", action="store_true")
    parser.add_argument("--use-langgraph", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    args = parser.parse_args()

    payload = build_agent_orchestration_payload(
        record_id=args.record_id,
        subject_id=args.subject_id,
        duration_hours=args.duration_hours,
        seed=args.seed,
        abnormal_event_rate_per_hour=args.abnormal_event_rate_per_hour,
        user_question=args.user_question,
        history_summary=args.history_summary,
        use_deepseek_report=args.use_deepseek_report,
        use_langgraph=args.use_langgraph,
    )

    try:
        summary = run_agent_orchestration_smoke(
            api_base_url=args.api_base_url,
            payload=payload,
            timeout_seconds=args.timeout_seconds,
        )
    except httpx.HTTPError as exc:
        print(f"Agent orchestration smoke request failed: {exc}")
        return 1
    except (ValidationError, ValueError, TypeError) as exc:
        print(f"Agent orchestration response failed contract validation: {exc}")
        return 2

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
