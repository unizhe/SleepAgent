from __future__ import annotations

from copy import deepcopy

from sleepagent.radar_agent.agents import (
    AgentResult,
    AlertCareAgent,
    ContextPacket,
    DialogueAgent,
    EvidencePacket,
    MemoryAgent,
    RAGAgent,
    RadarDataAgent,
    ReportAgent,
    RiskSignalAgent,
    TrendAgent,
)
from sleepagent.radar_agent.orchestrator.agent import MinimalOrchestratorAgent
from sleepagent.radar_agent.orchestrator.conflicts import validate_expression_agent_output
from sleepagent.radar_agent.orchestrator.contracts import OrchestratorDecision, WorkflowNodeName
from sleepagent.radar_agent.prompts import PromptBundle, build_prompt_bundle
from sleepagent.radar_agent.schemas import (
    A2AMessage,
    EvidenceLedger,
    HumanConfirmationRequest,
    MemoryCandidate,
    RadarAgentName,
    RagContext,
)
from sleepagent.radar_agent.memory import (
    MemoryWriteDecision,
    OrchestratedMemoryWriter,
    ShortTermDialogueTurn,
    ShortTermMemoryContext,
    build_short_term_memory,
)
from sleepagent.radar_agent.skills import SKILL_BY_AGENT
from sleepagent.radar_agent.questionnaire import (
    QuestionnaireCandidate,
    QuestionnaireService,
    QuestionnaireTrigger,
)


class FullOrchestratorAgent:
    """Complete single-task v1 workflow; lifecycle and persistence stay in TaskService."""

    name = RadarAgentName.ORCHESTRATOR

    def __init__(
        self,
        *,
        radar_data_agent: RadarDataAgent,
        trend_agent: TrendAgent | None = None,
        risk_signal_agent: RiskSignalAgent | None = None,
        rag_agent: RAGAgent | None = None,
        report_agent: ReportAgent | None = None,
        dialogue_agent: DialogueAgent | None = None,
        alert_care_agent: AlertCareAgent | None = None,
        memory_agent: MemoryAgent | None = None,
    ) -> None:
        self.minimal = MinimalOrchestratorAgent(
            radar_data_agent=radar_data_agent,
            trend_agent=trend_agent,
            risk_signal_agent=risk_signal_agent,
        )
        self.rag_agent = rag_agent or RAGAgent()
        self.report_agent = report_agent or ReportAgent()
        self.dialogue_agent = dialogue_agent or DialogueAgent()
        self.alert_care_agent = alert_care_agent or AlertCareAgent()
        self.memory_agent = memory_agent or MemoryAgent()
        self.last_context_packets: dict[WorkflowNodeName, ContextPacket] = {}
        self.last_prompt_bundles: dict[RadarAgentName, PromptBundle] = {}
        self.last_short_term_memory: ShortTermMemoryContext | None = None

    def run(self, context: ContextPacket) -> OrchestratorDecision:
        self.last_context_packets = {}
        self.last_short_term_memory = None
        self.last_prompt_bundles = {
            self.name: build_prompt_bundle(SKILL_BY_AGENT[self.name], context)
        }
        base = self.minimal.run(context)
        if base.evidence_ledger is None:
            raise ValueError("Minimal orchestrator did not produce an EvidenceLedger.")
        self.last_context_packets.update(self.minimal.last_context_packets)
        base_nodes = {
            RadarAgentName.RADAR_DATA: WorkflowNodeName.INGEST_RADAR_DATA,
            RadarAgentName.TREND: WorkflowNodeName.TREND_ANALYSIS,
            RadarAgentName.RISK_SIGNAL: WorkflowNodeName.RISK_SIGNAL_ASSESSMENT,
        }
        results: list[AgentResult] = []
        for result in base.accepted_results:
            skill = SKILL_BY_AGENT[result.agent_name]
            node_context = self.minimal.last_context_packets[base_nodes[result.agent_name]]
            self.last_prompt_bundles[result.agent_name] = build_prompt_bundle(skill, node_context)
            results.append(result.model_copy(update={"skill_version": skill.version, "prompt_version": skill.version}))
        latest_summary = _latest_summary(results)
        self.last_context_packets[WorkflowNodeName.EVIDENCE_LEDGER_REVIEW] = _downstream_context(
            context,
            stage=WorkflowNodeName.EVIDENCE_LEDGER_REVIEW,
            purpose="orchestration",
            ledger=base.evidence_ledger,
            latest_summary=latest_summary,
        )

        rag_context = _downstream_context(
            context,
            stage=WorkflowNodeName.RAG_GROUNDING,
            purpose="rag",
            ledger=base.evidence_ledger,
            latest_summary=latest_summary,
            data_quality={
                "rag_query": _rag_query(base.evidence_ledger),
                "rag_roles": ["elder", "family", "doctor"],
            },
        )
        rag_result = self._run(self.rag_agent, rag_context)
        results.append(rag_result)
        ledger = _ground_ledger(base.evidence_ledger, rag_result)
        questionnaire_candidates = _select_questionnaire_candidates(
            context,
            ledger,
            latest_summary,
        )
        self.last_context_packets[WorkflowNodeName.A2A_COLLABORATION_ROUND] = _downstream_context(
            context,
            stage=WorkflowNodeName.A2A_COLLABORATION_ROUND,
            purpose="orchestration",
            ledger=ledger,
            latest_summary=latest_summary,
            rag_context=_rag_context(rag_result),
        )

        report_context = _downstream_context(
            context,
            stage=WorkflowNodeName.ROLE_REPORT_GENERATION,
            purpose="report",
            ledger=ledger,
            latest_summary=latest_summary,
            rag_context=_rag_context(rag_result),
        )
        report_result = self._run(self.report_agent, report_context)
        validate_expression_agent_output(
            agent_name=RadarAgentName.REPORT,
            evidence_ledger=ledger,
            result=report_result,
        )
        results.append(report_result)
        unavailable_messages = []
        if "agent_unavailable" in report_result.safety_flags:
            unavailable_messages.append(
                _agent_unavailable_message(
                    task_id=context.task_context.task_id,
                    agent_name=RadarAgentName.REPORT,
                    evidence_refs=report_result.evidence_refs,
                )
            )

        alert_context = _downstream_context(
            context,
            stage=WorkflowNodeName.ALERT_DECISION,
            purpose="alert",
            ledger=ledger,
            latest_summary=latest_summary,
        )
        alert_result = self._run(self.alert_care_agent, alert_context)
        results.append(alert_result)

        memory_context = _downstream_context(
            context,
            stage=WorkflowNodeName.MEMORY_CANDIDATE,
            purpose="memory",
            ledger=ledger,
            latest_summary=latest_summary,
            data_quality=_memory_data(context, results, report_result),
        )
        self.last_short_term_memory = build_short_term_memory(memory_context)
        memory_result = self._run(self.memory_agent, memory_context)
        results.append(memory_result)
        confirmations = _confirmations(base, alert_result, memory_result)
        self.last_context_packets[WorkflowNodeName.HUMAN_CONFIRMATION] = _downstream_context(
            context,
            stage=WorkflowNodeName.HUMAN_CONFIRMATION,
            purpose="orchestration",
            ledger=ledger,
            latest_summary=latest_summary,
            data_quality={
                "confirmation_ids": [item.confirmation_id for item in confirmations]
            },
        )
        self.last_context_packets[WorkflowNodeName.PUBLISH_ARTIFACTS] = _downstream_context(
            context,
            stage=WorkflowNodeName.PUBLISH_ARTIFACTS,
            purpose="orchestration",
            ledger=ledger,
            latest_summary=latest_summary,
            data_quality={"publish_boundary": "orchestrator_only"},
        )
        return OrchestratorDecision(
            task_id=context.task_context.task_id,
            node=WorkflowNodeName.PUBLISH_ARTIFACTS,
            accepted_results=results,
            a2a_messages=[*base.a2a_messages, *unavailable_messages],
            conflicts=base.conflicts,
            confirmation_requests=confirmations,
            questionnaire_candidates=questionnaire_candidates,
            evidence_ledger=ledger,
            decision_summary="Full radar v1 workflow completed with candidate-only alerts and memory.",
        )

    def answer(self, context: ContextPacket, ledger: EvidenceLedger) -> AgentResult:
        dialogue_context = _downstream_context(
            context,
            stage=WorkflowNodeName.PUBLISH_ARTIFACTS,
            purpose="chat",
            ledger=ledger,
            latest_summary=context.evidence_packet.night_summaries[-1] if context.evidence_packet.night_summaries else None,
            data_quality=context.evidence_packet.data_quality,
            rag_context=context.rag_context,
        )
        result = self._run(self.dialogue_agent, dialogue_context)
        validated = validate_expression_agent_output(
            agent_name=RadarAgentName.DIALOGUE,
            evidence_ledger=ledger,
            result=result,
        )
        response = validated.output_payload.get("dialogue", {})
        turns = list(
            self.last_short_term_memory.dialogue_turns
            if self.last_short_term_memory is not None
            else []
        )
        turns.extend(
            [
                ShortTermDialogueTurn(
                    role="user",
                    content=str(
                        context.evidence_packet.data_quality.get(
                            "user_question", "请解释当前报告。"
                        )
                    ),
                    sensitive=bool(
                        context.evidence_packet.data_quality.get(
                            "dialogue_sensitive", False
                        )
                    ),
                ),
                ShortTermDialogueTurn(
                    role="assistant",
                    content=str(response.get("answer", "当前无法生成解释。")),
                ),
            ]
        )
        self.last_short_term_memory = build_short_term_memory(
            dialogue_context,
            dialogue_turns=turns,
            current_report_artifact=(
                self.last_short_term_memory.current_report_artifact
                if self.last_short_term_memory is not None
                else None
            ),
        )
        return validated

    def review_memory_write(
        self,
        candidate: MemoryCandidate,
        confirmation: HumanConfirmationRequest | None,
        *,
        writer: OrchestratedMemoryWriter | None = None,
    ) -> MemoryWriteDecision:
        """Apply Orchestrator-owned privacy and confirmation policy."""

        return (writer or OrchestratedMemoryWriter()).decide(
            candidate,
            confirmation,
            actor="orchestrator",
        )

    def _run(self, agent, context: ContextPacket) -> AgentResult:
        self.last_context_packets[WorkflowNodeName(context.task_context.stage)] = context
        skill = SKILL_BY_AGENT[agent.name]
        self.last_prompt_bundles[agent.name] = build_prompt_bundle(skill, context)
        result = agent.run(context)
        return result.model_copy(update={"skill_version": skill.version, "prompt_version": skill.version})


def _downstream_context(
    context: ContextPacket,
    *,
    stage: WorkflowNodeName,
    purpose: str,
    ledger: EvidenceLedger,
    latest_summary,
    data_quality: dict | None = None,
    rag_context: RagContext | None = None,
) -> ContextPacket:
    packet = deepcopy(context)
    return ContextPacket(
        context_packet_id=f"context:{context.task_context.task_id}:{stage.value}",
        task_context=packet.task_context.model_copy(
            update={"stage": stage.value, "purpose": purpose, "allowed_actions": _allowed(stage)}
        ),
        evidence_packet=EvidencePacket(
            evidence_refs=list(ledger.canonical_evidence_refs),
            night_summaries=[latest_summary] if latest_summary is not None else [],
            data_quality=dict(data_quality or {}),
            claim_refs=[claim.claim_id for claim in ledger.claims],
            evidence_ledger=ledger,
        ),
        memory_snippets=packet.memory_snippets,
        rag_context=rag_context or RagContext(),
        safety_policy=packet.safety_policy,
    )


def _allowed(stage: WorkflowNodeName) -> list[str]:
    return {
        WorkflowNodeName.EVIDENCE_LEDGER_REVIEW: ["review_evidence_ledger"],
        WorkflowNodeName.RAG_GROUNDING: ["retrieve_reviewed_seed"],
        WorkflowNodeName.A2A_COLLABORATION_ROUND: ["route_a2a_messages"],
        WorkflowNodeName.ROLE_REPORT_GENERATION: ["generate_role_reports"],
        WorkflowNodeName.ALERT_DECISION: ["propose_care_action"],
        WorkflowNodeName.MEMORY_CANDIDATE: ["propose_memory_candidate"],
        WorkflowNodeName.HUMAN_CONFIRMATION: ["collect_confirmation_candidates"],
        WorkflowNodeName.PUBLISH_ARTIFACTS: ["answer_from_ledger"],
    }.get(stage, [])


def _latest_summary(results: list[AgentResult]):
    for result in results:
        payload = result.output_payload.get("night_summary")
        if payload:
            from sleepagent.radar_agent.schemas import RadarNightSummary
            return RadarNightSummary.model_validate(payload)
    return None


def _rag_query(ledger: EvidenceLedger) -> str:
    return " ".join(claim.text for claim in ledger.claims) + " data quality role"


def _rag_context(result: AgentResult) -> RagContext:
    return RagContext.model_validate(result.output_payload.get("rag_context", {}))


def _ground_ledger(ledger: EvidenceLedger, result: AgentResult) -> EvidenceLedger:
    return ledger.model_copy(
        update={
            "canonical_evidence_refs": list(dict.fromkeys(ledger.canonical_evidence_refs + result.evidence_refs)),
            "caveats": list(dict.fromkeys(ledger.caveats + result.output_payload.get("rag_context", {}).get("caveats", []))),
        }
    )


def _memory_data(
    context: ContextPacket,
    results: list[AgentResult],
    report_result: AgentResult,
) -> dict:
    data = dict(context.evidence_packet.data_quality)
    trend = next(
        (result for result in results if result.agent_name == RadarAgentName.TREND),
        None,
    )
    if trend is not None:
        data["trend_result"] = trend.output_payload.get("trend_result", {})
    reports = report_result.output_payload.get("reports", [])
    if reports:
        data["current_report_artifact"] = next(
            (
                report
                for report in reports
                if report.get("role") == context.task_context.role
            ),
            reports[0],
        )
    return data


def _confirmations(base: OrchestratorDecision, *results: AgentResult) -> list[HumanConfirmationRequest]:
    values = list(base.confirmation_requests)
    for result in results:
        for key in ("alert_care", "memory_proposal"):
            payload = result.output_payload.get(key, {})
            for item in payload.get("confirmation_requests", []):
                values.append(HumanConfirmationRequest.model_validate(item))
        proposal = result.output_payload.get("memory_proposal", {})
        if (
            result.agent_name == RadarAgentName.MEMORY
            and proposal.get("candidates")
            and not proposal.get("confirmation_requests")
        ):
            values.append(HumanConfirmationRequest(
                confirmation_id=f"confirm:{base.task_id}:write_long_term_memory",
                task_id=base.task_id,
                action_type="write_long_term_memory",
                requested_role="family",
                reason="Long-term memory write requires family confirmation.",
                evidence_refs=result.evidence_refs,
            ))
    return list({item.confirmation_id: item for item in values}.values())


def _agent_unavailable_message(
    *,
    task_id: str,
    agent_name: RadarAgentName,
    evidence_refs: list[str],
) -> A2AMessage:
    """Auditable fail-soft record; the deterministic main chain continues."""

    return A2AMessage(
        message_id=f"a2a:{task_id}:{agent_name.value}:agent-unavailable",
        sender=agent_name.value,
        receiver=RadarAgentName.ORCHESTRATOR.value,
        task_id=task_id,
        intent="agent_unavailable",
        evidence_refs=evidence_refs,
        requested_action="skip_llm_enhancement_and_continue",
        payload={"status": "agent_unavailable", "main_chain_blocked": False},
        message_status="handled",
    )


def _select_questionnaire_candidates(
    context: ContextPacket,
    ledger: EvidenceLedger,
    latest_summary,
) -> list[QuestionnaireCandidate]:
    """Select reviewed questions from canonical evidence, never scenario IDs."""

    if latest_summary is None or not latest_summary.subject_id:
        return []
    risk = str(ledger.derived_metrics.get("risk_level", "info"))
    if risk == "urgent_boundary":
        return []

    quality_reasons = set(latest_summary.quality_reasons)
    if latest_summary.data_quality_status.value == "unusable":
        preferred = ["q-device-placement", "q-slept-away-from-bed"]
        triggers = [QuestionnaireTrigger.DATA_QUALITY_INSUFFICIENT]
    elif risk == "escalate":
        preferred = [
            "q-daytime-sleepiness",
            "q-snoring-or-gasping",
            "q-night-bathroom",
        ]
        triggers = [QuestionnaireTrigger.WATCH_OR_ESCALATE]
    elif (
        "abnormal_readings" in quality_reasons
        or latest_summary.explainable_metrics.get("vital_fluctuation_count", 0)
    ):
        preferred = ["q-snoring-or-gasping", "q-daytime-sleepiness"]
        triggers = [QuestionnaireTrigger.WATCH_OR_ESCALATE]
    elif latest_summary.out_of_bed_count >= 5:
        preferred = ["q-night-bathroom", "q-daytime-sleepiness"]
        triggers = [QuestionnaireTrigger.WATCH_OR_ESCALATE]
    elif risk == "watch":
        preferred = ["q-sleep-schedule-change", "q-daytime-sleepiness"]
        triggers = [
            QuestionnaireTrigger.TREND_CAUSE_UNKNOWN,
            QuestionnaireTrigger.WATCH_OR_ESCALATE,
        ]
    else:
        return []

    selection = QuestionnaireService().select(
        subject_id=latest_summary.subject_id,
        role=(
            context.task_context.role
            if context.task_context.role in {"elder", "family", "doctor"}
            else "family"
        ),
        triggers=triggers,
        prior_entries=context.evidence_packet.questionnaire_entries,
        max_questions=len(preferred),
        preferred_question_ids=preferred,
    )
    return selection.candidates


__all__ = ["FullOrchestratorAgent"]
