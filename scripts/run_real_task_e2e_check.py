from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

from sleepagent.services.analysis_service import YASA_ASSISTIVE_CAVEAT


DEFAULT_API_BASE_URL = os.getenv(
    "SLEEPAGENT_API_BASE_URL",
    "http://127.0.0.1:18000",
)
TERMINAL_EVENT_TYPES = {"task_completed", "error"}
MOCK_MARKERS = (
    "mock-analysis",
    "mock-report",
    "generate_mock_sleep",
    "synthetic mock data",
    "模拟睡眠报告",
    "mock-shhs",
    "mock-subject",
    "analysis_origin: mock",
)


class RealTaskE2ECheckError(RuntimeError):
    def __init__(
        self,
        code: str,
        reason: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "failed",
            "error_code": self.code,
            "reason": self.reason,
            "details": self.details,
        }


def build_task_payload(
    *,
    record_id: str,
    shhs_root: str | None,
    eeg_channel: str,
    eog_channel: str | None,
    emg_channel: str | None,
) -> dict[str, Any]:
    analysis_request: dict[str, Any] = {
        "record_id": record_id,
        "eeg_channel": eeg_channel,
        "eog_channel": eog_channel,
        "emg_channel": emg_channel,
    }
    if shhs_root is not None:
        analysis_request["shhs_root"] = shhs_root
    return {
        "title": f"Real SHHS E2E check: {record_id}",
        "userGoal": "验证 confirm 到真实 SHHS/YASA 分析和 Artifact 持久化链路。",
        "recordId": record_id,
        "analysisRequest": analysis_request,
        "useDeepseekReport": False,
    }


def run_real_task_e2e_check(
    *,
    api_base_url: str,
    record_id: str,
    shhs_root: str | None = None,
    eeg_channel: str = "EEG",
    eog_channel: str | None = "EOG(L)",
    emg_channel: str | None = "EMG",
    timeout_seconds: float = 600.0,
    poll_interval_seconds: float = 1.0,
    http_client: Any | None = None,
) -> dict[str, Any]:
    base_url = api_base_url.rstrip("/")
    client = http_client or httpx.Client(timeout=timeout_seconds)
    try:
        health = _get_json(client, f"{base_url}/health")
        if health.get("status") != "ok":
            raise RealTaskE2ECheckError(
                "health_check_failed",
                f"GET /health returned unexpected payload: {health!r}",
            )

        task = _post_json(
            client,
            f"{base_url}/tasks",
            build_task_payload(
                record_id=record_id,
                shhs_root=shhs_root,
                eeg_channel=eeg_channel,
                eog_channel=eog_channel,
                emg_channel=emg_channel,
            ),
        )
        task_id = str(task.get("id", ""))
        if not task_id:
            raise RealTaskE2ECheckError(
                "invalid_task_response",
                "POST /tasks did not return a task id.",
                details={"response": task},
            )

        _post_json(
            client,
            f"{base_url}/tasks/{task_id}/confirm",
            {"runSynchronously": True},
        )
        events = _poll_terminal_events(
            client,
            f"{base_url}/tasks/{task_id}/events",
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        artifacts = _get_json(client, f"{base_url}/tasks/{task_id}/artifacts")
        if not isinstance(artifacts, list):
            raise RealTaskE2ECheckError(
                "invalid_artifact_response",
                "GET /tasks/{task_id}/artifacts did not return a list.",
                details={"task_id": task_id},
            )

        terminal_type = events[-1].get("type")
        if terminal_type == "error":
            _raise_task_failure(task_id, events[-1], artifacts)

        evidence = _validate_real_execution_evidence(
            task_id=task_id,
            record_id=record_id,
            events=events,
            artifacts=artifacts,
        )
        return {
            "status": "passed",
            "task_id": task_id,
            "record_id": record_id,
            "health": health,
            "terminal_event": terminal_type,
            "event_count": len(events),
            "artifact_count": len(artifacts),
            **evidence,
        }
    finally:
        if http_client is None:
            client.close()


def _poll_terminal_events(
    client: Any,
    url: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        events = _get_json(client, url)
        if not isinstance(events, list):
            raise RealTaskE2ECheckError(
                "invalid_event_response",
                "GET /tasks/{task_id}/events did not return a list.",
            )
        terminal_events = [
            event for event in events if event.get("type") in TERMINAL_EVENT_TYPES
        ]
        if terminal_events:
            terminal = terminal_events[-1]
            terminal_index = events.index(terminal)
            return events[: terminal_index + 1]
        if time.monotonic() >= deadline:
            raise RealTaskE2ECheckError(
                "task_timeout",
                f"Task did not reach completed/failed within {timeout_seconds:.1f}s.",
                details={"event_count": len(events)},
            )
        time.sleep(max(0.0, poll_interval_seconds))


def _raise_task_failure(
    task_id: str,
    error_event: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> None:
    payload = error_event.get("payload") or {}
    error_code = str(payload.get("error_code") or "task_failed")
    message = str(error_event.get("message") or "Task failed without an error message.")
    if error_code == "missing_local_data":
        reason = (
            f"missing_local_data: task {task_id} could not find the required local "
            f"SHHS EDF/XML files. {message}"
        )
    else:
        reason = f"Task {task_id} failed with {error_code}: {message}"
    raise RealTaskE2ECheckError(
        error_code,
        reason,
        details={
            "task_id": task_id,
            "error_event": error_event,
            "artifact_count": len(artifacts),
        },
    )


def _validate_real_execution_evidence(
    *,
    task_id: str,
    record_id: str,
    events: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    serialized_events = json.dumps(events, ensure_ascii=False).lower()
    artifact_content = "\n".join(str(item.get("content", "")) for item in artifacts)
    serialized_artifacts = json.dumps(artifacts, ensure_ascii=False).lower()
    mock_hits = sorted(
        marker
        for marker in MOCK_MARKERS
        if marker.lower() in serialized_events or marker.lower() in serialized_artifacts
    )
    if mock_hits:
        raise RealTaskE2ECheckError(
            "mock_data_detected",
            "Real task evidence contains mock analysis/report markers.",
            details={"task_id": task_id, "mock_markers": mock_hits},
        )

    analysis_calls = [
        event
        for event in events
        if event.get("type") == "tool_called"
        and ((event.get("payload") or {}).get("tool_call") or {}).get("toolName")
        == "AnalysisService.run_analysis"
    ]
    if not analysis_calls:
        raise RealTaskE2ECheckError(
            "analysis_service_not_observed",
            "No task event proves that AnalysisService.run_analysis was called.",
            details={"task_id": task_id},
        )

    staging_findings = [
        event
        for event in events
        if event.get("type") == "finding_created"
        and event.get("stepId") == "sleep_staging"
        and (event.get("payload") or {}).get("status") == "completed"
    ]
    if not staging_findings:
        raise RealTaskE2ECheckError(
            "yasa_execution_not_observed",
            "No completed sleep_staging finding was persisted for the task.",
            details={"task_id": task_id},
        )

    source_paths: dict[str, str] = {}
    source_artifacts: dict[str, str] = {}
    for event in events:
        payload = event.get("payload") or {}
        source_paths.update(payload.get("source_paths") or {})
        source_artifacts.update(payload.get("source_artifacts") or {})
    source_values = [*source_paths.values(), *source_artifacts.values()]

    evidence_artifacts = []
    for artifact in artifacts:
        content = str(artifact.get("content", ""))
        if (
            record_id in content
            and any(value in content for value in source_values)
            and YASA_ASSISTIVE_CAVEAT in content
        ):
            evidence_artifacts.append(artifact)

    missing_artifact_evidence: list[str] = []
    if record_id not in artifact_content:
        missing_artifact_evidence.append("record_id")
    if not source_values or not any(value in artifact_content for value in source_values):
        missing_artifact_evidence.append("SHHS source path or source artifact")
    if YASA_ASSISTIVE_CAVEAT not in artifact_content:
        missing_artifact_evidence.append("YASA caveat")
    if not missing_artifact_evidence and not evidence_artifacts:
        missing_artifact_evidence.append(
            "one Artifact containing record_id, source evidence, and YASA caveat together"
        )
    if missing_artifact_evidence:
        raise RealTaskE2ECheckError(
            "artifact_evidence_incomplete",
            "Artifact content is missing required real-analysis evidence: "
            + ", ".join(missing_artifact_evidence),
            details={
                "task_id": task_id,
                "source_paths": source_paths,
                "source_artifacts": source_artifacts,
            },
        )

    return {
        "analysis_service_call_observed": True,
        "yasa_staging_observed": True,
        "source_paths": source_paths,
        "source_artifacts": source_artifacts,
        "evidence_artifact_ids": [
            artifact.get("id") for artifact in evidence_artifacts
        ],
        "yasa_caveat": YASA_ASSISTIVE_CAVEAT,
    }


def _get_json(client: Any, url: str) -> Any:
    response = client.get(url)
    response.raise_for_status()
    return response.json()


def _post_json(client: Any, url: str, payload: dict[str, Any]) -> Any:
    response = client.post(url, json=payload)
    response.raise_for_status()
    return response.json()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the real POST /tasks/{task_id}/confirm SHHS/YASA chain."
    )
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    parser.add_argument("--record-id", default="shhs1-200001")
    parser.add_argument(
        "--shhs-root",
        default=None,
        help="Optional server-visible SHHS root; otherwise the server environment is used.",
    )
    parser.add_argument("--eeg-channel", default="EEG")
    parser.add_argument("--eog-channel", default="EOG(L)")
    parser.add_argument("--emg-channel", default="EMG")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        result = run_real_task_e2e_check(
            api_base_url=args.api_base_url,
            record_id=args.record_id,
            shhs_root=args.shhs_root,
            eeg_channel=args.eeg_channel,
            eog_channel=args.eog_channel,
            emg_channel=args.emg_channel,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
    except RealTaskE2ECheckError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 3 if exc.code == "missing_local_data" else 2
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "api_request_failed",
                    "reason": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
