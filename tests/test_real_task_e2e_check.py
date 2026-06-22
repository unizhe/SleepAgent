from pathlib import Path

import pytest

from scripts.run_real_task_e2e_check import (
    RealTaskE2ECheckError,
    _validate_real_execution_evidence,
    run_real_task_e2e_check,
)
from sleepagent.models import build_yasa_sleep_staging_result
from sleepagent.services.analysis_service import AnalysisService, YASA_ASSISTIVE_CAVEAT
from sleepagent.services.data_management import SLEEPAGENT_DATA_STORE_DIR_ENV
from sleepagent.services.repository_factory import SLEEPAGENT_REPOSITORY_BACKEND_ENV
from sleepagent.services.task_repository import LocalJsonlTaskRepository
from sleepagent.services.task_service import TaskService
from sleepagent.services.yasa_runner import EDFSignalInfo, YASARunnerResult


def test_real_task_e2e_check_reports_missing_local_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from backend import main as backend_main

    monkeypatch.setenv(SLEEPAGENT_DATA_STORE_DIR_ENV, str(tmp_path / "store"))
    monkeypatch.setenv(SLEEPAGENT_REPOSITORY_BACKEND_ENV, "local")
    client = TestClient(backend_main.app)

    with pytest.raises(RealTaskE2ECheckError) as exc_info:
        run_real_task_e2e_check(
            api_base_url="http://testserver",
            record_id="shhs1-200001",
            shhs_root=str(tmp_path / "missing-shhs"),
            timeout_seconds=2.0,
            poll_interval_seconds=0.0,
            http_client=client,
        )

    error = exc_info.value
    assert error.code == "missing_local_data"
    assert "required local SHHS EDF/XML files" in error.reason
    assert error.details["error_event"]["payload"]["error_code"] == (
        "missing_local_data"
    )
    assert error.details["artifact_count"] == 0


def test_real_task_e2e_check_accepts_real_task_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from backend import main as backend_main

    store_dir = tmp_path / "store"
    shhs_root = _write_minimal_shhs_layout(tmp_path / "shhs")
    monkeypatch.setenv(SLEEPAGENT_DATA_STORE_DIR_ENV, str(store_dir))
    monkeypatch.setenv(SLEEPAGENT_REPOSITORY_BACKEND_ENV, "local")
    service = TaskService(
        repository=LocalJsonlTaskRepository(store_dir),
        analysis_service=AnalysisService(
            edf_inspector=_fake_edf_inspector,
            yasa_runner=_fake_yasa_runner,
        ),
    )
    monkeypatch.setattr(backend_main, "_task_service", lambda: service)

    result = run_real_task_e2e_check(
        api_base_url="http://testserver",
        record_id="shhs1-200001",
        shhs_root=str(shhs_root),
        timeout_seconds=2.0,
        poll_interval_seconds=0.0,
        http_client=TestClient(backend_main.app),
    )

    assert result["status"] == "passed"
    assert result["analysis_service_call_observed"] is True
    assert result["yasa_staging_observed"] is True
    assert result["source_paths"]["edf"].endswith("shhs1-200001.edf")
    assert result["yasa_caveat"] == YASA_ASSISTIVE_CAVEAT
    artifacts = service.list_artifacts(result["task_id"])
    technical = next(item for item in artifacts if item.type == "technical_report")
    assert "analysis_origin: real_shhs_yasa" in technical.content
    assert "record_id: shhs1-200001" in technical.content
    assert str(shhs_root) in technical.content
    assert YASA_ASSISTIVE_CAVEAT in technical.content
    assert "synthetic mock data" not in "\n".join(
        item.content for item in artifacts
    )


def test_real_task_e2e_check_preserves_missing_local_data_from_api() -> None:
    client = _FakeHTTPClient(
        {
            ("GET", "/health"): {"status": "ok"},
            ("POST", "/tasks"): {"id": "task-missing"},
            ("POST", "/tasks/task-missing/confirm"): {"status": "failed"},
            ("GET", "/tasks/task-missing/events"): [
                {
                    "type": "error",
                    "message": "Missing required local SHHS file role(s): edf",
                    "payload": {"error_code": "missing_local_data"},
                }
            ],
            ("GET", "/tasks/task-missing/artifacts"): [],
        }
    )

    with pytest.raises(RealTaskE2ECheckError) as exc_info:
        run_real_task_e2e_check(
            api_base_url="http://testserver",
            record_id="shhs1-200001",
            timeout_seconds=1.0,
            poll_interval_seconds=0.0,
            http_client=client,
        )

    assert exc_info.value.code == "missing_local_data"
    assert "Missing required local SHHS" in exc_info.value.reason
    assert client.calls[-1] == ("GET", "/tasks/task-missing/artifacts")


def test_real_task_e2e_check_rejects_mock_artifact_content() -> None:
    events = [
        {
            "type": "tool_called",
            "payload": {"tool_call": {"toolName": "AnalysisService.run_analysis"}},
        },
        {
            "type": "finding_created",
            "stepId": "sleep_staging",
            "payload": {
                "status": "completed",
                "source_paths": {"edf": "/data/shhs1-200001.edf"},
            },
        },
    ]
    artifacts = [
        {
            "content": (
                "record_id: shhs1-200001\n/data/shhs1-200001.edf\n"
                f"{YASA_ASSISTIVE_CAVEAT}\nsynthetic mock data"
            )
        }
    ]

    with pytest.raises(RealTaskE2ECheckError) as exc_info:
        _validate_real_execution_evidence(
            task_id="task-mock",
            record_id="shhs1-200001",
            events=events,
            artifacts=artifacts,
        )

    assert exc_info.value.code == "mock_data_detected"
    assert "synthetic mock data" in exc_info.value.details["mock_markers"]


def _fake_edf_inspector(edf_path: str | Path) -> EDFSignalInfo:
    return EDFSignalInfo(
        path=str(Path(edf_path).expanduser().resolve()),
        channel_names=["EEG", "EOG(L)", "EMG"],
        sampling_rate_hz=125.0,
        duration_seconds=120.0,
        n_samples=15000,
    )


def _fake_yasa_runner(
    edf_path: str | Path,
    *,
    eeg_name: str,
    eog_name: str | None,
    emg_name: str | None,
) -> YASARunnerResult:
    return YASARunnerResult(
        edf_info=_fake_edf_inspector(edf_path),
        eeg_name=eeg_name,
        eog_name=eog_name,
        emg_name=emg_name,
        staging=build_yasa_sleep_staging_result(
            ["WAKE", "N2", "REM", "N2"],
            recording_duration_seconds=120.0,
        ),
    )


def _write_minimal_shhs_layout(root: Path) -> Path:
    edf_dir = root / "polysomnography" / "edfs" / "shhs1"
    annotation_dir = (
        root / "polysomnography" / "annotations-events-profusion" / "shhs1"
    )
    edf_dir.mkdir(parents=True)
    annotation_dir.mkdir(parents=True)
    (edf_dir / "shhs1-200001.edf").write_bytes(b"fake-edf")
    (annotation_dir / "shhs1-200001-profusion.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<CMPStudyConfig>
  <ScoredEvents>
    <ScoredEvent>
      <Name>Hypopnea</Name>
      <Start>35</Start>
      <Duration>10</Duration>
    </ScoredEvent>
  </ScoredEvents>
</CMPStudyConfig>
""",
        encoding="utf-8",
    )
    return root


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _FakeHTTPClient:
    def __init__(self, responses: dict[tuple[str, str], object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str) -> _FakeResponse:
        return self._response("GET", url)

    def post(self, url: str, *, json: dict) -> _FakeResponse:
        _ = json
        return self._response("POST", url)

    def _response(self, method: str, url: str) -> _FakeResponse:
        path = url.removeprefix("http://testserver")
        self.calls.append((method, path))
        return _FakeResponse(self.responses[(method, path)])
