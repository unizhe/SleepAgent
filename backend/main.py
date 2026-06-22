import os
import asyncio
import json
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from sleepagent.agents import (
    LangGraphUnavailableError,
    run_sleep_agent_langgraph_orchestration,
    run_sleep_agent_orchestration,
)
from sleepagent.preprocessing import generate_mock_sleep_analysis
from sleepagent.schemas import (
    AnalysisMode,
    AnalysisRequest,
    AnalysisRunResult,
    AgentEvent,
    Artifact,
    ArtifactExportRequest,
    ArtifactExportResult,
    ArtifactReviseRequest,
    ArtifactVersion,
    MockSleepReport,
    SleepAgentEndpointRequest,
    SleepAgentOrchestrationRequest,
    SleepAgentOrchestrationResult,
    SleepAgentTask,
    SleepAnalysisResult,
    Stage9MockContextRequest,
    Stage9MockContextResult,
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskStatus,
)
from sleepagent.services import (
    SLEEPAGENT_DATA_STORE_DIR_ENV,
    AnalysisService,
    AnalysisServiceError,
    LocalJsonlAlertEventRepository,
    LocalJsonlSleepDataRepository,
    build_mock_external_context,
    compress_memory_from_repository,
    generate_mock_sleep_report,
    generate_sleep_report_with_deepseek_fallback,
    record_high_risk_alert_if_needed,
)
from sleepagent.services.artifact_repository import ArtifactNotFoundError
from sleepagent.services.artifact_service import (
    ArtifactExportBlockedError,
    ArtifactSafetyBlockedError,
    ArtifactService,
)
from sleepagent.services.repository_factory import build_repository_bundle
from sleepagent.services.task_repository import TaskNotFoundError
from sleepagent.services.task_service import InvalidTaskTransitionError, TaskService


app = FastAPI(title="SleepAgent", version="0.1.0")
DEFAULT_STAGE9_API_STORE_DIR = "/tmp/sleepagent_stage9_api"
DEFAULT_CORS_ORIGINS = (
    "http://127.0.0.1:18510",
    "http://localhost:18510",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "SLEEPAGENT_CORS_ORIGINS",
            ",".join(DEFAULT_CORS_ORIGINS),
        ).split(",")
        if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {
        "status": "ok",
        "project": "SleepAgent",
        "stage": "project_skeleton",
    }


@app.get("/mock-analysis", response_model=SleepAnalysisResult)
def mock_analysis(
    record_id: Annotated[
        str,
        Query(min_length=1, description="Mock PSG record identifier."),
    ] = "mock-shhs-0001",
    subject_id: Annotated[
        str,
        Query(min_length=1, description="Mock subject identifier."),
    ] = "mock-subject-0001",
    duration_hours: Annotated[
        float,
        Query(ge=0.5, le=12.0, description="Synthetic recording duration in hours."),
    ] = 8.0,
    seed: Annotated[
        int,
        Query(description="Random seed for deterministic mock data."),
    ] = 42,
    abnormal_event_rate_per_hour: Annotated[
        float,
        Query(ge=0.0, le=60.0, description="Synthetic abnormal respiratory event rate."),
    ] = 6.0,
) -> SleepAnalysisResult:
    return generate_mock_sleep_analysis(
        record_id=record_id,
        subject_id=subject_id,
        duration_hours=duration_hours,
        seed=seed,
        abnormal_event_rate_per_hour=abnormal_event_rate_per_hour,
    )


@app.get("/mock-report", response_model=MockSleepReport)
def mock_report(
    record_id: Annotated[
        str,
        Query(min_length=1, description="Mock PSG record identifier."),
    ] = "mock-shhs-0001",
    subject_id: Annotated[
        str,
        Query(min_length=1, description="Mock subject identifier."),
    ] = "mock-subject-0001",
    duration_hours: Annotated[
        float,
        Query(ge=0.5, le=12.0, description="Synthetic recording duration in hours."),
    ] = 8.0,
    seed: Annotated[
        int,
        Query(description="Random seed for deterministic mock data."),
    ] = 42,
    abnormal_event_rate_per_hour: Annotated[
        float,
        Query(ge=0.0, le=60.0, description="Synthetic abnormal respiratory event rate."),
    ] = 6.0,
) -> MockSleepReport:
    analysis = generate_mock_sleep_analysis(
        record_id=record_id,
        subject_id=subject_id,
        duration_hours=duration_hours,
        seed=seed,
        abnormal_event_rate_per_hour=abnormal_event_rate_per_hour,
    )
    return generate_mock_sleep_report(analysis)


@app.get("/mock-report/llm", response_model=MockSleepReport)
def mock_report_llm(
    record_id: Annotated[
        str,
        Query(min_length=1, description="Mock PSG record identifier."),
    ] = "mock-shhs-0001",
    subject_id: Annotated[
        str,
        Query(min_length=1, description="Mock subject identifier."),
    ] = "mock-subject-0001",
    duration_hours: Annotated[
        float,
        Query(ge=0.5, le=12.0, description="Synthetic recording duration in hours."),
    ] = 8.0,
    seed: Annotated[
        int,
        Query(description="Random seed for deterministic mock data."),
    ] = 42,
    abnormal_event_rate_per_hour: Annotated[
        float,
        Query(ge=0.0, le=60.0, description="Synthetic abnormal respiratory event rate."),
    ] = 6.0,
    use_deepseek: Annotated[
        bool,
        Query(description="Opt in to the guarded DeepSeek report fallback path."),
    ] = False,
) -> MockSleepReport:
    analysis = generate_mock_sleep_analysis(
        record_id=record_id,
        subject_id=subject_id,
        duration_hours=duration_hours,
        seed=seed,
        abnormal_event_rate_per_hour=abnormal_event_rate_per_hour,
    )
    if not use_deepseek:
        return generate_mock_sleep_report(analysis)
    return generate_sleep_report_with_deepseek_fallback(analysis)


@app.post("/analysis/run", response_model=AnalysisRunResult)
def run_analysis(request: AnalysisRequest) -> AnalysisRunResult:
    return AnalysisService().run_analysis(request)


@app.post("/tasks", response_model=SleepAgentTask)
def create_task(request: TaskCreateRequest) -> SleepAgentTask:
    return _task_service().create_task(request)


@app.get("/tasks/{task_id}", response_model=SleepAgentTask)
def get_task(task_id: str) -> SleepAgentTask:
    try:
        return _task_service().get_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Task not found.") from exc


@app.get("/tasks/{task_id}/events", response_model=list[AgentEvent])
def get_task_events(task_id: str) -> list[AgentEvent]:
    try:
        return _task_service().list_events(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Task not found.") from exc


@app.get("/tasks/{task_id}/events/stream")
def stream_task_events(task_id: str) -> StreamingResponse:
    try:
        _task_service().get_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Task not found.") from exc

    return StreamingResponse(
        _iter_task_event_sse(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/tasks/{task_id}/artifacts", response_model=list[Artifact])
def get_task_artifacts(task_id: str) -> list[Artifact]:
    try:
        return _task_service().list_artifacts(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Task not found.") from exc


@app.get("/artifacts/{artifact_id}", response_model=Artifact)
def get_artifact(artifact_id: str) -> Artifact:
    try:
        return _artifact_service().get_artifact(artifact_id)
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found.") from exc


@app.post("/artifacts/{artifact_id}/revise", response_model=Artifact)
def revise_artifact(
    artifact_id: str,
    request: ArtifactReviseRequest,
) -> Artifact:
    try:
        return _artifact_service().revise_artifact(artifact_id, request)
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found.") from exc
    except ArtifactSafetyBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/artifacts/{artifact_id}/confirm", response_model=Artifact)
def confirm_artifact(artifact_id: str) -> Artifact:
    try:
        return _artifact_service().confirm_artifact(artifact_id)
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found.") from exc


@app.get("/artifacts/{artifact_id}/versions", response_model=list[ArtifactVersion])
def get_artifact_versions(artifact_id: str) -> list[ArtifactVersion]:
    try:
        _artifact_service().get_artifact(artifact_id)
        return _artifact_service().list_versions(artifact_id)
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found.") from exc


@app.post("/artifacts/{artifact_id}/export", response_model=ArtifactExportResult)
def export_artifact(
    artifact_id: str,
    request: ArtifactExportRequest,
) -> ArtifactExportResult:
    try:
        return _artifact_service().export_artifact(artifact_id, request)
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found.") from exc
    except ArtifactExportBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ArtifactSafetyBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/tasks/{task_id}/confirm", response_model=SleepAgentTask)
def confirm_task(
    task_id: str,
    request: TaskConfirmRequest | None = None,
) -> SleepAgentTask:
    try:
        return _task_service().confirm_task(task_id, request)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Task not found.") from exc
    except InvalidTaskTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/tasks/{task_id}/cancel", response_model=SleepAgentTask)
def cancel_task(task_id: str) -> SleepAgentTask:
    try:
        return _task_service().cancel_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Task not found.") from exc
    except InvalidTaskTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/agent/orchestrate", response_model=SleepAgentOrchestrationResult)
def agent_orchestrate(
    record_id: Annotated[
        str,
        Query(min_length=1, description="Mock PSG record identifier."),
    ] = "mock-shhs-0001",
    subject_id: Annotated[
        str,
        Query(min_length=1, description="Mock subject identifier."),
    ] = "mock-subject-0001",
    duration_hours: Annotated[
        float,
        Query(ge=0.5, le=12.0, description="Synthetic recording duration in hours."),
    ] = 8.0,
    seed: Annotated[
        int,
        Query(description="Random seed for deterministic mock data."),
    ] = 42,
    abnormal_event_rate_per_hour: Annotated[
        float,
        Query(ge=0.0, le=60.0, description="Synthetic abnormal respiratory event rate."),
    ] = 6.0,
    user_question: Annotated[
        str | None,
        Query(min_length=1, description="Optional report-grounded user question."),
    ] = None,
    use_deepseek_report: Annotated[
        bool,
        Query(description="Opt in to the guarded DeepSeek report fallback path."),
    ] = False,
    use_langgraph: Annotated[
        bool,
        Query(description="Opt in to the optional LangGraph orchestration path."),
    ] = False,
    analysis_mode: Annotated[
        AnalysisMode,
        Query(description="Choose mock or real SHHS/YASA analysis mode."),
    ] = AnalysisMode.MOCK,
    shhs_root: Annotated[
        str | None,
        Query(min_length=1, description="Local SHHS root for real analysis mode."),
    ] = None,
    eeg_channel: Annotated[
        str,
        Query(min_length=1, description="EEG channel name passed to YASA."),
    ] = "EEG",
    eog_channel: Annotated[
        str | None,
        Query(min_length=1, description="Optional EOG channel name passed to YASA."),
    ] = "EOG(L)",
    emg_channel: Annotated[
        str | None,
        Query(min_length=1, description="Optional EMG channel name passed to YASA."),
    ] = "EMG",
    use_respiratory_model: Annotated[
        bool,
        Query(description="Request respiratory model inference; gated in Phase 1."),
    ] = False,
    respiratory_checkpoint_path: Annotated[
        str | None,
        Query(min_length=1, description="Optional respiratory checkpoint path."),
    ] = None,
    allow_demo_respiratory_model: Annotated[
        bool,
        Query(description="Mark a demo respiratory checkpoint as pipeline-demo only."),
    ] = False,
) -> SleepAgentOrchestrationResult:
    request = SleepAgentEndpointRequest(
        record_id=record_id,
        subject_id=subject_id,
        duration_hours=duration_hours,
        seed=seed,
        abnormal_event_rate_per_hour=abnormal_event_rate_per_hour,
        user_question=user_question,
        use_deepseek_report=use_deepseek_report,
        use_langgraph=use_langgraph,
        analysis_mode=analysis_mode,
        shhs_root=shhs_root,
        eeg_channel=eeg_channel,
        eog_channel=eog_channel,
        emg_channel=emg_channel,
        use_respiratory_model=use_respiratory_model,
        respiratory_checkpoint_path=respiratory_checkpoint_path,
        allow_demo_respiratory_model=allow_demo_respiratory_model,
    )
    return _run_agent_orchestration_request(request)


@app.post("/agent/orchestrate", response_model=SleepAgentOrchestrationResult)
def agent_orchestrate_post(
    request: SleepAgentEndpointRequest,
) -> SleepAgentOrchestrationResult:
    return _run_agent_orchestration_request(request)


@app.post("/stage9/mock-context", response_model=Stage9MockContextResult)
def stage9_mock_context(
    request: Stage9MockContextRequest,
) -> Stage9MockContextResult:
    store_dir = _resolve_stage9_store_dir()
    data_repository = LocalJsonlSleepDataRepository(store_dir)
    alert_repository = LocalJsonlAlertEventRepository(store_dir)

    analysis = generate_mock_sleep_analysis(
        record_id=request.record_id,
        subject_id=request.subject_id,
        duration_hours=request.duration_hours,
        seed=request.seed,
        abnormal_event_rate_per_hour=request.abnormal_event_rate_per_hour,
    )
    report = generate_mock_sleep_report(analysis)

    analysis_record = data_repository.save_analysis(analysis)
    report_record = data_repository.save_report(
        report,
        analysis_id=analysis_record.analysis_id,
    )
    memory_summary = compress_memory_from_repository(
        data_repository,
        subject_id=request.subject_id,
        max_records=request.max_memory_records,
    )
    alert_event = record_high_risk_alert_if_needed(
        alert_repository,
        analysis_record,
    )
    external_context = build_mock_external_context(
        subject_id=request.subject_id,
        location=request.location,
        context_date=request.context_date,
        seed=request.external_context_seed,
    )

    return Stage9MockContextResult(
        analysis_record=analysis_record,
        report_record=report_record,
        memory_summary=memory_summary,
        alert_event=alert_event,
        external_context=external_context,
        local_store_dir=str(store_dir),
        generated_at=analysis.generated_at,
    )


def _run_agent_orchestration_request(
    request: SleepAgentEndpointRequest,
) -> SleepAgentOrchestrationResult:
    try:
        if not request.use_langgraph:
            return run_sleep_agent_orchestration(
                SleepAgentOrchestrationRequest.model_validate(
                    request.model_dump(exclude={"use_langgraph"})
                )
            )

        graph_request = SleepAgentOrchestrationRequest.model_validate(
            request.model_dump(exclude={"use_langgraph"})
        )
        return run_sleep_agent_langgraph_orchestration(graph_request)
    except LangGraphUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail="LangGraph is not installed. Install the agent extra to use this path.",
        ) from exc
    except AnalysisServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_stage9_store_dir() -> Path:
    return Path(os.getenv(SLEEPAGENT_DATA_STORE_DIR_ENV, DEFAULT_STAGE9_API_STORE_DIR))


def _task_service() -> TaskService:
    return TaskService()


def _artifact_service() -> ArtifactService:
    repositories = build_repository_bundle()
    return ArtifactService(
        artifact_repository=repositories.artifact_repository,
        task_repository=repositories.task_repository,
    )


async def _iter_task_event_sse(
    task_id: str,
    *,
    poll_interval_seconds: float = 0.5,
    idle_timeout_seconds: float = 300.0,
) -> AsyncIterator[str]:
    seen_event_ids: set[str] = set()
    idle_seconds = 0.0

    while idle_seconds <= idle_timeout_seconds:
        service = _task_service()
        try:
            task = service.get_task(task_id)
        except TaskNotFoundError:
            yield _format_sse_event(
                AgentEvent(
                    id=f"{task_id}-event-not-found",
                    type="error",
                    title="任务不存在",
                    message="无法继续订阅事件流，因为任务不存在。",
                    payload={"error_code": "task_not_found"},
                )
            )
            return

        events = service.list_events(task_id)
        new_events = [
            event for event in events if event.id not in seen_event_ids
        ]
        for event in new_events:
            seen_event_ids.add(event.id)
            yield _format_sse_event(event)

        if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            return

        if new_events:
            idle_seconds = 0.0
        else:
            idle_seconds += poll_interval_seconds
            yield ": keep-alive\n\n"
        await asyncio.sleep(poll_interval_seconds)

    yield _format_sse_event(
        AgentEvent(
            id=f"{task_id}-event-stream-timeout",
            type="error",
            title="事件流超时",
            message="任务事件流在等待新事件时超时，请重新拉取任务状态。",
            payload={"error_code": "event_stream_timeout"},
        )
    )


def _format_sse_event(event: AgentEvent) -> str:
    payload = event.model_dump(mode="json", by_alias=True)
    return (
        f"id: {event.id}\n"
        f"event: {event.type.value}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )
