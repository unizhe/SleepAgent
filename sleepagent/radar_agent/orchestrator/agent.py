from __future__ import annotations

from copy import deepcopy

from sleepagent.radar_agent.agents import (
    AgentResult,
    ContextPacket,
    EvidencePacket,
    RadarDataAgent,
    RiskSignalAgent,
    TaskContext,
    TrendAgent,
)
from sleepagent.radar_agent.a2a import A2AEventBus
from sleepagent.radar_agent.evidence import EvidenceLedgerBuilder
from sleepagent.radar_agent.orchestrator.conflicts import ConflictResolver
from sleepagent.radar_agent.orchestrator.contracts import (
    OrchestratorDecision,
    WorkflowNodeName,
)
from sleepagent.radar_agent.schemas import (
    A2AMessage,
    EvidenceClaim,
    EvidenceLedger,
    RadarAgentName,
    RadarNightSummary,
)


MINIMAL_MAIN_CHAIN: tuple[WorkflowNodeName, ...] = (
    WorkflowNodeName.INGEST_RADAR_DATA,
    WorkflowNodeName.NORMALIZE_CANONICAL_DATA,
    WorkflowNodeName.DATA_QUALITY_GATE,
    WorkflowNodeName.NIGHT_SUMMARY,
    WorkflowNodeName.TREND_ANALYSIS,
    WorkflowNodeName.RISK_SIGNAL_ASSESSMENT,
)


class MinimalOrchestratorAgent:
    """Single-task in-process orchestrator for the v1 radar main chain."""

    def __init__(
        self,
        *,
        radar_data_agent: RadarDataAgent,
        trend_agent: TrendAgent | None = None,
        risk_signal_agent: RiskSignalAgent | None = None,
        a2a_bus: A2AEventBus | None = None,
    ) -> None:
        self.radar_data_agent = radar_data_agent
        self.trend_agent = trend_agent or TrendAgent()
        self.risk_signal_agent = risk_signal_agent or RiskSignalAgent()
        self.a2a_bus = a2a_bus or A2AEventBus()
        self.last_context_packets: dict[WorkflowNodeName, ContextPacket] = {}

    def run(self, context: ContextPacket) -> OrchestratorDecision:
        self.last_context_packets = {}
        initial_context = deepcopy(context)
        accepted_results: list[AgentResult] = []

        for node in MINIMAL_MAIN_CHAIN[:4]:
            self.last_context_packets[node] = self._context_for_radar_data_node(
                initial_context,
                node,
            )
        radar_context = self.last_context_packets[WorkflowNodeName.INGEST_RADAR_DATA]
        radar_result = self.radar_data_agent.run(radar_context)
        accepted_results.append(radar_result)
        self._forward_next_requests(context.task_context.task_id, radar_result)
        night_summary = _night_summary_from_result(radar_result)
        trend_messages = self.a2a_bus.deliver_to(
            task_id=context.task_context.task_id,
            receiver=RadarAgentName.TREND.value,
        )

        trend_context = self._context_for_trend(
            initial_context=initial_context,
            night_summary=night_summary,
            a2a_messages=trend_messages,
        )
        self.last_context_packets[WorkflowNodeName.TREND_ANALYSIS] = trend_context
        trend_result = self.trend_agent.run(trend_context)
        self._mark_messages_handled(trend_messages)
        accepted_results.append(trend_result)
        self._forward_next_requests(context.task_context.task_id, trend_result)
        risk_messages = self.a2a_bus.deliver_to(
            task_id=context.task_context.task_id,
            receiver=RadarAgentName.RISK_SIGNAL.value,
        )

        risk_context = self._context_for_risk(
            initial_context=initial_context,
            night_summary=night_summary,
            trend_result=trend_result,
            a2a_messages=risk_messages,
        )
        self.last_context_packets[WorkflowNodeName.RISK_SIGNAL_ASSESSMENT] = risk_context
        risk_result = self.risk_signal_agent.run(risk_context)
        self._mark_messages_handled(risk_messages)
        accepted_results.append(risk_result)
        self._forward_next_requests(context.task_context.task_id, risk_result)
        evidence_ledger = _build_evidence_ledger(
            context=context,
            accepted_results=accepted_results,
            night_summary=night_summary,
        )
        resolution = ConflictResolver().resolve(
            task_id=context.task_context.task_id,
            accepted_results=accepted_results,
            evidence_ledger=evidence_ledger,
            night_summary=night_summary,
            safety_policy=context.safety_policy,
        )

        return OrchestratorDecision(
            task_id=context.task_context.task_id,
            node=WorkflowNodeName.RISK_SIGNAL_ASSESSMENT,
            accepted_results=accepted_results,
            a2a_messages=self.a2a_bus.list_messages(
                task_id=context.task_context.task_id
            ),
            conflicts=resolution.conflicts,
            confirmation_requests=_confirmation_requests_from_result(risk_result),
            evidence_ledger=resolution.evidence_ledger,
            decision_summary="Minimal radar main chain completed through RiskSignalAssessment.",
        )

    def _context_for_radar_data_node(
        self,
        context: ContextPacket,
        node: WorkflowNodeName,
    ) -> ContextPacket:
        return ContextPacket(
            task_context=_task_context(
                context,
                stage=node.value,
                purpose="analysis",
                allowed_actions=_allowed_actions_for_data_node(node),
            ),
            evidence_packet=EvidencePacket(
                data_quality={
                    key: value
                    for key, value in context.evidence_packet.data_quality.items()
                    if key in {"radar_device_id", "night_of"}
                },
            ),
            safety_policy=context.safety_policy,
        )

    def _context_for_trend(
        self,
        *,
        initial_context: ContextPacket,
        night_summary: RadarNightSummary,
        a2a_messages: list[A2AMessage],
    ) -> ContextPacket:
        summaries = [
            summary
            for summary in initial_context.evidence_packet.night_summaries
            if summary.night_of != night_summary.night_of
            or summary.radar_device_id != night_summary.radar_device_id
        ]
        summaries.append(night_summary)
        return ContextPacket(
            task_context=_task_context(
                initial_context,
                stage=WorkflowNodeName.TREND_ANALYSIS.value,
                purpose="analysis",
                allowed_actions=["compute_trend_metrics"],
            ),
            evidence_packet=EvidencePacket(night_summaries=summaries),
            safety_policy=initial_context.safety_policy,
            a2a_messages=a2a_messages,
        )

    def _context_for_risk(
        self,
        *,
        initial_context: ContextPacket,
        night_summary: RadarNightSummary,
        trend_result: AgentResult,
        a2a_messages: list[A2AMessage],
    ) -> ContextPacket:
        return ContextPacket(
            task_context=_task_context(
                initial_context,
                stage=WorkflowNodeName.RISK_SIGNAL_ASSESSMENT.value,
                purpose="risk",
                allowed_actions=["classify_risk_signal", "request_confirmation_candidate"],
            ),
            evidence_packet=EvidencePacket(
                night_summaries=[night_summary],
                data_quality={
                    "trend_claims": [
                        claim.model_dump(mode="json") for claim in trend_result.claims
                    ],
                    "text_inputs": _text_inputs(initial_context),
                },
            ),
            safety_policy=initial_context.safety_policy,
            a2a_messages=a2a_messages,
        )

    def _forward_next_requests(self, task_id: str, result: AgentResult) -> None:
        for request in result.next_requests:
            if request.task_id != task_id:
                raise ValueError("A2A request task_id does not match current task")
            routed = request.model_copy(update={"routed_by": RadarAgentName.ORCHESTRATOR.value})
            self.a2a_bus.forward_from_orchestrator(
                routed,
                approved_by_orchestrator=True,
            )

    def _mark_messages_handled(self, messages: list[A2AMessage]) -> None:
        for message in messages:
            self.a2a_bus.mark_handled(message)


def _task_context(
    context: ContextPacket,
    *,
    stage: str,
    purpose: str,
    allowed_actions: list[str],
) -> TaskContext:
    return context.task_context.model_copy(
        update={
            "stage": stage,
            "purpose": purpose,
            "allowed_actions": allowed_actions,
        }
    )


def _allowed_actions_for_data_node(node: WorkflowNodeName) -> list[str]:
    if node == WorkflowNodeName.INGEST_RADAR_DATA:
        return ["read_radar_provider"]
    if node == WorkflowNodeName.NORMALIZE_CANONICAL_DATA:
        return ["normalize_canonical_data"]
    if node == WorkflowNodeName.DATA_QUALITY_GATE:
        return ["quality_gate"]
    if node == WorkflowNodeName.NIGHT_SUMMARY:
        return ["build_night_summary"]
    return []


def _night_summary_from_result(result: AgentResult) -> RadarNightSummary:
    payload = result.output_payload.get("night_summary")
    if payload is None:
        raise ValueError("RadarDataAgent result does not contain night_summary.")
    return RadarNightSummary.model_validate(payload)


def _confirmation_requests_from_result(result: AgentResult):
    payload = result.output_payload.get("risk_signal", {})
    return payload.get("confirmation_requests", [])


def _build_evidence_ledger(
    *,
    context: ContextPacket,
    accepted_results: list[AgentResult],
    night_summary: RadarNightSummary,
) -> EvidenceLedger:
    claims: list[EvidenceClaim] = [
        claim for result in accepted_results for claim in result.claims
    ]
    refs = _dedupe(ref for result in accepted_results for ref in result.evidence_refs)
    builder = EvidenceLedgerBuilder(
        ledger_id=f"ledger:{context.task_context.task_id}",
        task_id=context.task_context.task_id,
    )
    for ref in refs:
        builder.add_canonical_ref(ref)
    for entry in context.evidence_packet.questionnaire_entries:
        builder.add_questionnaire_entry(entry)
    for document in context.evidence_packet.supplementary_documents:
        builder.add_supplementary_document(document)
    builder.add_metric("workflow_nodes", [node.value for node in MINIMAL_MAIN_CHAIN])
    builder.add_metric("risk_level", _risk_level_from_results(accepted_results))
    builder.add_metric("data_quality_status", night_summary.data_quality_status.value)
    builder.add_metric("data_coverage_ratio", night_summary.data_coverage_ratio)
    for claim in claims:
        builder.add_claim(claim)
        for caveat in claim.caveats:
            builder.add_caveat(caveat)
    if not night_summary.health_conclusion_allowed:
        builder.mark_uninterpretable_scope(
            scopes=["sleep_health_conclusion", "risk_signal"],
            reason="; ".join(night_summary.blocked_reasons)
            or night_summary.data_quality_status.value,
            evidence_refs=[
                ref for ref in refs if ref.startswith("night-summary:")
            ][:1]
            or refs[:1],
        )
    return builder.build()


def _risk_level_from_results(results: list[AgentResult]) -> str | None:
    for result in reversed(results):
        if result.agent_name != RadarAgentName.RISK_SIGNAL:
            continue
        payload = result.output_payload.get("risk_signal", {})
        risk_level = payload.get("risk_level")
        return str(risk_level) if risk_level else None
    return None


def _text_inputs(context: ContextPacket) -> list[str]:
    values = context.evidence_packet.data_quality.get("text_inputs", [])
    if isinstance(values, str):
        return [values]
    if isinstance(values, list):
        return [str(value) for value in values if value is not None]
    return []


def _dedupe(items) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item in seen:
            continue
        deduped.append(item)
        seen.add(item)
    return deduped


__all__ = ["MINIMAL_MAIN_CHAIN", "MinimalOrchestratorAgent"]
