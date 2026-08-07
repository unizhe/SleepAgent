from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("langgraph")

from sleepagent.radar_agent.agents import ContextPacket, EvidencePacket, RadarDataAgent, TaskContext
from sleepagent.radar_agent.orchestrator import RadarLangGraphWorkflow, WORKFLOW_NODE_ORDER
from sleepagent.radar_agent.provider import ReplayRadarProvider


def test_real_langgraph_executes_the_complete_radar_workflow() -> None:
    workflow = RadarLangGraphWorkflow(
        radar_data_agent=RadarDataAgent(ReplayRadarProvider(scenario="normal_night"))
    )
    decision = workflow.run(_context(), run_id="real-radar-langgraph")
    checkpoint = workflow.checkpoint_store.load("real-radar-langgraph")

    assert workflow.build().__class__.__module__.startswith("langgraph.")
    assert checkpoint is not None
    assert checkpoint.state["completed_nodes"] == list(WORKFLOW_NODE_ORDER)
    assert checkpoint.state["node_trace"] == [node.value for node in WORKFLOW_NODE_ORDER]
    assert decision.decision_summary == "Radar LangGraph completed all 14 authorized nodes."


def _context() -> ContextPacket:
    return ContextPacket(
        task_context=TaskContext(
            task_id="task-real-langgraph",
            trace_id="trace-real-langgraph",
            role="family",
            purpose="orchestration",
            allowed_actions=["run_full_chain"],
        ),
        evidence_packet=EvidencePacket(
            data_quality={"night_of": date(2026, 7, 10).isoformat()}
        ),
    )
