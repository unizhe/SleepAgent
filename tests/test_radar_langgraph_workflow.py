from __future__ import annotations

from datetime import date
import sqlite3

import pytest

from sleepagent.radar_agent.agents import (
    AgentResult,
    AlertCareAgent,
    ContextPacket,
    EvidencePacket,
    RAGAgent,
    RadarDataAgent,
    TaskContext,
    TrendAgent,
)
from sleepagent.radar_agent.orchestrator import (
    InMemoryRadarCheckpointStore,
    RadarLangGraphWorkflow,
    RadarNodeAuthorizationError,
    RadarNodeAuthorityGuard,
    RadarNodeExecutionError,
    WORKFLOW_NODE_ORDER,
    WorkflowNodeName,
)
from sleepagent.radar_agent.orchestrator import langgraph as graph_module
from sleepagent.radar_agent.provider import ReplayRadarProvider
from sleepagent.radar_agent.persistence import RadarPersistenceStore, RadarSubject
from sleepagent.radar_agent.runtime import RadarNodeStatus, RadarTaskStatus, TaskService
from sleepagent.radar_agent.schemas import A2AMessage, RadarAgentName, RiskLevel


class FakeCompiledGraph:
    def __init__(self, graph: "FakeStateGraph") -> None:
        self.graph = graph

    def invoke(self, state: dict) -> dict:
        current = self.graph.entry_point
        while current != self.graph.end:
            patch = self.graph.nodes[current](state)
            state = {**state, **patch}
            current = self.graph.next_node(current)
        return state


class FakeStateGraph:
    end = "__end__"

    def __init__(self, state_schema: object) -> None:
        self.state_schema = state_schema
        self.nodes: dict[str, object] = {}
        self.edges: list[tuple[str, str]] = []
        self.entry_point: str | None = None

    def add_node(self, name: str, node) -> None:
        self.nodes[name] = node

    def set_entry_point(self, name: str) -> None:
        self.entry_point = name

    def add_edge(self, source: str, target: str) -> None:
        self.edges.append((source, target))

    def next_node(self, source: str) -> str:
        return next(target for edge_source, target in self.edges if edge_source == source)

    def compile(self) -> FakeCompiledGraph:
        return FakeCompiledGraph(self)


def _fake_symbols():
    return FakeStateGraph, FakeStateGraph.end


@pytest.fixture(autouse=True)
def fake_langgraph(monkeypatch):
    monkeypatch.setattr(graph_module, "_load_langgraph_symbols", _fake_symbols)


def test_graph_has_exact_14_node_order_and_required_safety_positions() -> None:
    workflow = _workflow()
    compiled = workflow.build()
    names = [node.value for node in WORKFLOW_NODE_ORDER]

    assert list(compiled.graph.nodes) == names
    assert compiled.graph.entry_point == names[0]
    assert compiled.graph.edges == [
        *list(zip(names, names[1:])),
        (names[-1], FakeStateGraph.end),
    ]
    assert names.index("DataQualityGate") < names.index("RiskSignalAssessment")
    assert names.index("EvidenceLedgerReview") < names.index("RoleReportGeneration")
    assert names.index("RoleReportGeneration") < names.index("AlertDecision") < names.index("PublishArtifacts")


def test_graph_executes_all_nodes_and_persists_exact_trace() -> None:
    checkpoints = InMemoryRadarCheckpointStore()
    workflow = _workflow(checkpoint_store=checkpoints)

    decision = workflow.run(_context(), run_id="graph-success")
    checkpoint = checkpoints.load("graph-success")

    assert decision.node == WorkflowNodeName.PUBLISH_ARTIFACTS
    assert checkpoint is not None
    assert checkpoint.failed_node is None
    assert checkpoint.state["completed_nodes"] == list(WORKFLOW_NODE_ORDER)
    assert checkpoint.state["node_trace"] == [node.value for node in WORKFLOW_NODE_ORDER]
    assert checkpoint.state["a2a_rounds"] <= 2
    assert [result.agent_name for result in decision.accepted_results] == [
        RadarAgentName.RADAR_DATA,
        RadarAgentName.TREND,
        RadarAgentName.RISK_SIGNAL,
        RadarAgentName.RAG,
        RadarAgentName.REPORT,
        RadarAgentName.ALERT_CARE,
        RadarAgentName.MEMORY,
    ]


def test_failed_node_resumes_from_last_successful_checkpoint() -> None:
    checkpoints = InMemoryRadarCheckpointStore()
    flaky = FlakyRAGAgent()
    workflow = _workflow(rag_agent=flaky, checkpoint_store=checkpoints)

    with pytest.raises(RadarNodeExecutionError) as error:
        workflow.run(_context(), run_id="graph-recovery")
    failed = checkpoints.load("graph-recovery")

    assert error.value.node == WorkflowNodeName.RAG_GROUNDING
    assert failed is not None
    assert failed.failed_node == WorkflowNodeName.RAG_GROUNDING
    assert failed.state["completed_nodes"][-1] == WorkflowNodeName.EVIDENCE_LEDGER_REVIEW

    decision = workflow.run(_context(), run_id="graph-recovery", resume=True)
    recovered = checkpoints.load("graph-recovery")

    assert decision.node == WorkflowNodeName.PUBLISH_ARTIFACTS
    assert flaky.calls == 2
    assert recovered is not None
    assert recovered.failed_node is None
    assert recovered.state["completed_nodes"] == list(WORKFLOW_NODE_ORDER)
    assert recovered.state["node_trace"] == [node.value for node in WORKFLOW_NODE_ORDER]


def test_task_service_persists_failed_node_and_retry_resumes_graph() -> None:
    provider = ReplayRadarProvider(scenario="normal_night")
    radar_agent = CountingRadarDataAgent(provider)
    workflow = RadarLangGraphWorkflow(
        radar_data_agent=radar_agent,
        rag_agent=FlakyRAGAgent(),
    )
    service = _task_service(provider)
    task = service.create_task(
        subject_id="elder-001",
        radar_device_id=provider.list_devices()[0].radar_device_id,
        scenario="normal_night",
    )
    context = _context(task_id=task.task_id, trace_id=task.trace_id)

    with pytest.raises(RadarNodeExecutionError):
        service.execute(task.task_id, workflow, context)
    failed = service.get_task(task.task_id)

    assert failed.status == RadarTaskStatus.FAILED
    assert failed.failure is not None
    assert failed.failure.failed_node == WorkflowNodeName.RAG_GROUNDING.value
    assert failed.node_status[WorkflowNodeName.EVIDENCE_LEDGER_REVIEW.value] == RadarNodeStatus.SUCCEEDED
    assert failed.node_status[WorkflowNodeName.RAG_GROUNDING.value] == RadarNodeStatus.FAILED

    service.retry_failed_task(task.task_id)
    decision = service.execute(task.task_id, workflow, context)
    recovered = service.get_task(task.task_id)

    assert decision.node == WorkflowNodeName.PUBLISH_ARTIFACTS
    assert radar_agent.ingest_calls == 1
    assert all(status == RadarNodeStatus.SUCCEEDED for status in recovered.node_status.values())


def test_guard_rejects_skipping_quality_and_privileged_publish() -> None:
    guard = RadarNodeAuthorityGuard(max_a2a_rounds=2)

    with pytest.raises(RadarNodeAuthorizationError, match="out of order"):
        guard.authorize(
            node=WorkflowNodeName.RISK_SIGNAL_ASSESSMENT,
            actor=RadarAgentName.RISK_SIGNAL,
            completed_nodes=[
                WorkflowNodeName.INGEST_RADAR_DATA,
                WorkflowNodeName.NORMALIZE_CANONICAL_DATA,
            ],
        )
    with pytest.raises(RadarNodeAuthorizationError, match="cannot execute"):
        guard.authorize(
            node=WorkflowNodeName.PUBLISH_ARTIFACTS,
            actor=RadarAgentName.REPORT,
            completed_nodes=list(WORKFLOW_NODE_ORDER[:-1]),
        )


def test_a2a_is_limited_to_one_or_two_rounds() -> None:
    with pytest.raises(ValueError, match="one or two"):
        RadarNodeAuthorityGuard(max_a2a_rounds=3)

    workflow = _workflow(
        trend_agent=RoundTwoTrendAgent(),
        max_a2a_rounds=1,
    )
    with pytest.raises(RadarNodeExecutionError) as error:
        workflow.run(_context(), run_id="graph-a2a-limit")

    assert error.value.node == WorkflowNodeName.A2A_COLLABORATION_ROUND
    assert isinstance(error.value.cause, RadarNodeAuthorizationError)


def test_alert_agent_cannot_execute_external_action_before_publish() -> None:
    workflow = _workflow(alert_care_agent=OverreachingAlertAgent())

    with pytest.raises(RadarNodeExecutionError) as error:
        workflow.run(_context(), run_id="graph-alert-overreach")

    assert error.value.node == WorkflowNodeName.ALERT_DECISION
    assert isinstance(error.value.cause, RadarNodeAuthorizationError)


class FlakyRAGAgent(RAGAgent):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def run(self, context: ContextPacket) -> AgentResult:
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("temporary RAG timeout")
        return super().run(context)


class CountingRadarDataAgent(RadarDataAgent):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.ingest_calls = 0

    def ingest(self, context: ContextPacket):
        self.ingest_calls += 1
        return super().ingest(context)


class RoundTwoTrendAgent(TrendAgent):
    def run(self, context: ContextPacket) -> AgentResult:
        result = super().run(context)
        request = A2AMessage(
            message_id="a2a-round-two",
            sender=RadarAgentName.TREND.value,
            receiver=RadarAgentName.RISK_SIGNAL.value,
            task_id=context.task_context.task_id,
            intent="request_risk_review",
            collaboration_round=2,
            risk_level=RiskLevel.WATCH,
        )
        return result.model_copy(update={"next_requests": [request]})


class OverreachingAlertAgent(AlertCareAgent):
    def run(self, context: ContextPacket) -> AgentResult:
        result = super().run(context)
        payload = dict(result.output_payload)
        payload["alert_care"] = {
            **payload["alert_care"],
            "external_action_executed": True,
        }
        return result.model_copy(update={"output_payload": payload})


def _workflow(**kwargs) -> RadarLangGraphWorkflow:
    return RadarLangGraphWorkflow(
        radar_data_agent=RadarDataAgent(ReplayRadarProvider(scenario="normal_night")),
        **kwargs,
    )


def _context(
    *,
    task_id: str = "task-langgraph",
    trace_id: str = "trace-langgraph",
) -> ContextPacket:
    return ContextPacket(
        task_context=TaskContext(
            task_id=task_id,
            trace_id=trace_id,
            role="family",
            purpose="orchestration",
            allowed_actions=["run_full_chain"],
        ),
        evidence_packet=EvidencePacket(
            data_quality={"night_of": date(2026, 7, 10).isoformat()}
        ),
    )


def _task_service(provider: ReplayRadarProvider) -> TaskService:
    store = RadarPersistenceStore.connect_sqlite(sqlite3.connect(":memory:"))
    store.save_subject(RadarSubject(subject_id="elder-001", display_name="Test elder"))
    device = provider.list_devices()[0].model_copy(update={"bound_subject_id": "elder-001"})
    store.save_device(device)
    return TaskService(store, require_authorization=False)
