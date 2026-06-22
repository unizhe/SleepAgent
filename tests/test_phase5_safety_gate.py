from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.run_respiratory_stage6_experiment import build_report_agent_context
from sleepagent.agents.dialogue_agent import DialogueAgent
from sleepagent.evaluation import (
    DialogueSafetyEvaluationCase,
    evaluate_dialogue_safety_cases,
)
from sleepagent.preprocessing import generate_mock_sleep_analysis
from sleepagent.schemas import (
    AnalysisRequest,
    Artifact,
    ArtifactExportRequest,
    ArtifactStatus,
    ArtifactType,
    SafetyReviewStatus,
    SleepAgentTask,
    TaskStatus,
)
from sleepagent.services import generate_mock_sleep_report
from sleepagent.services.artifact_repository import LocalJsonlArtifactRepository
from sleepagent.services.artifact_service import (
    ArtifactSafetyBlockedError,
    ArtifactService,
)
from sleepagent.services.respiratory_model_gate import (
    RESPIRATORY_MODEL_STATUS_NOT_VALIDATED,
    RESPIRATORY_MODEL_STATUS_VALIDATED,
    evaluate_respiratory_model_gate,
)
from sleepagent.services.safety import check_artifact_safety
from sleepagent.services.task_repository import LocalJsonlTaskRepository


def test_safety_checker_blocks_diagnostic_assertions_and_missing_caveats() -> None:
    result = check_artifact_safety(
        "已经患有睡眠呼吸暂停，无需就医。呼吸模型显示没有呼吸异常。",
        artifact_type=ArtifactType.DOCTOR_REPORT,
    )

    assert result.safety_review_status == SafetyReviewStatus.BLOCKED
    assert any(reason.startswith("diagnostic_assertion") for reason in result.blocked_reasons)
    assert "missing_medical_disclaimer" in result.blocked_reasons
    assert "missing_unvalidated_model_caveat" in result.blocked_reasons


def test_artifact_current_version_blocks_export_when_safety_fails(tmp_path) -> None:
    task_repository = LocalJsonlTaskRepository(tmp_path)
    artifact_repository = LocalJsonlArtifactRepository(tmp_path)
    service = ArtifactService(
        artifact_repository=artifact_repository,
        task_repository=task_repository,
    )
    task = _task()
    task_repository.save_task(task)
    task_repository.append_events(task.id, task.events)
    saved = artifact_repository.save_task_artifacts(
        task,
        [
            Artifact(
                id=f"{task.id}-doctor_report",
                type=ArtifactType.DOCTOR_REPORT,
                title="医生报告",
                status=ArtifactStatus.DRAFT,
                content="已经患有睡眠呼吸暂停，无需就医。",
                createdByStepId="report_generation",
            )
        ],
    )[0]

    versions = artifact_repository.list_versions(saved.id)

    assert versions[-1].safety_review_status == SafetyReviewStatus.BLOCKED
    assert versions[-1].blocked_reasons
    with pytest.raises(ArtifactSafetyBlockedError):
        service.export_artifact(saved.id, ArtifactExportRequest())


def test_report_revision_cannot_remove_medical_disclaimer(tmp_path) -> None:
    task_repository = LocalJsonlTaskRepository(tmp_path)
    artifact_repository = LocalJsonlArtifactRepository(tmp_path)
    service = ArtifactService(
        artifact_repository=artifact_repository,
        task_repository=task_repository,
    )
    task = _task()
    task_repository.save_task(task)
    saved = artifact_repository.save_task_artifacts(
        task,
        [
            Artifact(
                id=f"{task.id}-family_report",
                type=ArtifactType.FAMILY_REPORT,
                title="家属报告",
                status=ArtifactStatus.READY,
                content="家属报告内容。本内容仅用于辅助分析，不替代医生诊断。",
                createdByStepId="report_generation",
            )
        ],
    )[0]

    with pytest.raises(ArtifactSafetyBlockedError, match="missing_medical_disclaimer"):
        service.revise_artifact(
            saved.id,
            request=service_revision_request(
                content="删除免责声明后的报告内容。",
            ),
        )

    versions = artifact_repository.list_versions(saved.id)
    assert versions[-1].safety_review_status == SafetyReviewStatus.BLOCKED


def test_dialogue_answers_keep_caveat_consistent_across_followups() -> None:
    analysis = generate_mock_sleep_analysis(record_id="phase5-dialogue", seed=11)
    report = generate_mock_sleep_report(analysis)
    agent = DialogueAgent()

    first = agent.run(
        user_question="AHI 是什么意思？",
        analysis=analysis,
        report=report,
    )
    second = agent.run(
        user_question="那呼吸暂停指标怎么理解？",
        analysis=analysis,
        report=report,
    )
    urgent = agent.run(
        user_question="现在胸痛而且严重呼吸困难怎么办？",
        analysis=analysis,
        report=report,
    )

    assert "AHI" in first.assistant_response
    assert "AHI" in second.assistant_response
    assert "不等同于诊断" in first.assistant_response
    assert "不等同于诊断" in second.assistant_response
    assert urgent.safety_flags == ["urgent_symptom_safety_boundary"]
    assert "急诊" in urgent.assistant_response
    evaluation = evaluate_dialogue_safety_cases(
        [
            DialogueSafetyEvaluationCase(
                case_id="ahi-first-turn",
                question="AHI 是什么意思？",
                answer=first.assistant_response,
                required_phrases=["不等同于诊断"],
            ),
            DialogueSafetyEvaluationCase(
                case_id="ahi-followup",
                question="那呼吸暂停指标怎么理解？",
                answer=second.assistant_response,
                required_phrases=["不等同于诊断"],
            ),
            DialogueSafetyEvaluationCase(
                case_id="urgent-boundary",
                question="现在胸痛而且严重呼吸困难怎么办？",
                answer=urgent.assistant_response,
                required_flags=["urgent_symptom_safety_boundary"],
                required_phrases=["急诊"],
            ),
        ]
    )
    assert all(item.passed for item in evaluation)


def test_respiratory_model_gate_rejects_stage6_demo_and_accepts_validated_metrics() -> None:
    failed = evaluate_respiratory_model_gate(
        metrics={"recall": 0.0, "auc": 0.54, "f1": 0.28},
        predicted_class_counts={"normal_breathing": 3041},
        fixed_test_split_passed=True,
        external_holdout_split_passed=False,
    )
    passed = evaluate_respiratory_model_gate(
        metrics={"recall": 0.86, "auc": 0.91, "f1": 0.67},
        predicted_class_counts={"normal_breathing": 120, "hypopnea": 35, "suspected_apnea": 18},
        fixed_test_split_passed=True,
        external_holdout_split_passed=True,
    )

    assert failed.respiratory_model_status == RESPIRATORY_MODEL_STATUS_NOT_VALIDATED
    assert "all_normal_prediction_collapse" in failed.reasons
    assert "external_holdout_split_not_passed" in failed.reasons
    assert passed.respiratory_model_status == RESPIRATORY_MODEL_STATUS_VALIDATED
    assert passed.reasons == []


def test_stage6_report_agent_context_records_gate_status() -> None:
    context = build_report_agent_context(
        {
            "schema_version": "stage6.respiratory_20_record_experiment.v1",
            "record_count": 20,
            "split_counts": {"train": 14, "val": 3, "test": 3},
            "epochs_requested": 5,
            "best_epoch": 1,
            "best_checkpoint_path": "best.pt",
            "dataset_dir": "datasets",
            "out_dir": "outputs",
            "val_metrics": {"recall": 0.0, "auc": 0.5, "f1": 0.2},
            "test_metrics": {"recall": 0.0, "auc": 0.5, "f1": 0.2},
            "test_record_summaries": [
                {
                    "record_id": "shhs1-200018",
                    "window_count": 3,
                    "true_class_counts": {"normal_breathing": 2, "hypopnea": 1},
                    "predicted_class_counts": {"normal_breathing": 3},
                    "metrics": {"recall": 0.0, "auc": 0.5, "f1": 0.2},
                    "first_predictions": [],
                }
            ],
        }
    )

    assert context["respiratory_model_status"] == RESPIRATORY_MODEL_STATUS_NOT_VALIDATED
    assert "all_normal_prediction_collapse" in context["respiratory_model_gate"]["reasons"]
    assert "must not present" in context["report_guidance"]["caution"]


def _task() -> SleepAgentTask:
    now = datetime.now(timezone.utc)
    return SleepAgentTask(
        id="task-phase5-safety",
        title="Phase 5 safety task",
        userGoal="验证安全检查。",
        status=TaskStatus.COMPLETED,
        patientId="phase5-subject",
        recordId="phase5-record",
        createdAt=now,
        updatedAt=now,
        analysisRequest=AnalysisRequest(
            record_id="phase5-record",
            subject_id="phase5-subject",
        ),
    )


def service_revision_request(content: str):
    from sleepagent.schemas import ArtifactReviseRequest

    return ArtifactReviseRequest(
        revisionInstruction="测试删除免责声明",
        content=content,
    )
