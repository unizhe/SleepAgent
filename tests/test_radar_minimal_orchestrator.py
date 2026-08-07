from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sleepagent.radar_agent.agents import (
    AgentResult,
    ContextPacket,
    EvidencePacket,
    RadarAgentName,
    RadarDataAgent,
    TaskContext,
)
from sleepagent.radar_agent.orchestrator import (
    MINIMAL_MAIN_CHAIN,
    MinimalOrchestratorAgent,
    WorkflowNodeName,
)
from sleepagent.radar_agent.provider import ReplayRadarProvider
from sleepagent.radar_agent.schemas import (
    RadarDataQualityStatus,
    RadarNightSummary,
    RiskLevel,
)


CURRENT_NIGHT = date(2026, 7, 9)


def test_minimal_orchestrator_runs_ingest_to_risk_signal_chain() -> None:
    orchestrator = MinimalOrchestratorAgent(
        radar_data_agent=RadarDataAgent(
            ReplayRadarProvider(scenario="worsening_trend")
        )
    )
    context = _context(
        task_id="task-main-chain",
        historical_summaries=_historical_summaries(),
    )

    decision = orchestrator.run(context)

    assert decision.node == WorkflowNodeName.RISK_SIGNAL_ASSESSMENT
    assert [result.agent_name for result in decision.accepted_results] == [
        RadarAgentName.RADAR_DATA,
        RadarAgentName.TREND,
        RadarAgentName.RISK_SIGNAL,
    ]
    assert decision.evidence_ledger is not None
    assert decision.evidence_ledger.derived_metrics["workflow_nodes"] == [
        node.value for node in MINIMAL_MAIN_CHAIN
    ]
    assert decision.evidence_ledger.derived_metrics["risk_level"] == (
        RiskLevel.ESCALATE.value
    )
    assert decision.evidence_ledger.claims
    assert all(claim.evidence_refs for claim in decision.evidence_ledger.claims)
    assert decision.decision_summary.endswith("RiskSignalAssessment.")


def test_sub_agents_use_context_packet_and_structured_agent_result_contract() -> None:
    orchestrator = MinimalOrchestratorAgent(
        radar_data_agent=RadarDataAgent(ReplayRadarProvider(scenario="normal_night"))
    )
    decision = orchestrator.run(
        _context(
            task_id="task-contract",
            historical_summaries=_historical_summaries(),
        )
    )

    for result in decision.accepted_results:
        assert isinstance(result, AgentResult)
        assert isinstance(result.claims, list)
        assert isinstance(result.evidence_refs, list)
        assert isinstance(result.confidence, float)
        assert isinstance(result.uncertainties, list)
        assert isinstance(result.next_requests, list)
        assert isinstance(result.safety_flags, list)
        assert isinstance(result.candidate_actions, list)
        assert result.evidence_refs


def test_orchestrator_crops_minimum_necessary_context_for_each_agent() -> None:
    orchestrator = MinimalOrchestratorAgent(
        radar_data_agent=RadarDataAgent(ReplayRadarProvider(scenario="normal_night"))
    )
    context = _context(
        task_id="task-context-crop",
        historical_summaries=_historical_summaries(),
        text_inputs=["ordinary sleep question"],
    )

    orchestrator.run(context)
    radar_context = orchestrator.last_context_packets[WorkflowNodeName.INGEST_RADAR_DATA]
    trend_context = orchestrator.last_context_packets[WorkflowNodeName.TREND_ANALYSIS]
    risk_context = orchestrator.last_context_packets[
        WorkflowNodeName.RISK_SIGNAL_ASSESSMENT
    ]

    assert set(orchestrator.last_context_packets) == set(MINIMAL_MAIN_CHAIN)
    assert [
        orchestrator.last_context_packets[node].task_context.stage
        for node in MINIMAL_MAIN_CHAIN
    ] == [node.value for node in MINIMAL_MAIN_CHAIN]
    assert radar_context.evidence_packet.night_summaries == []
    assert set(radar_context.evidence_packet.data_quality) == {"night_of"}
    assert radar_context.task_context.allowed_actions == ["read_radar_provider"]
    assert trend_context.evidence_packet.night_summaries
    assert trend_context.evidence_packet.data_quality == {}
    assert trend_context.memory_snippets == []
    assert risk_context.evidence_packet.night_summaries
    assert set(risk_context.evidence_packet.data_quality) == {
        "trend_claims",
        "text_inputs",
    }
    assert risk_context.rag_context.snippets == []
    assert "ordinary sleep question" in risk_context.evidence_packet.data_quality["text_inputs"]


def test_sub_agents_do_not_publish_reports_write_memory_or_execute_high_risk_actions() -> None:
    orchestrator = MinimalOrchestratorAgent(
        radar_data_agent=RadarDataAgent(
            ReplayRadarProvider(scenario="worsening_trend")
        )
    )
    decision = orchestrator.run(
        _context(
            task_id="task-no-side-effects",
            historical_summaries=_historical_summaries(),
        )
    )
    actions = {
        action
        for result in decision.accepted_results
        for action in result.candidate_actions
    }

    assert "publish_final_report" not in actions
    assert "write_long_term_memory" not in actions
    assert "send_external_alert" not in actions
    assert all(
        "no_global_state_write" in result.safety_flags
        for result in decision.accepted_results
        if result.agent_name == RadarAgentName.RADAR_DATA
    )
    assert decision.confirmation_requests
    assert decision.confirmation_requests[0].status == "pending"


def _context(
    *,
    task_id: str,
    historical_summaries: list[RadarNightSummary],
    text_inputs: list[str] | None = None,
) -> ContextPacket:
    return ContextPacket(
        task_context=TaskContext(
            task_id=task_id,
            trace_id=f"trace-{task_id}",
            purpose="orchestration",
            allowed_actions=["run_minimal_chain"],
        ),
        evidence_packet=EvidencePacket(
            night_summaries=historical_summaries,
            data_quality={
                "night_of": CURRENT_NIGHT.isoformat(),
                "text_inputs": list(text_inputs or []),
            },
        ),
    )


def _historical_summaries() -> list[RadarNightSummary]:
    summaries: list[RadarNightSummary] = []
    baseline_start = CURRENT_NIGHT - timedelta(days=109)
    for offset in range(20):
        summaries.append(
            _summary(
                night=baseline_start + timedelta(days=offset),
                total_sleep_minutes=430,
                out_of_bed_count=1,
                movement_count=8,
                breath_rate=15,
                heart_rate=64,
                coverage=0.96,
            )
        )
    recent_start = CURRENT_NIGHT - timedelta(days=6)
    for offset in range(6):
        summaries.append(
            _summary(
                night=recent_start + timedelta(days=offset),
                total_sleep_minutes=340,
                out_of_bed_count=3,
                movement_count=24,
                breath_rate=18,
                heart_rate=74,
                coverage=0.91,
            )
        )
    return summaries


def _summary(
    *,
    night: date,
    total_sleep_minutes: float,
    out_of_bed_count: int,
    movement_count: int,
    breath_rate: float,
    heart_rate: float,
    coverage: float,
) -> RadarNightSummary:
    return RadarNightSummary(
        radar_device_id="radar-device-demo-001",
        subject_id="elder-demo-001",
        night_of=night,
        timezone_name="Asia/Shanghai",
        night_boundary_start_at=datetime.combine(
            night,
            datetime.min.time(),
            tzinfo=timezone.utc,
        ),
        night_boundary_end_at=datetime.combine(
            night + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ),
        total_sleep_minutes=total_sleep_minutes,
        out_of_bed_count=out_of_bed_count,
        movement_count=movement_count,
        data_coverage_ratio=coverage,
        data_quality_status=RadarDataQualityStatus.GOOD,
        explainable_metrics={
            "in_bed_minutes": total_sleep_minutes + 35,
            "mean_breath_rate_bpm": breath_rate,
            "mean_heart_rate_bpm": heart_rate,
            "coverage_ratio": coverage,
        },
        source_report_ref=f"night-summary:radar-device-demo-001:{night.isoformat()}",
    )
