import subprocess
import sys
from pathlib import Path

from scripts.run_stage10_shhs_demo import (
    build_shhs_data_status,
    build_stage10_demo_commands,
    run_api_smoke,
)


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self.payload


class FakeHTTPClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.closed = False

    def get(self, url: str, *, params: dict | None = None) -> FakeResponse:
        self.calls.append({"method": "GET", "url": url, "params": params})
        if url.endswith("/health"):
            return FakeResponse({"status": "ok", "project": "SleepAgent"})
        if url.endswith("/mock-analysis"):
            return FakeResponse(
                {
                    "risk_level": "low",
                    "respiratory_summary": {"ahi": 2.5},
                }
            )
        if url.endswith("/mock-report"):
            return FakeResponse({"report_id": "report-1", "summary": {}})
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url: str, *, json: dict) -> FakeResponse:
        self.calls.append({"method": "POST", "url": url, "json": json})
        if url.endswith("/tasks"):
            return FakeResponse(
                {
                    "id": "task-1",
                    "status": "awaiting_confirmation",
                    "recordId": json["recordId"],
                    "events": [{"type": "task_created"}],
                }
            )
        if url.endswith("/agent/orchestrate"):
            return FakeResponse(
                {
                    "orchestration_mode": "linear",
                    "steps": [{"step_name": "sleep_analysis"}],
                }
            )
        if url.endswith("/stage9/mock-context"):
            return FakeResponse(
                {
                    "local_store_dir": "/tmp/sleepagent_stage10_demo",
                    "alert_event": None,
                }
            )
        raise AssertionError(f"unexpected POST {url}")

    def close(self) -> None:
        self.closed = True


def _write_minimal_shhs_paths(root: Path, record_id: str = "shhs1-200001") -> None:
    visit = "shhs1"
    (root / "polysomnography" / "edfs" / visit).mkdir(parents=True)
    (root / "polysomnography" / "annotations-events-nsrr" / visit).mkdir(
        parents=True
    )
    (root / "polysomnography" / "annotations-events-profusion" / visit).mkdir(
        parents=True
    )
    (root / "polysomnography" / "edfs" / visit / f"{record_id}.edf").write_bytes(
        b"fake-edf"
    )
    (
        root
        / "polysomnography"
        / "annotations-events-nsrr"
        / visit
        / f"{record_id}-nsrr.xml"
    ).write_text("<root />", encoding="utf-8")
    (
        root
        / "polysomnography"
        / "annotations-events-profusion"
        / visit
        / f"{record_id}-profusion.xml"
    ).write_text("<root />", encoding="utf-8")


def test_build_shhs_data_status_reports_required_files(tmp_path: Path) -> None:
    _write_minimal_shhs_paths(tmp_path)

    status = build_shhs_data_status(shhs_root=tmp_path, record_id="shhs1-200001")

    assert status["ok"] is True
    assert status["record_id"] == "shhs1-200001"
    assert status["exists_by_role"]["edf"] is True
    assert status["exists_by_role"]["nsrr_annotation"] is True
    assert status["missing_required_roles"] == []


def test_build_stage10_demo_commands_include_service_and_shhs_steps(
    tmp_path: Path,
) -> None:
    _write_minimal_shhs_paths(tmp_path)

    commands = build_stage10_demo_commands(
        shhs_root=tmp_path,
        record_id="shhs1-200001",
        output_dir=tmp_path / "out",
        api_base_url="http://127.0.0.1:18000",
        stage9_store_dir="/tmp/sleepagent_stage10_demo",
        frontend_port=18510,
        eeg="EEG",
        eog="EOG(L)",
        emg="EMG",
    )
    command_text = "\n".join(command.command for command in commands)

    assert "uvicorn backend.main:app" in command_text
    assert "Start Next.js frontend" in "\n".join(command.title for command in commands)
    assert "cd frontend && npm run dev" in command_text
    assert "NEXT_PUBLIC_SLEEPAGENT_API_BASE_URL=http://127.0.0.1:18000" in "\n".join(
        command.note or "" for command in commands
    )
    assert "NEXT_PUBLIC_SLEEPAGENT_MOCK_MODE=false" in "\n".join(
        command.note or "" for command in commands
    )
    assert "streamlit run frontend/app.py" not in command_text
    assert "scripts/run_stage10_shhs_demo.py --api-smoke" in command_text
    assert "scripts/summarize_shhs_sample.py" in command_text
    assert "scripts/run_yasa_sleep_staging_sample.py" in command_text
    assert "scripts/evaluate_yasa_staging_against_shhs_xml.py" in command_text
    assert str(tmp_path / "polysomnography" / "edfs" / "shhs1") in command_text


def test_run_api_smoke_calls_expected_endpoints() -> None:
    client = FakeHTTPClient()

    result = run_api_smoke(
        api_base_url="http://127.0.0.1:18000/",
        record_id="demo-record",
        subject_id="demo-subject",
        timeout_seconds=3.0,
        http_client=client,
    )

    assert client.closed is False
    assert [call["method"] for call in client.calls] == [
        "GET",
        "POST",
        "GET",
        "GET",
        "POST",
        "POST",
    ]
    assert [check["endpoint"] for check in result["checks"]] == [
        "/health",
        "/tasks",
        "/mock-analysis",
        "/mock-report",
        "/agent/orchestrate",
        "/stage9/mock-context",
    ]
    assert result["checks"][1]["task_status"] == "awaiting_confirmation"
    assert result["checks"][1]["record_id"] == "demo-record"
    assert result["checks"][2]["risk_level"] == "low"
    assert result["checks"][4]["step_names"] == ["sleep_analysis"]
    assert result["checks"][5]["alert_created"] is False


def test_stage10_demo_script_help_is_import_safe() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/run_stage10_shhs_demo.py", "--help"],
        cwd=".",
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Stage 10" in completed.stdout
    assert "--api-smoke" in completed.stdout
