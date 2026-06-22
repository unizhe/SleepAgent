from pathlib import Path
import asyncio

from fastapi import HTTPException

from backend import main as backend_main
from sleepagent.agents import task_graph as task_graph_module
from sleepagent.agents.task_graph import build_sleep_agent_task_langgraph
from sleepagent.models import build_yasa_sleep_staging_result
from sleepagent.schemas import AnalysisRequest, TaskConfirmRequest, TaskCreateRequest
from sleepagent.services import SLEEPAGENT_DATA_STORE_DIR_ENV
from sleepagent.services.analysis_service import AnalysisService
from sleepagent.services.task_repository import (
    LocalJsonlTaskRepository,
    TASK_EVENTS_FILENAME,
    TASK_RECORDS_FILENAME,
)
from sleepagent.services.task_service import TaskService
from sleepagent.services.yasa_runner import EDFSignalInfo, YASARunnerResult


def test_task_api_creates_planned_task_and_recovers_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SLEEPAGENT_DATA_STORE_DIR_ENV, str(tmp_path))

    task = backend_main.create_task(
        TaskCreateRequest(
            title="Phase 2 test task",
            userGoal="运行真实任务状态图。",
            recordId="shhs1-200001",
            patientId="subject-200001",
        )
    )

    assert task.status == "awaiting_confirmation"
    assert task.record_id == "shhs1-200001"
    assert task.patient_id == "subject-200001"
    assert [event.type for event in task.events] == [
        "task_created",
        "plan_created",
    ]
    assert [step.id for step in task.plan] == [
        "load_record",
        "quality_check",
        "sleep_staging",
        "respiratory_detection",
        "risk_assessment",
        "medical_rag",
        "report_generation",
        "care_plan",
        "chat_context",
    ]
    assert tmp_path.joinpath(TASK_RECORDS_FILENAME).exists()
    assert tmp_path.joinpath(TASK_EVENTS_FILENAME).exists()

    recovered = backend_main.get_task(task.id)
    events = backend_main.get_task_events(task.id)

    assert recovered.id == task.id
    assert [event.type for event in events] == [
        "task_created",
        "plan_created",
    ]


def test_task_api_confirm_runs_graph_events_and_artifacts(tmp_path, monkeypatch) -> None:
    store_dir = tmp_path / "store"
    shhs_root = _write_minimal_shhs_layout(tmp_path / "shhs")
    service = TaskService(
        repository=LocalJsonlTaskRepository(store_dir),
        analysis_service=_FakeAnalysisService(),
    )
    monkeypatch.setattr(backend_main, "_task_service", lambda: service)

    task = backend_main.create_task(
        TaskCreateRequest(
            recordId="shhs1-200001",
            analysisRequest=AnalysisRequest(
                record_id="shhs1-200001",
                shhs_root=str(shhs_root),
                use_respiratory_model=True,
                allow_demo_respiratory_model=True,
            ),
        )
    )

    result = backend_main.confirm_task(task.id, TaskConfirmRequest())

    assert result.status == "completed"
    assert result.plan[-1].status == "completed"
    event_types = [event.type for event in result.events]
    assert "step_started" in event_types
    assert "tool_called" in event_types
    assert "finding_created" in event_types
    assert "artifact_created" in event_types
    assert event_types[-1] == "task_completed"
    assert {
        artifact.type for artifact in result.artifacts
    } >= {
        "risk_summary",
        "evidence_chain",
        "elder_report",
        "family_report",
        "doctor_report",
        "technical_report",
        "care_plan",
    }
    risk_artifact = next(
        artifact for artifact in result.artifacts if artifact.type == "risk_summary"
    )
    assert "呼吸模型状态：not_validated_for_risk_conclusion" in risk_artifact.content
    assert result.next_actions[0].requires_confirmation is True

    recovered = backend_main.get_task(task.id)
    persisted_events = backend_main.get_task_events(task.id)
    persisted_artifacts = backend_main.get_task_artifacts(task.id)

    assert recovered.status == "completed"
    assert persisted_events[-1].type == "task_completed"
    assert len(persisted_artifacts) == len(result.artifacts)


def test_task_api_confirm_persists_structured_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SLEEPAGENT_DATA_STORE_DIR_ENV, str(tmp_path))

    task = backend_main.create_task(
        TaskCreateRequest(
            analysisRequest=AnalysisRequest(
                record_id="shhs1-200001",
                shhs_root=str(tmp_path / "missing-shhs"),
            )
        )
    )

    result = backend_main.confirm_task(task.id, TaskConfirmRequest())

    assert result.status == "failed"
    assert result.errors
    assert result.events[-1].type == "error"
    assert result.events[-1].payload["error_code"] == "missing_local_data"

    recovered = backend_main.get_task(task.id)
    assert recovered.status == "failed"
    assert recovered.events[-1].type == "error"


def test_phase2_task_langgraph_runs_with_fake_state_graph(tmp_path, monkeypatch) -> None:
    shhs_root = _write_minimal_shhs_layout(tmp_path / "shhs")
    service = TaskService(
        repository=LocalJsonlTaskRepository(tmp_path / "store"),
        analysis_service=_FakeAnalysisService(),
    )
    task = service.create_task(
        TaskCreateRequest(
            analysisRequest=AnalysisRequest(
                record_id="shhs1-200001",
                shhs_root=str(shhs_root),
            )
        )
    )
    monkeypatch.setattr(
        task_graph_module,
        "_load_langgraph_symbols",
        _fake_langgraph_symbols,
    )

    app = build_sleep_agent_task_langgraph(
        analysis_service=_FakeAnalysisService(),
    )
    final_state = app.invoke(
        {
            "task": task,
            "events": list(task.events),
            "artifacts": [],
            "errors": [],
        }
    )

    assert final_state["task"].status == "completed"
    assert final_state["task"].events[-1].type == "task_completed"
    assert final_state["task"].artifacts


def test_task_event_sse_stream_replays_history_and_stops(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(SLEEPAGENT_DATA_STORE_DIR_ENV, str(tmp_path))
    task = backend_main.create_task(
        TaskCreateRequest(recordId="shhs1-200001", patientId="subject-200001")
    )

    chunks = asyncio.run(
        _collect_sse_chunks(
            backend_main._iter_task_event_sse(
                task.id,
                poll_interval_seconds=0.0,
            )
        )
    )

    assert len(chunks) == 2
    assert chunks[0].startswith(f"id: {task.events[0].id}\n")
    assert "event: task_created\n" in chunks[0]
    assert '"stepId"' in chunks[0]
    assert "event: plan_created\n" in chunks[1]


def test_task_api_reports_missing_task(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SLEEPAGENT_DATA_STORE_DIR_ENV, str(tmp_path))

    try:
        backend_main.get_task("not-found")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("Expected HTTPException for missing task.")


async def _collect_sse_chunks(stream) -> list[str]:
    chunks: list[str] = []
    async for chunk in stream:
        chunks.append(chunk)
        if len(chunks) >= 2:
            break
    return chunks


class _FakeAnalysisService:
    def run_analysis(self, request: AnalysisRequest):
        service = AnalysisService(
            edf_inspector=_fake_edf_inspector,
            yasa_runner=_fake_yasa_runner,
        )
        return service.run_analysis(request)


class _FakeCompiledGraph:
    def __init__(self, graph: "_FakeStateGraph") -> None:
        self.graph = graph

    def invoke(self, state: dict) -> dict:
        current = self.graph.entry_point
        while current != self.graph.end:
            node_result = self.graph.nodes[current](state)
            state = {**state, **node_result}
            current = self.graph.next_node(current)
        return state


class _FakeStateGraph:
    end = "__end__"

    def __init__(self, state_schema: object) -> None:
        self.state_schema = state_schema
        self.nodes = {}
        self.edges = []
        self.entry_point = None

    def add_node(self, name: str, node) -> None:
        self.nodes[name] = node

    def set_entry_point(self, name: str) -> None:
        self.entry_point = name

    def add_edge(self, source: str, target: str) -> None:
        self.edges.append((source, target))

    def next_node(self, source: str) -> str:
        for edge_source, edge_target in self.edges:
            if edge_source == source:
                return edge_target
        raise AssertionError(f"Missing outgoing edge for {source}")

    def compile(self) -> _FakeCompiledGraph:
        return _FakeCompiledGraph(self)


def _fake_langgraph_symbols():
    return _FakeStateGraph, _FakeStateGraph.end


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
    staging = build_yasa_sleep_staging_result(
        ["WAKE", "N2", "REM", "N2"],
        recording_duration_seconds=120.0,
    )
    return YASARunnerResult(
        edf_info=_fake_edf_inspector(edf_path),
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
