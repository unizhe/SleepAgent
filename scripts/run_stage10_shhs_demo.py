from __future__ import annotations

import argparse
import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import httpx

from sleepagent.preprocessing.shhs_paths import (
    SHHS_ROOT_ENV_VAR,
    SHHSPathError,
    build_shhs_record_paths,
)


DEFAULT_API_BASE_URL = os.getenv(
    "SLEEPAGENT_API_BASE_URL",
    "http://127.0.0.1:18000",
)
DEFAULT_FRONTEND_PORT = 18510
DEFAULT_OUTPUT_DIR = "../data/processed/sleepagent/stage10_demo"
DEFAULT_SHHS_ROOT = os.getenv(SHHS_ROOT_ENV_VAR, "../data/raw/shhs_sample")
DEFAULT_STAGE9_STORE_DIR = "/tmp/sleepagent_stage10_demo"


@dataclass(frozen=True)
class DemoCommand:
    title: str
    command: str
    note: str | None = None

    def to_json_dict(self) -> dict[str, str | None]:
        return {
            "title": self.title,
            "command": self.command,
            "note": self.note,
        }


def build_shhs_data_status(
    *,
    shhs_root: str | Path,
    record_id: str,
) -> dict[str, Any]:
    root_path = Path(shhs_root).expanduser().resolve()
    try:
        record_paths = build_shhs_record_paths(root_path, record_id)
    except SHHSPathError as exc:
        return {
            "ok": False,
            "root": str(root_path),
            "record_id": record_id,
            "error": str(exc),
            "paths": {},
            "exists_by_role": {},
            "missing_required_roles": ["edf", "nsrr_annotation"],
        }

    exists_by_role = {
        role: path.exists()
        for role, path in record_paths.paths_by_role.items()
    }
    return {
        "ok": not record_paths.missing_roles(("edf", "nsrr_annotation")),
        "root": str(root_path),
        "record_id": record_paths.record_id,
        "visit": record_paths.visit,
        "paths": {
            role: str(path)
            for role, path in record_paths.paths_by_role.items()
        },
        "exists_by_role": exists_by_role,
        "missing_required_roles": record_paths.missing_roles(
            ("edf", "nsrr_annotation")
        ),
    }


def build_stage10_demo_commands(
    *,
    shhs_root: str | Path,
    record_id: str,
    output_dir: str | Path,
    api_base_url: str,
    stage9_store_dir: str | Path,
    frontend_port: int,
    eeg: str,
    eog: str | None,
    emg: str | None,
) -> list[DemoCommand]:
    data_status = build_shhs_data_status(shhs_root=shhs_root, record_id=record_id)
    root_path = Path(data_status["root"])
    output_path = Path(output_dir).expanduser().resolve()
    yasa_summary_path = output_path / f"{data_status['record_id']}_yasa_summary.json"
    yasa_eval_path = output_path / f"{data_status['record_id']}_yasa_vs_shhs_eval.json"

    edf_path = data_status["paths"].get("edf", "<local-edf-path>")
    nsrr_xml_path = data_status["paths"].get(
        "nsrr_annotation",
        "<local-nsrr-xml-path>",
    )

    commands = [
        DemoCommand(
            title="Install base package",
            command="python -m pip install -e .",
            note="Run once from the sleepagent/ project root.",
        ),
        DemoCommand(
            title="Export local SHHS root",
            command=f"export {SHHS_ROOT_ENV_VAR}={_quote(root_path)}",
            note="This directory must stay local and outside Git.",
        ),
        DemoCommand(
            title="Start FastAPI backend",
            command=_env_command(
                {"SLEEPAGENT_DATA_STORE_DIR": str(stage9_store_dir)},
                [
                    "uvicorn",
                    "backend.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    _port_from_base_url(api_base_url),
                ],
            ),
            note="Keep this running in terminal 1.",
        ),
        DemoCommand(
            title="Start Next.js frontend",
            command="cd frontend && npm run dev",
            note=(
                f"Open http://127.0.0.1:{frontend_port} in a browser. "
                "The default Next.js config uses "
                f"NEXT_PUBLIC_SLEEPAGENT_API_BASE_URL={api_base_url} and "
                "NEXT_PUBLIC_SLEEPAGENT_MOCK_MODE=false; set frontend/.env.local "
                "only if you override these values."
            ),
        ),
        DemoCommand(
            title="Run API integration smoke",
            command=_join_command(
                [
                    "python",
                    "scripts/run_stage10_shhs_demo.py",
                    "--api-smoke",
                    "--api-base-url",
                    api_base_url,
                    "--record-id",
                    data_status["record_id"],
                ]
            ),
            note=(
                "Exercises /health, /tasks, /mock-analysis, /mock-report, "
                "/agent/orchestrate, and /stage9/mock-context."
            ),
        ),
        DemoCommand(
            title="Run existing Agent smoke script",
            command=_join_command(
                [
                    "python",
                    "scripts/run_agent_orchestration_smoke.py",
                    "--api-base-url",
                    api_base_url,
                    "--record-id",
                    f"{data_status['record_id']}-agent-demo",
                    "--subject-id",
                    "stage10-demo-subject",
                ]
            ),
            note="This is the Stage 8 endpoint smoke script reused in the final demo.",
        ),
        DemoCommand(
            title="Summarize local SHHS XML",
            command=_join_command(
                [
                    "python",
                    "scripts/summarize_shhs_sample.py",
                    "--root",
                    str(root_path),
                    "--record-id",
                    data_status["record_id"],
                ]
            ),
            note="Reads XML metadata and labels only; it does not read EDF signal contents.",
        ),
        DemoCommand(
            title="Run YASA on local SHHS EDF",
            command=_join_command(
                _yasa_command_parts(
                    edf_path=edf_path,
                    eeg=eeg,
                    eog=eog,
                    emg=emg,
                    out_path=yasa_summary_path,
                )
            ),
            note="Reads the EDF. Adjust channel names if the EDF header differs.",
        ),
        DemoCommand(
            title="Evaluate YASA against SHHS XML",
            command=_join_command(
                [
                    "python",
                    "scripts/evaluate_yasa_staging_against_shhs_xml.py",
                    "--yasa-summary",
                    str(yasa_summary_path),
                    "--shhs-xml",
                    nsrr_xml_path,
                    "--out",
                    str(yasa_eval_path),
                ]
            ),
            note="Compares Wake/REM/NREM predictions against local SHHS annotations.",
        ),
    ]
    return commands


def run_api_smoke(
    *,
    api_base_url: str,
    record_id: str,
    subject_id: str,
    timeout_seconds: float,
    http_client: Any | None = None,
) -> dict[str, Any]:
    base_url = api_base_url.rstrip("/")
    client = http_client or httpx.Client(timeout=timeout_seconds)
    should_close = http_client is None
    try:
        checks = [
            _get_check(
                client,
                base_url,
                "/health",
                name="health",
            ),
            _post_check(
                client,
                base_url,
                "/tasks",
                name="task_create",
                payload=_task_create_payload(record_id, subject_id),
            ),
            _get_check(
                client,
                base_url,
                "/mock-analysis",
                name="mock_analysis",
                params=_mock_params(record_id, subject_id),
            ),
            _get_check(
                client,
                base_url,
                "/mock-report",
                name="mock_report",
                params=_mock_params(record_id, subject_id),
            ),
            _post_check(
                client,
                base_url,
                "/agent/orchestrate",
                name="agent_orchestrate",
                payload={
                    **_mock_params(record_id, subject_id),
                    "user_question": "AHI 是什么？",
                    "use_deepseek_report": False,
                    "use_langgraph": False,
                },
            ),
            _post_check(
                client,
                base_url,
                "/stage9/mock-context",
                name="stage9_mock_context",
                payload={
                    **_mock_params(record_id, subject_id),
                    "location": "Shanghai",
                    "max_memory_records": 5,
                    "external_context_seed": 11,
                },
            ),
        ]
    finally:
        if should_close:
            client.close()

    return {
        "api_base_url": base_url,
        "record_id": record_id,
        "subject_id": subject_id,
        "checks": checks,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare the Stage 10 manual demo commands for SleepAgent and, "
            "optionally, run a local backend API smoke."
        )
    )
    parser.add_argument("--shhs-root", default=DEFAULT_SHHS_ROOT)
    parser.add_argument("--record-id", default="shhs1-200001")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    parser.add_argument("--stage9-store-dir", default=DEFAULT_STAGE9_STORE_DIR)
    parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT)
    parser.add_argument("--eeg", default="EEG")
    parser.add_argument("--eog", default="EOG(L)")
    parser.add_argument("--emg", default="EMG")
    parser.add_argument("--subject-id", default="stage10-demo-subject")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the command plan and data status as JSON.",
    )
    parser.add_argument(
        "--api-smoke",
        action="store_true",
        help="Call the local backend demo endpoints instead of printing commands.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.api_smoke:
        try:
            result = run_api_smoke(
                api_base_url=args.api_base_url,
                record_id=args.record_id,
                subject_id=args.subject_id,
                timeout_seconds=args.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            print(f"Stage 10 API smoke failed: {exc}")
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    data_status = build_shhs_data_status(
        shhs_root=args.shhs_root,
        record_id=args.record_id,
    )
    commands = build_stage10_demo_commands(
        shhs_root=args.shhs_root,
        record_id=args.record_id,
        output_dir=args.output_dir,
        api_base_url=args.api_base_url,
        stage9_store_dir=args.stage9_store_dir,
        frontend_port=args.frontend_port,
        eeg=args.eeg,
        eog=_optional_arg(args.eog),
        emg=_optional_arg(args.emg),
    )
    if args.json:
        print(
            json.dumps(
                {
                    "data_status": data_status,
                    "commands": [command.to_json_dict() for command in commands],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print_stage10_demo_guide(data_status=data_status, commands=commands)
    return 0


def print_stage10_demo_guide(
    *,
    data_status: Mapping[str, Any],
    commands: Sequence[DemoCommand],
) -> None:
    print("SleepAgent Stage 10 SHHS manual demo")
    print()
    print("Local SHHS data status")
    print(f"- root: {data_status['root']}")
    print(f"- record_id: {data_status['record_id']}")
    for role, path in data_status["paths"].items():
        exists = data_status["exists_by_role"].get(role, False)
        marker = "OK" if exists else "MISSING"
        print(f"- {role}: {marker} {path}")
    if not data_status["ok"]:
        missing = ", ".join(data_status["missing_required_roles"])
        print(f"- required files missing: {missing}")
    print()
    print("Run these commands from the sleepagent/ project root:")
    for index, command in enumerate(commands, start=1):
        print()
        print(f"{index}. {command.title}")
        if command.note:
            print(f"   {command.note}")
        print(f"   {command.command}")


def _get_check(
    client: Any,
    base_url: str,
    endpoint: str,
    *,
    name: str,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.get(f"{base_url}{endpoint}", params=params)
    return _summarize_response(name=name, method="GET", endpoint=endpoint, response=response)


def _post_check(
    client: Any,
    base_url: str,
    endpoint: str,
    *,
    name: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    response = client.post(f"{base_url}{endpoint}", json=dict(payload))
    return _summarize_response(
        name=name,
        method="POST",
        endpoint=endpoint,
        response=response,
    )


def _summarize_response(
    *,
    name: str,
    method: str,
    endpoint: str,
    response: Any,
) -> dict[str, Any]:
    response.raise_for_status()
    payload = response.json()
    summary: dict[str, Any] = {
        "name": name,
        "method": method,
        "endpoint": endpoint,
        "status_code": response.status_code,
        "top_level_fields": sorted(payload.keys()) if isinstance(payload, dict) else [],
    }
    if isinstance(payload, dict):
        if endpoint == "/mock-analysis":
            summary["risk_level"] = payload.get("risk_level")
            summary["ahi"] = payload.get("respiratory_summary", {}).get("ahi")
        elif endpoint == "/tasks":
            summary["task_id"] = payload.get("id")
            summary["task_status"] = payload.get("status")
            summary["record_id"] = payload.get("recordId")
            summary["event_count"] = len(payload.get("events", []))
        elif endpoint == "/mock-report":
            summary["report_id"] = payload.get("report_id")
        elif endpoint == "/agent/orchestrate":
            summary["orchestration_mode"] = payload.get("orchestration_mode")
            summary["step_names"] = [
                step.get("step_name")
                for step in payload.get("steps", [])
                if isinstance(step, dict)
            ]
        elif endpoint == "/stage9/mock-context":
            summary["local_store_dir"] = payload.get("local_store_dir")
            summary["alert_created"] = payload.get("alert_event") is not None
    return summary


def _mock_params(record_id: str, subject_id: str) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "subject_id": subject_id,
        "duration_hours": 0.5,
        "seed": 42,
        "abnormal_event_rate_per_hour": 6.0,
    }


def _task_create_payload(record_id: str, subject_id: str) -> dict[str, Any]:
    return {
        "title": "Stage 10 demo task",
        "userGoal": "基于本地 SHHS PSG 数据运行 SleepAgent v1 任务。",
        "recordId": record_id,
        "patientId": subject_id,
        "analysisRequest": {
            "record_id": record_id,
            "subject_id": subject_id,
        },
        "useDeepseekReport": False,
    }


def _yasa_command_parts(
    *,
    edf_path: str | Path,
    eeg: str,
    eog: str | None,
    emg: str | None,
    out_path: str | Path,
) -> list[str]:
    parts = [
        "python",
        "scripts/run_yasa_sleep_staging_sample.py",
        "--edf",
        str(edf_path),
        "--eeg",
        eeg,
        "--out",
        str(out_path),
    ]
    if eog:
        parts.extend(["--eog", eog])
    if emg:
        parts.extend(["--emg", emg])
    return parts


def _env_command(env: Mapping[str, str], parts: Sequence[str]) -> str:
    env_parts = [f"{name}={_quote(value)}" for name, value in env.items()]
    return " ".join([*env_parts, *_quoted_parts(parts)])


def _join_command(parts: Sequence[str]) -> str:
    return " ".join(_quoted_parts(parts))


def _quoted_parts(parts: Sequence[str]) -> list[str]:
    return [_quote(part) for part in parts]


def _quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def _optional_arg(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _port_from_base_url(api_base_url: str) -> str:
    parsed = urlparse(api_base_url)
    if parsed.port is not None:
        return str(parsed.port)
    if parsed.scheme == "https":
        return "443"
    return "80"


if __name__ == "__main__":
    raise SystemExit(main())
