from pathlib import Path

import pytest
from fastapi import HTTPException

from backend import main as backend_main
from sleepagent.agents import SleepAnalysisAgent
from sleepagent.models import build_yasa_sleep_staging_result
from sleepagent.preprocessing import generate_mock_sleep_analysis
from sleepagent.schemas import (
    AnalysisMode,
    AnalysisNodeStatus,
    AnalysisRequest,
    RiskLevel,
    SleepAgentOrchestrationRequest,
)
from sleepagent.services import AnalysisService, AnalysisServiceError
from sleepagent.services.analysis_service import (
    RESPIRATORY_DEMO_ONLY_CAVEAT,
    RESPIRATORY_MODEL_UNVALIDATED_CAVEAT,
    YASA_ASSISTIVE_CAVEAT,
)
from sleepagent.services.yasa_runner import EDFSignalInfo, YASARunnerResult


def test_analysis_service_runs_real_shhs_yasa_minimal_loop(tmp_path: Path) -> None:
    root = _write_minimal_shhs_layout(tmp_path)
    service = AnalysisService(
        edf_inspector=_fake_edf_inspector,
        yasa_runner=_fake_yasa_runner,
    )

    result = service.run_analysis(
        AnalysisRequest(
            record_id="shhs1-200001",
            shhs_root=str(root),
            use_respiratory_model=True,
            respiratory_checkpoint_path="../data/checkpoints/demo.pt",
            allow_demo_respiratory_model=True,
        )
    )

    assert result.record_status == AnalysisNodeStatus.COMPLETED
    assert result.record_result.status == AnalysisNodeStatus.COMPLETED
    assert result.quality_result.status == AnalysisNodeStatus.COMPLETED
    assert result.sleep_staging_result.status == AnalysisNodeStatus.COMPLETED
    assert result.respiratory_result.status == AnalysisNodeStatus.SKIPPED_WITH_WARNING
    assert result.risk_result.status == AnalysisNodeStatus.COMPLETED
    assert result.sleep_analysis_result is not None
    assert result.sleep_analysis_result.metadata.source_dataset == "shhs"
    assert result.sleep_analysis_result.metadata.patient.subject_id == "200001"
    assert result.sleep_analysis_result.sleep_summary.total_sleep_time_minutes == pytest.approx(
        1.5
    )
    assert result.sleep_analysis_result.respiratory_summary.hypopnea_count == 1
    assert result.sleep_analysis_result.respiratory_summary.suspected_apnea_count == 1
    assert result.sleep_analysis_result.risk_level == RiskLevel.HIGH
    assert (
        result.respiratory_result.payload["respiratory_model_status"]
        == "not_validated_for_risk_conclusion"
    )
    assert (
        result.risk_result.payload["respiratory_model_status"]
        == "not_validated_for_risk_conclusion"
    )
    assert YASA_ASSISTIVE_CAVEAT in result.caveats
    assert RESPIRATORY_MODEL_UNVALIDATED_CAVEAT in result.caveats
    assert RESPIRATORY_DEMO_ONLY_CAVEAT in result.caveats
    assert result.respiratory_result.source_artifacts == {
        "respiratory_checkpoint": "../data/checkpoints/demo.pt"
    }
    assert "not used as negative evidence" in " ".join(
        result.risk_result.payload["evidence_chain"]
    )


def test_analysis_service_returns_structured_failure_for_missing_local_data(
    tmp_path: Path,
) -> None:
    service = AnalysisService(
        edf_inspector=_fake_edf_inspector,
        yasa_runner=_fake_yasa_runner,
    )

    result = service.run_analysis(
        AnalysisRequest(record_id="shhs1-200001", shhs_root=str(tmp_path))
    )

    assert result.record_status == AnalysisNodeStatus.FAILED
    assert result.record_result.status == AnalysisNodeStatus.FAILED
    assert result.record_result.payload["missing_required_roles"] == [
        "edf",
        "nsrr_or_profusion_xml",
    ]
    assert result.sleep_analysis_result is None
    assert result.sleep_staging_result.status == AnalysisNodeStatus.SKIPPED

    with pytest.raises(AnalysisServiceError, match="Missing required local SHHS"):
        service.run_sleep_analysis(
            AnalysisRequest(record_id="shhs1-200001", shhs_root=str(tmp_path))
        )


def test_sleep_analysis_agent_real_mode_calls_analysis_service() -> None:
    class FakeAnalysisService:
        def __init__(self) -> None:
            self.requests: list[AnalysisRequest] = []

        def run_sleep_analysis(self, request: AnalysisRequest):
            self.requests.append(request)
            return generate_mock_sleep_analysis(
                record_id=request.record_id,
                subject_id=request.subject_id or "fake-subject",
                duration_hours=0.5,
            )

    fake_service = FakeAnalysisService()
    agent = SleepAnalysisAgent(analysis_service=fake_service)  # type: ignore[arg-type]

    analysis = agent.run(
        SleepAgentOrchestrationRequest(
            analysis_mode=AnalysisMode.REAL,
            record_id="shhs1-200001",
            shhs_root="/local/shhs",
            eeg_channel="EEG(sec)",
            use_respiratory_model=True,
            allow_demo_respiratory_model=True,
        )
    )

    assert analysis is not None
    assert fake_service.requests == [
        AnalysisRequest(
            record_id="shhs1-200001",
            subject_id="mock-subject-0001",
            shhs_root="/local/shhs",
            eeg_channel="EEG(sec)",
            use_respiratory_model=True,
            allow_demo_respiratory_model=True,
        )
    ]


def test_backend_analysis_run_route_uses_analysis_service(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[AnalysisRequest] = []
    expected_result = _analysis_service_result_from_root(tmp_path)

    class FakeAnalysisService:
        def run_analysis(self, request: AnalysisRequest):
            calls.append(request)
            return expected_result

    monkeypatch.setattr(backend_main, "AnalysisService", FakeAnalysisService)

    request = AnalysisRequest(record_id="shhs1-200001", shhs_root="/local/shhs")
    result = backend_main.run_analysis(request)

    assert result is expected_result
    assert calls == [request]


def test_backend_orchestration_maps_real_analysis_errors(monkeypatch) -> None:
    def raise_analysis_error(request):
        raise AnalysisServiceError("Missing required local SHHS file role(s): edf")

    monkeypatch.setattr(
        backend_main,
        "run_sleep_agent_orchestration",
        raise_analysis_error,
    )

    with pytest.raises(HTTPException) as exc_info:
        backend_main._run_agent_orchestration_request(
            backend_main.SleepAgentEndpointRequest(analysis_mode=AnalysisMode.REAL)
        )

    assert exc_info.value.status_code == 400
    assert "Missing required local SHHS" in exc_info.value.detail


def _analysis_service_result_from_root(root: Path):
    service = AnalysisService(
        edf_inspector=_fake_edf_inspector,
        yasa_runner=_fake_yasa_runner,
    )
    root = _write_minimal_shhs_layout(root)
    return service.run_analysis(
        AnalysisRequest(record_id="shhs1-200001", shhs_root=str(root))
    )


def _fake_edf_inspector(edf_path: str | Path) -> EDFSignalInfo:
    return EDFSignalInfo(
        path=str(Path(edf_path).expanduser().resolve()),
        channel_names=["EEG", "EOG(L)", "EMG", "NEW AIR"],
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
    edf_info = _fake_edf_inspector(edf_path)
    staging = build_yasa_sleep_staging_result(
        ["WAKE", "N2", "REM", "N2"],
        recording_duration_seconds=120.0,
    )
    return YASARunnerResult(
        edf_info=edf_info,
        eeg_name=eeg_name,
        eog_name=eog_name,
        emg_name=emg_name,
        staging=staging,
    )


def _write_minimal_shhs_layout(root: Path) -> Path:
    edf_dir = root / "polysomnography" / "edfs" / "shhs1"
    nsrr_dir = root / "polysomnography" / "annotations-events-nsrr" / "shhs1"
    profusion_dir = (
        root / "polysomnography" / "annotations-events-profusion" / "shhs1"
    )
    edf_dir.mkdir(parents=True, exist_ok=True)
    nsrr_dir.mkdir(parents=True, exist_ok=True)
    profusion_dir.mkdir(parents=True, exist_ok=True)
    (edf_dir / "shhs1-200001.edf").write_bytes(b"fake-edf")
    (nsrr_dir / "shhs1-200001-nsrr.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<PSGAnnotation>
  <EpochLength>30</EpochLength>
</PSGAnnotation>
""",
        encoding="utf-8",
    )
    (profusion_dir / "shhs1-200001-profusion.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<CMPStudyConfig>
  <ScoredEvents>
    <ScoredEvent>
      <Name>Hypopnea</Name>
      <Start>35</Start>
      <Duration>10</Duration>
    </ScoredEvent>
    <ScoredEvent>
      <Name>Obstructive apnea</Name>
      <Start>70</Start>
      <Duration>20</Duration>
    </ScoredEvent>
  </ScoredEvents>
</CMPStudyConfig>
""",
        encoding="utf-8",
    )
    return root
