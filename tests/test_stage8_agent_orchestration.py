import pytest
from pydantic import ValidationError

from sleepagent.agents import run_sleep_agent_orchestration
from sleepagent.schemas import (
    AgentOrchestrationMode,
    AgentStepName,
    AgentStepStatus,
    DialogueContext,
    SleepAgentOrchestrationRequest,
)


def test_stage8_request_schema_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SleepAgentOrchestrationRequest.model_validate({"unexpected": "field"})


def test_stage8_orchestration_runs_analysis_and_report_without_question() -> None:
    result = run_sleep_agent_orchestration(
        record_id="stage8-record",
        subject_id="stage8-subject",
        duration_hours=0.5,
        seed=21,
    )

    assert result.analysis.metadata.record_id == "stage8-record"
    assert result.report.summary.record_id == "stage8-record"
    assert result.dialogue is None
    assert result.orchestration_mode == AgentOrchestrationMode.LINEAR
    assert [step.step_name for step in result.steps] == [
        AgentStepName.SLEEP_ANALYSIS,
        AgentStepName.REPORT,
        AgentStepName.SKIP_DIALOGUE,
    ]
    assert result.steps[-1].status == AgentStepStatus.SKIPPED
    assert result.generated_at == result.analysis.generated_at


def test_stage8_orchestration_answers_report_grounded_question() -> None:
    result = run_sleep_agent_orchestration(
        record_id="stage8-question-record",
        duration_hours=0.5,
        seed=22,
        user_question="AHI 和呼吸暂停是什么意思？",
    )

    assert result.dialogue is not None
    assert result.dialogue.referenced_record_id == "stage8-question-record"
    assert "AHI" in result.dialogue.assistant_response
    assert "不等同于诊断" in result.dialogue.assistant_response
    assert result.steps[-1].status == AgentStepStatus.COMPLETED


def test_stage8_orchestration_uses_dialogue_context_when_provided() -> None:
    result = run_sleep_agent_orchestration(
        record_id="stage8-context-record",
        duration_hours=0.5,
        seed=25,
        user_question="有什么照护建议？",
        dialogue_context=DialogueContext(
            history_summary="最近一周夜间憋醒比上周更频繁",
            user_preferences=["回答尽量简短", "面向家属解释"],
            recent_questions=["AHI 是否升高"],
        ),
    )

    assert result.dialogue is not None
    assert result.dialogue.context_used is True
    assert "历史摘要提示" in result.dialogue.assistant_response
    assert "最近一周夜间憋醒比上周更频繁" in result.dialogue.assistant_response
    assert "回答尽量简短" in result.dialogue.assistant_response


def test_stage8_dialogue_keeps_urgent_symptom_boundary() -> None:
    result = run_sleep_agent_orchestration(
        duration_hours=0.5,
        seed=23,
        user_question="现在胸痛而且严重呼吸困难，怎么办？",
    )

    assert result.dialogue is not None
    assert result.dialogue.safety_flags == ["urgent_symptom_safety_boundary"]
    assert "急诊" in result.dialogue.assistant_response
    assert "不能替代医生" in result.dialogue.assistant_response
    assert result.dialogue.context_used is False


def test_stage8_default_report_agent_does_not_call_deepseek(monkeypatch) -> None:
    def fail_deepseek_call(*args, **kwargs):
        raise AssertionError("DeepSeek should remain opt-in for Stage 8 orchestration")

    monkeypatch.setattr(
        "sleepagent.agents.report_agent.generate_sleep_report_with_deepseek_fallback",
        fail_deepseek_call,
    )

    result = run_sleep_agent_orchestration(duration_hours=0.5, seed=24)

    assert result.report.elder_report
    assert "模拟睡眠报告" in result.report.elder_report
