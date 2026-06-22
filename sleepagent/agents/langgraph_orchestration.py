from __future__ import annotations

from typing import Any, TypedDict

from sleepagent.agents.dialogue_agent import DialogueAgent
from sleepagent.agents.report_agent import ReportAgent
from sleepagent.agents.sleep_analysis_agent import SleepAnalysisAgent
from sleepagent.schemas import (
    AgentOrchestrationMode,
    AgentStepName,
    AgentStepStatus,
    AgentStepTrace,
    DialogueTurn,
    MockSleepReport,
    SleepAgentOrchestrationRequest,
    SleepAgentOrchestrationResult,
    SleepAnalysisResult,
)
from sleepagent.services.repository_factory import build_repository_bundle


SLEEP_ANALYSIS_NODE = "sleep_analysis"
REPORT_NODE = "report"
DIALOGUE_NODE = "dialogue"
BUILD_RESULT_NODE = "build_result"


class LangGraphUnavailableError(RuntimeError):
    """Raised when the optional LangGraph dependency is not installed."""


class SleepAgentLangGraphState(TypedDict, total=False):
    request: SleepAgentOrchestrationRequest
    analysis: SleepAnalysisResult
    report: MockSleepReport
    dialogue: DialogueTurn | None
    steps: list[AgentStepTrace]
    result: SleepAgentOrchestrationResult


def build_sleep_agent_langgraph(
    *,
    sleep_analysis_agent: SleepAnalysisAgent | None = None,
    report_agent: ReportAgent | None = None,
    dialogue_agent: DialogueAgent | None = None,
) -> Any:
    state_graph, end = _load_langgraph_symbols()
    graph = state_graph(SleepAgentLangGraphState)
    resolved_sleep_analysis_agent = sleep_analysis_agent or SleepAnalysisAgent()
    resolved_report_agent = report_agent or ReportAgent()
    resolved_dialogue_agent = dialogue_agent or DialogueAgent(
        memory_repository=build_repository_bundle().memory_repository
    )

    graph.add_node(
        SLEEP_ANALYSIS_NODE,
        lambda state: _sleep_analysis_node(state, resolved_sleep_analysis_agent),
    )
    graph.add_node(
        REPORT_NODE,
        lambda state: _report_node(state, resolved_report_agent),
    )
    graph.add_node(
        DIALOGUE_NODE,
        lambda state: _dialogue_node(state, resolved_dialogue_agent),
    )
    graph.add_node(BUILD_RESULT_NODE, _build_result_node)
    graph.set_entry_point(SLEEP_ANALYSIS_NODE)
    graph.add_edge(SLEEP_ANALYSIS_NODE, REPORT_NODE)
    graph.add_edge(REPORT_NODE, DIALOGUE_NODE)
    graph.add_edge(DIALOGUE_NODE, BUILD_RESULT_NODE)
    graph.add_edge(BUILD_RESULT_NODE, end)
    return graph.compile()


def run_sleep_agent_langgraph_orchestration(
    request: SleepAgentOrchestrationRequest | None = None,
    **request_overrides: object,
) -> SleepAgentOrchestrationResult:
    if request is not None and request_overrides:
        raise ValueError("Pass either request or request_overrides, not both.")

    resolved_request = request or SleepAgentOrchestrationRequest.model_validate(
        request_overrides
    )
    app = build_sleep_agent_langgraph()
    final_state = app.invoke({"request": resolved_request, "steps": []})
    result = final_state.get("result")
    if not isinstance(result, SleepAgentOrchestrationResult):
        raise RuntimeError("LangGraph run did not produce a SleepAgent result.")
    return result


def _sleep_analysis_node(
    state: SleepAgentLangGraphState,
    agent: SleepAnalysisAgent,
) -> SleepAgentLangGraphState:
    request = _require_state_value(state, "request", SleepAgentOrchestrationRequest)
    analysis = agent.run(request)
    return {
        "analysis": analysis,
        "steps": [
            *_state_steps(state),
            AgentStepTrace(
                step_name=AgentStepName.SLEEP_ANALYSIS,
                status=AgentStepStatus.COMPLETED,
                message="Generated structured sleep analysis result.",
            ),
        ],
    }


def _report_node(
    state: SleepAgentLangGraphState,
    agent: ReportAgent,
) -> SleepAgentLangGraphState:
    request = _require_state_value(state, "request", SleepAgentOrchestrationRequest)
    analysis = _require_state_value(state, "analysis", SleepAnalysisResult)
    report = agent.run(
        analysis,
        use_deepseek_report=request.use_deepseek_report,
    )
    return {
        "report": report,
        "steps": [
            *_state_steps(state),
            AgentStepTrace(
                step_name=AgentStepName.REPORT,
                status=AgentStepStatus.COMPLETED,
                message="Generated report from the analysis result.",
            ),
        ],
    }


def _dialogue_node(
    state: SleepAgentLangGraphState,
    agent: DialogueAgent,
) -> SleepAgentLangGraphState:
    request = _require_state_value(state, "request", SleepAgentOrchestrationRequest)
    analysis = _require_state_value(state, "analysis", SleepAnalysisResult)
    report = _require_state_value(state, "report", MockSleepReport)

    if request.user_question:
        dialogue = agent.run(
            user_question=request.user_question,
            analysis=analysis,
            report=report,
            dialogue_context=request.dialogue_context,
        )
        step = AgentStepTrace(
            step_name=AgentStepName.DIALOGUE,
            status=AgentStepStatus.COMPLETED,
            message="Answered the user question using the report context.",
        )
    else:
        dialogue = None
        step = AgentStepTrace(
            step_name=AgentStepName.SKIP_DIALOGUE,
            status=AgentStepStatus.SKIPPED,
            message="No user question was provided.",
        )

    return {
        "dialogue": dialogue,
        "steps": [*_state_steps(state), step],
    }


def _build_result_node(state: SleepAgentLangGraphState) -> SleepAgentLangGraphState:
    analysis = _require_state_value(state, "analysis", SleepAnalysisResult)
    report = _require_state_value(state, "report", MockSleepReport)
    dialogue = state.get("dialogue")
    if dialogue is not None and not isinstance(dialogue, DialogueTurn):
        raise TypeError("State field 'dialogue' must be a DialogueTurn or None.")

    return {
        "result": SleepAgentOrchestrationResult(
            analysis=analysis,
            report=report,
            dialogue=dialogue,
            steps=_state_steps(state),
            orchestration_mode=AgentOrchestrationMode.LANGGRAPH,
            generated_at=analysis.generated_at,
        )
    }


def _state_steps(state: SleepAgentLangGraphState) -> list[AgentStepTrace]:
    steps = state.get("steps", [])
    if not isinstance(steps, list):
        raise TypeError("State field 'steps' must be a list.")
    return steps


def _require_state_value(
    state: SleepAgentLangGraphState,
    key: str,
    expected_type: type[Any],
) -> Any:
    value = state.get(key)
    if not isinstance(value, expected_type):
        raise TypeError(f"State field {key!r} must be {expected_type.__name__}.")
    return value


def _load_langgraph_symbols() -> tuple[type[Any], Any]:
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:
        raise LangGraphUnavailableError(
            "LangGraph is optional. Install it with `python -m pip install -e "
            "\".[agent]\"` before running the LangGraph orchestration path."
        ) from exc
    return StateGraph, END
