from __future__ import annotations

from datetime import date

from sleepagent.radar_agent.agents import ContextPacket, EvidencePacket, RadarDataAgent, TaskContext
from sleepagent.radar_agent.orchestrator import (
    FullOrchestratorAgent,
    WORKFLOW_NODE_ORDER,
    WorkflowNodeName,
)
from sleepagent.radar_agent.provider import ReplayRadarProvider
from sleepagent.radar_agent.schemas import RadarAgentName


def test_full_orchestrator_runs_all_capability_agents_to_publish_boundary() -> None:
    orchestrator = FullOrchestratorAgent(
        radar_data_agent=RadarDataAgent(ReplayRadarProvider(scenario="normal_night"))
    )
    decision = orchestrator.run(_context())

    assert decision.node == WorkflowNodeName.PUBLISH_ARTIFACTS
    assert [result.agent_name for result in decision.accepted_results] == [
        RadarAgentName.RADAR_DATA,
        RadarAgentName.TREND,
        RadarAgentName.RISK_SIGNAL,
        RadarAgentName.RAG,
        RadarAgentName.REPORT,
        RadarAgentName.ALERT_CARE,
        RadarAgentName.MEMORY,
    ]
    assert decision.evidence_ledger is not None
    assert RadarAgentName.ORCHESTRATOR in orchestrator.last_prompt_bundles
    assert set(orchestrator.last_prompt_bundles) == {
        RadarAgentName.ORCHESTRATOR,
        RadarAgentName.RADAR_DATA,
        RadarAgentName.TREND,
        RadarAgentName.RISK_SIGNAL,
        RadarAgentName.RAG,
        RadarAgentName.REPORT,
        RadarAgentName.ALERT_CARE,
        RadarAgentName.MEMORY,
    }
    assert all(len(bundle.messages) == 3 for bundle in orchestrator.last_prompt_bundles.values())
    assert set(orchestrator.last_context_packets) == set(WORKFLOW_NODE_ORDER)
    assert all(
        not result.output_payload.get("external_action_executed", False)
        for result in decision.accepted_results
    )


def test_full_orchestrator_dialogue_path_cannot_mutate_facts() -> None:
    orchestrator = FullOrchestratorAgent(
        radar_data_agent=RadarDataAgent(ReplayRadarProvider(scenario="normal_night"))
    )
    context = _context()
    decision = orchestrator.run(context)
    result = orchestrator.answer(context, decision.evidence_ledger)

    assert result.agent_name == RadarAgentName.DIALOGUE
    assert result.claims == []
    assert "evidence_ledger" not in result.output_payload
    assert RadarAgentName.DIALOGUE in orchestrator.last_prompt_bundles


def _context() -> ContextPacket:
    return ContextPacket(
        task_context=TaskContext(
            task_id="task-full",
            trace_id="trace-full",
            role="family",
            purpose="orchestration",
            allowed_actions=["run_full_chain"],
        ),
        evidence_packet=EvidencePacket(
            data_quality={
                "night_of": date(2026, 7, 10).isoformat(),
                "user_question": "昨晚怎么样？",
            }
        ),
    )
