from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend import main as backend_main
from sleepagent.agents.dialogue_agent import DialogueAgent
from sleepagent.preprocessing import generate_mock_sleep_analysis
from sleepagent.schemas import (
    AgentEvent,
    AgentEventType,
    AnalysisRequest,
    Artifact,
    ArtifactExportFormat,
    ArtifactExportRequest,
    ArtifactReviseRequest,
    ArtifactStatus,
    ArtifactType,
    SleepAgentTask,
    TaskConfirmRequest,
    TaskCreateRequest,
    TaskStatus,
)
from sleepagent.services import generate_mock_sleep_report
from sleepagent.services import task_service as task_service_module
from sleepagent.services.artifact_repository import (
    ARTIFACT_RECORDS_FILENAME,
    ARTIFACT_VERSIONS_FILENAME,
    LocalJsonlArtifactRepository,
)
from sleepagent.services.artifact_service import ArtifactService
from sleepagent.services.data_management import (
    ANALYSIS_RECORDS_FILENAME,
    REPORT_RECORDS_FILENAME,
    LocalJsonlSleepDataRepository,
)
from sleepagent.services.memory_repository import (
    MEMORY_SUMMARIES_FILENAME,
    LocalJsonlMemoryRepository,
)
from sleepagent.services.task_repository import LocalJsonlTaskRepository
from sleepagent.services.task_service import TaskService


def test_phase4_artifact_revision_export_and_memory_persistence(
    tmp_path,
    monkeypatch,
) -> None:
    store_dir = tmp_path / "store"
    task_repository = LocalJsonlTaskRepository(store_dir)
    artifact_repository = LocalJsonlArtifactRepository(store_dir)
    memory_repository = LocalJsonlMemoryRepository(store_dir)
    data_repository = LocalJsonlSleepDataRepository(store_dir)
    task_service = TaskService(
        repository=task_repository,
        artifact_repository=artifact_repository,
        memory_repository=memory_repository,
        data_repository=data_repository,
    )
    artifact_service = ArtifactService(
        artifact_repository=artifact_repository,
        task_repository=task_repository,
    )
    analysis = generate_mock_sleep_analysis(
        record_id="phase4-record-001",
        subject_id="phase4-subject-001",
        seed=7,
        abnormal_event_rate_per_hour=12.0,
    )
    report = generate_mock_sleep_report(analysis)

    monkeypatch.setattr(
        task_service_module,
        "run_sleep_agent_task_state_graph",
        _fake_completed_task_graph(analysis=analysis, report=report),
    )
    monkeypatch.setattr(backend_main, "_task_service", lambda: task_service)
    monkeypatch.setattr(backend_main, "_artifact_service", lambda: artifact_service)

    task = backend_main.create_task(
        TaskCreateRequest(
            title="Phase 4 artifact task",
            analysisRequest=AnalysisRequest(
                record_id=analysis.metadata.record_id,
                subject_id=analysis.metadata.patient.subject_id,
            ),
        )
    )
    completed = backend_main.confirm_task(task.id, TaskConfirmRequest())

    assert completed.status == TaskStatus.COMPLETED
    assert store_dir.joinpath(ARTIFACT_RECORDS_FILENAME).exists()
    assert store_dir.joinpath(ARTIFACT_VERSIONS_FILENAME).exists()
    assert store_dir.joinpath(ANALYSIS_RECORDS_FILENAME).exists()
    assert store_dir.joinpath(REPORT_RECORDS_FILENAME).exists()
    assert store_dir.joinpath(MEMORY_SUMMARIES_FILENAME).exists()

    artifacts = backend_main.get_task_artifacts(task.id)
    doctor_artifact = _artifact_by_type(artifacts, ArtifactType.DOCTOR_REPORT)
    risk_artifact = _artifact_by_type(artifacts, ArtifactType.RISK_SUMMARY)

    assert doctor_artifact.task_id == task.id
    assert doctor_artifact.subject_id == analysis.metadata.patient.subject_id
    assert doctor_artifact.record_id == analysis.metadata.record_id
    assert doctor_artifact.current_version_id == f"{doctor_artifact.id}-v1"

    with pytest.raises(HTTPException) as blocked_export:
        backend_main.export_artifact(
            doctor_artifact.id,
            ArtifactExportRequest(format=ArtifactExportFormat.MARKDOWN),
        )
    assert blocked_export.value.status_code == 409

    revised = backend_main.revise_artifact(
        doctor_artifact.id,
        ArtifactReviseRequest(
            revisionInstruction="补充医生复核提示",
            content=(
                "医生版修订内容：来源=SleepAgent，生成方式=Phase4 test。"
                "本内容仅用于睡眠健康辅助分析，不替代医生诊断。"
            ),
        ),
    )
    versions = backend_main.get_artifact_versions(doctor_artifact.id)

    assert revised.status == ArtifactStatus.REVISED
    assert revised.current_version_id == f"{doctor_artifact.id}-v2"
    assert [version.version_number for version in versions] == [1, 2]
    assert versions[-1].content == revised.content

    refreshed_task = backend_main.get_task(task.id)
    refreshed_doctor = _artifact_by_type(
        refreshed_task.artifacts,
        ArtifactType.DOCTOR_REPORT,
    )
    assert refreshed_doctor.content == revised.content

    confirmed = backend_main.confirm_artifact(doctor_artifact.id)
    exported_doctor = backend_main.export_artifact(
        confirmed.id,
        ArtifactExportRequest(format=ArtifactExportFormat.MARKDOWN),
    )
    exported_risk_json = backend_main.export_artifact(
        risk_artifact.id,
        ArtifactExportRequest(format=ArtifactExportFormat.JSON),
    )
    exported_risk_csv = backend_main.export_artifact(
        risk_artifact.id,
        ArtifactExportRequest(format=ArtifactExportFormat.CSV),
    )

    assert confirmed.status == ArtifactStatus.READY
    assert exported_doctor.media_type == "text/markdown;charset=utf-8"
    assert "Disclaimer" in exported_doctor.content
    assert "SleepAgent 输出仅用于睡眠健康辅助分析" in exported_doctor.content
    assert exported_risk_json.media_type == "application/json"
    assert '"risk_summary"' in exported_risk_json.content
    assert exported_risk_csv.content.splitlines()[0].startswith("artifact_id,task_id")

    memory_summary = memory_repository.get_latest_memory_summary(
        analysis.metadata.patient.subject_id,
    )
    assert memory_summary is not None
    dialogue_turn = DialogueAgent(memory_repository=memory_repository).run(
        user_question="后续照护有什么建议？",
        analysis=analysis,
        report=report,
    )
    assert dialogue_turn.context_used is True
    assert "历史摘要提示" in dialogue_turn.assistant_response


def _fake_completed_task_graph(*, analysis, report):
    def _run(task: SleepAgentTask, **_: object) -> SleepAgentTask:
        now = datetime.now(timezone.utc)
        artifacts = [
            Artifact(
                id=f"{task.id}-risk_summary",
                taskId=task.id,
                subjectId=task.patient_id,
                recordId=task.record_id,
                type=ArtifactType.RISK_SUMMARY,
                title="风险摘要",
                status=ArtifactStatus.READY,
                content=(
                    "来源：SleepAgent Phase 4 测试。生成方式：任务状态图。"
                    "免责声明：仅用于睡眠健康辅助分析。"
                ),
                createdByStepId="risk_assessment",
            ),
            Artifact(
                id=f"{task.id}-doctor_report",
                taskId=task.id,
                subjectId=task.patient_id,
                recordId=task.record_id,
                type=ArtifactType.DOCTOR_REPORT,
                title="医生摘要",
                status=ArtifactStatus.DRAFT,
                content=(
                    "医生版摘要。来源：SleepAgent Phase 4 测试。"
                    "生成方式：报告 Agent。免责声明：仅用于辅助分析，不替代医生诊断。"
                ),
                createdByStepId="report_generation",
            ),
            Artifact(
                id=f"{task.id}-care_plan",
                taskId=task.id,
                subjectId=task.patient_id,
                recordId=task.record_id,
                type=ArtifactType.CARE_PLAN,
                title="照护计划",
                status=ArtifactStatus.DRAFT,
                content=(
                    "照护计划草案。来源：SleepAgent Phase 4 测试。"
                    "生成方式：照护 Agent。免责声明：仅用于辅助分析，不替代医生诊断，启用前需确认。"
                ),
                createdByStepId="care_plan",
            ),
        ]
        events = [
            *task.events,
            AgentEvent(
                id=f"{task.id}-event-0003",
                type=AgentEventType.TASK_COMPLETED,
                title="任务已完成",
                message="Phase 4 测试任务已完成。",
                timestamp=now,
            ),
        ]
        return task.model_copy(
            update={
                "status": TaskStatus.COMPLETED,
                "events": events,
                "artifacts": artifacts,
                "analysis_result": analysis,
                "report_result": report,
                "updated_at": now,
            }
        )

    return _run


def _artifact_by_type(
    artifacts: list[Artifact],
    artifact_type: ArtifactType,
) -> Artifact:
    for artifact in artifacts:
        if artifact.type == artifact_type:
            return artifact
    raise AssertionError(f"Missing artifact type: {artifact_type.value}")
