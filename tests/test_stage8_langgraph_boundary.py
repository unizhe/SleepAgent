import pytest

from sleepagent.agents import (
    LangGraphUnavailableError,
    run_sleep_agent_langgraph_orchestration,
)
from sleepagent.agents import langgraph_orchestration as graph_module
from sleepagent.schemas import AgentOrchestrationMode, AgentStepName, AgentStepStatus


class FakeCompiledGraph:
    def __init__(self, graph: "FakeStateGraph") -> None:
        self.graph = graph

    def invoke(self, state: dict) -> dict:
        current = self.graph.entry_point
        while current != self.graph.end:
            node_result = self.graph.nodes[current](state)
            state = {**state, **node_result}
            current = self.graph.next_node(current)
        return state


class FakeStateGraph:
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

    def compile(self) -> FakeCompiledGraph:
        return FakeCompiledGraph(self)


def _fake_langgraph_symbols():
    return FakeStateGraph, FakeStateGraph.end


def test_langgraph_boundary_reports_missing_optional_dependency(monkeypatch) -> None:
    def raise_missing_langgraph():
        raise LangGraphUnavailableError("missing langgraph")

    monkeypatch.setattr(
        graph_module,
        "_load_langgraph_symbols",
        raise_missing_langgraph,
    )

    with pytest.raises(LangGraphUnavailableError):
        graph_module.build_sleep_agent_langgraph()


def test_langgraph_boundary_runs_same_agent_steps_with_fake_graph(monkeypatch) -> None:
    monkeypatch.setattr(
        graph_module,
        "_load_langgraph_symbols",
        _fake_langgraph_symbols,
    )

    result = run_sleep_agent_langgraph_orchestration(
        record_id="langgraph-record",
        duration_hours=0.5,
        seed=31,
        user_question="AHI 是什么？",
    )

    assert result.orchestration_mode == AgentOrchestrationMode.LANGGRAPH
    assert result.analysis.metadata.record_id == "langgraph-record"
    assert result.report.summary.record_id == "langgraph-record"
    assert result.dialogue is not None
    assert "AHI" in result.dialogue.assistant_response
    assert [step.step_name for step in result.steps] == [
        AgentStepName.SLEEP_ANALYSIS,
        AgentStepName.REPORT,
        AgentStepName.DIALOGUE,
    ]
    assert result.steps[-1].status == AgentStepStatus.COMPLETED
    assert result.generated_at == result.analysis.generated_at


def test_langgraph_boundary_skips_dialogue_without_question(monkeypatch) -> None:
    monkeypatch.setattr(
        graph_module,
        "_load_langgraph_symbols",
        _fake_langgraph_symbols,
    )

    result = run_sleep_agent_langgraph_orchestration(duration_hours=0.5, seed=32)

    assert result.dialogue is None
    assert result.steps[-1].step_name == AgentStepName.SKIP_DIALOGUE
    assert result.steps[-1].status == AgentStepStatus.SKIPPED


def test_langgraph_boundary_keeps_deepseek_default_off(monkeypatch) -> None:
    def fail_deepseek_call(*args, **kwargs):
        raise AssertionError("DeepSeek should remain opt-in for LangGraph boundary")

    monkeypatch.setattr(
        graph_module,
        "_load_langgraph_symbols",
        _fake_langgraph_symbols,
    )
    monkeypatch.setattr(
        "sleepagent.agents.report_agent.generate_sleep_report_with_deepseek_fallback",
        fail_deepseek_call,
    )

    result = run_sleep_agent_langgraph_orchestration(duration_hours=0.5, seed=33)

    assert result.report.elder_report
    assert "模拟睡眠报告" in result.report.elder_report
