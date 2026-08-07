from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol, TypedDict

from sleepagent.radar_agent.a2a import A2AEventBus
from sleepagent.radar_agent.agents import (
    AgentResult,
    AlertCareAgent,
    ContextPacket,
    EvidencePacket,
    MemoryAgent,
    RAGAgent,
    RadarDataAgent,
    ReportAgent,
    RiskSignalAgent,
    TrendAgent,
)
from sleepagent.radar_agent.orchestrator.agent import _build_evidence_ledger
from sleepagent.radar_agent.orchestrator.conflicts import (
    ConflictResolver,
    validate_expression_agent_output,
)
from sleepagent.radar_agent.orchestrator.contracts import (
    OrchestratorDecision,
    WORKFLOW_NODE_ORDER,
    WorkflowNodeName,
)
from sleepagent.radar_agent.orchestrator.full import (
    _agent_unavailable_message,
    _confirmations,
    _downstream_context,
    _ground_ledger,
    _memory_data,
    _rag_context,
    _rag_query,
    _select_questionnaire_candidates,
)
from sleepagent.radar_agent.prompts import PromptBundle, build_prompt_bundle
from sleepagent.radar_agent.schemas import (
    A2AMessage,
    ConflictRecord,
    EvidenceLedger,
    HumanConfirmationRequest,
    RadarAgentName,
    RadarDevice,
    RadarNightSummary,
    RadarVitalSnapshot,
    RagContext,
)
from sleepagent.radar_agent.skills import SKILL_BY_AGENT
from sleepagent.radar_agent.questionnaire import QuestionnaireCandidate


class RadarLangGraphUnavailableError(RuntimeError):
    pass


class RadarNodeAuthorizationError(PermissionError):
    pass


class RadarNodeExecutionError(RuntimeError):
    def __init__(self, node: WorkflowNodeName, cause: Exception) -> None:
        super().__init__(f"Radar LangGraph node {node.value} failed: {cause}")
        self.node = node
        self.cause = cause


class RadarLangGraphState(TypedDict, total=False):
    context: ContextPacket
    device: RadarDevice
    snapshots: list[RadarVitalSnapshot]
    provider_report: RadarNightSummary | None
    night_summary: RadarNightSummary
    accepted_results: list[AgentResult]
    evidence_ledger: EvidenceLedger
    conflicts: list[ConflictRecord]
    rag_context: RagContext
    a2a_messages: list[A2AMessage]
    confirmation_requests: list[HumanConfirmationRequest]
    questionnaire_candidates: list[QuestionnaireCandidate]
    completed_nodes: list[WorkflowNodeName]
    node_trace: list[str]
    a2a_rounds: int
    decision: OrchestratorDecision


_NODE_ACTORS: dict[WorkflowNodeName, RadarAgentName] = {
    WorkflowNodeName.INGEST_RADAR_DATA: RadarAgentName.RADAR_DATA,
    WorkflowNodeName.NORMALIZE_CANONICAL_DATA: RadarAgentName.RADAR_DATA,
    WorkflowNodeName.DATA_QUALITY_GATE: RadarAgentName.RADAR_DATA,
    WorkflowNodeName.NIGHT_SUMMARY: RadarAgentName.RADAR_DATA,
    WorkflowNodeName.TREND_ANALYSIS: RadarAgentName.TREND,
    WorkflowNodeName.RISK_SIGNAL_ASSESSMENT: RadarAgentName.RISK_SIGNAL,
    WorkflowNodeName.EVIDENCE_LEDGER_REVIEW: RadarAgentName.ORCHESTRATOR,
    WorkflowNodeName.RAG_GROUNDING: RadarAgentName.RAG,
    WorkflowNodeName.A2A_COLLABORATION_ROUND: RadarAgentName.ORCHESTRATOR,
    WorkflowNodeName.ROLE_REPORT_GENERATION: RadarAgentName.REPORT,
    WorkflowNodeName.ALERT_DECISION: RadarAgentName.ALERT_CARE,
    WorkflowNodeName.MEMORY_CANDIDATE: RadarAgentName.MEMORY,
    WorkflowNodeName.HUMAN_CONFIRMATION: RadarAgentName.ORCHESTRATOR,
    WorkflowNodeName.PUBLISH_ARTIFACTS: RadarAgentName.ORCHESTRATOR,
}


class RadarNodeAuthorityGuard:
    def __init__(self, *, max_a2a_rounds: int = 2) -> None:
        if max_a2a_rounds not in {1, 2}:
            raise ValueError("A2A collaboration must be limited to one or two rounds.")
        self.max_a2a_rounds = max_a2a_rounds

    def authorize(
        self,
        *,
        node: WorkflowNodeName,
        actor: RadarAgentName,
        completed_nodes: list[WorkflowNodeName],
        requested_a2a_round: int | None = None,
    ) -> None:
        expected_prefix = list(WORKFLOW_NODE_ORDER[: WORKFLOW_NODE_ORDER.index(node)])
        if completed_nodes != expected_prefix:
            raise RadarNodeAuthorizationError(
                f"{node.value} cannot run out of order; required prefix is "
                f"{[item.value for item in expected_prefix]}."
            )
        if actor != _NODE_ACTORS[node]:
            raise RadarNodeAuthorizationError(
                f"{actor.value} cannot execute privileged node {node.value}."
            )
        if (
            node == WorkflowNodeName.A2A_COLLABORATION_ROUND
            and requested_a2a_round is not None
            and requested_a2a_round > self.max_a2a_rounds
        ):
            raise RadarNodeAuthorizationError(
                f"A2A round {requested_a2a_round} exceeds limit {self.max_a2a_rounds}."
            )


@dataclass
class RadarWorkflowCheckpoint:
    run_id: str
    state: RadarLangGraphState
    failed_node: WorkflowNodeName | None = None


class RadarCheckpointStore(Protocol):
    def load(self, run_id: str) -> RadarWorkflowCheckpoint | None: ...

    def save(self, checkpoint: RadarWorkflowCheckpoint) -> None: ...


class InMemoryRadarCheckpointStore:
    def __init__(self) -> None:
        self._checkpoints: dict[str, RadarWorkflowCheckpoint] = {}

    def load(self, run_id: str) -> RadarWorkflowCheckpoint | None:
        value = self._checkpoints.get(run_id)
        return deepcopy(value) if value is not None else None

    def save(self, checkpoint: RadarWorkflowCheckpoint) -> None:
        self._checkpoints[checkpoint.run_id] = deepcopy(checkpoint)


class RadarLangGraphWorkflow:
    """Executable 14-node radar-first StateGraph with resumable node checkpoints."""

    def __init__(
        self,
        *,
        radar_data_agent: RadarDataAgent,
        trend_agent: TrendAgent | None = None,
        risk_signal_agent: RiskSignalAgent | None = None,
        rag_agent: RAGAgent | None = None,
        report_agent: ReportAgent | None = None,
        alert_care_agent: AlertCareAgent | None = None,
        memory_agent: MemoryAgent | None = None,
        a2a_bus: A2AEventBus | None = None,
        checkpoint_store: RadarCheckpointStore | None = None,
        max_a2a_rounds: int = 2,
    ) -> None:
        self.radar_data_agent = radar_data_agent
        self.trend_agent = trend_agent or TrendAgent()
        self.risk_signal_agent = risk_signal_agent or RiskSignalAgent()
        self.rag_agent = rag_agent or RAGAgent()
        self.report_agent = report_agent or ReportAgent()
        self.alert_care_agent = alert_care_agent or AlertCareAgent()
        self.memory_agent = memory_agent or MemoryAgent()
        self.a2a_bus = a2a_bus or A2AEventBus()
        self.checkpoint_store = checkpoint_store or InMemoryRadarCheckpointStore()
        self.guard = RadarNodeAuthorityGuard(max_a2a_rounds=max_a2a_rounds)
        self.max_a2a_rounds = max_a2a_rounds
        self.last_prompt_bundles: dict[RadarAgentName, PromptBundle] = {}
        self.last_completed_nodes: list[WorkflowNodeName] = []
        self.last_failed_node: WorkflowNodeName | None = None
        self._active_run_id: str | None = None

    def build(self) -> Any:
        state_graph, end = _load_langgraph_symbols()
        graph = state_graph(RadarLangGraphState)
        for node in WORKFLOW_NODE_ORDER:
            graph.add_node(node.value, self._wrapped_node(node))
        graph.set_entry_point(WORKFLOW_NODE_ORDER[0].value)
        for source, target in zip(WORKFLOW_NODE_ORDER, WORKFLOW_NODE_ORDER[1:]):
            graph.add_edge(source.value, target.value)
        graph.add_edge(WORKFLOW_NODE_ORDER[-1].value, end)
        return graph.compile()

    def run(
        self,
        context: ContextPacket,
        *,
        run_id: str | None = None,
        resume: bool | None = None,
    ) -> OrchestratorDecision:
        resolved_run_id = run_id or context.task_context.task_id
        stored = self.checkpoint_store.load(resolved_run_id)
        should_resume = resume is True or (
            resume is None and stored is not None and stored.failed_node is not None
        )
        checkpoint = stored if should_resume else None
        if resume is True and checkpoint is None:
            raise KeyError(f"No radar workflow checkpoint exists for {resolved_run_id}.")
        initial: RadarLangGraphState = (
            checkpoint.state
            if checkpoint is not None
            else {
                "context": context,
                "accepted_results": [],
                "conflicts": [],
                "a2a_messages": [],
                "confirmation_requests": [],
                "completed_nodes": [],
                "node_trace": [],
                "a2a_rounds": 0,
            }
        )
        self.last_completed_nodes = list(initial.get("completed_nodes", []))
        self.last_failed_node = checkpoint.failed_node if checkpoint is not None else None
        self._active_run_id = resolved_run_id
        try:
            final_state = self.build().invoke(initial)
        finally:
            self._active_run_id = None
        decision = final_state.get("decision")
        if not isinstance(decision, OrchestratorDecision):
            raise RuntimeError("Radar LangGraph did not produce an OrchestratorDecision.")
        return decision

    def _wrapped_node(self, node: WorkflowNodeName):
        def execute(state: RadarLangGraphState) -> RadarLangGraphState:
            completed = list(state.get("completed_nodes", []))
            if node in completed:
                return {}
            requested_round = self._requested_a2a_round(state) if node == WorkflowNodeName.A2A_COLLABORATION_ROUND else None
            try:
                self.guard.authorize(
                    node=node,
                    actor=_NODE_ACTORS[node],
                    completed_nodes=completed,
                    requested_a2a_round=requested_round,
                )
                patch = self._execute(node, state)
            except Exception as exc:
                self.last_completed_nodes = completed
                self.last_failed_node = node
                self._save_checkpoint(state, failed_node=node)
                raise RadarNodeExecutionError(node, exc) from exc
            merged = {**state, **patch}
            merged["completed_nodes"] = [*completed, node]
            merged["node_trace"] = [*state.get("node_trace", []), node.value]
            self.last_completed_nodes = list(merged["completed_nodes"])
            self.last_failed_node = None
            self._save_checkpoint(merged, failed_node=None)
            return {
                **patch,
                "completed_nodes": merged["completed_nodes"],
                "node_trace": merged["node_trace"],
            }

        return execute

    def _execute(self, node: WorkflowNodeName, state: RadarLangGraphState) -> RadarLangGraphState:
        handlers = {
            WorkflowNodeName.INGEST_RADAR_DATA: self._ingest,
            WorkflowNodeName.NORMALIZE_CANONICAL_DATA: self._normalize,
            WorkflowNodeName.DATA_QUALITY_GATE: self._quality_gate,
            WorkflowNodeName.NIGHT_SUMMARY: self._night_summary,
            WorkflowNodeName.TREND_ANALYSIS: self._trend,
            WorkflowNodeName.RISK_SIGNAL_ASSESSMENT: self._risk,
            WorkflowNodeName.EVIDENCE_LEDGER_REVIEW: self._evidence_review,
            WorkflowNodeName.RAG_GROUNDING: self._rag,
            WorkflowNodeName.A2A_COLLABORATION_ROUND: self._a2a,
            WorkflowNodeName.ROLE_REPORT_GENERATION: self._report,
            WorkflowNodeName.ALERT_DECISION: self._alert,
            WorkflowNodeName.MEMORY_CANDIDATE: self._memory,
            WorkflowNodeName.HUMAN_CONFIRMATION: self._human_confirmation,
            WorkflowNodeName.PUBLISH_ARTIFACTS: self._publish,
        }
        return handlers[node](state)

    def _ingest(self, state: RadarLangGraphState) -> RadarLangGraphState:
        context = _require(state, "context", ContextPacket)
        device, snapshots, report = self.radar_data_agent.ingest(context)
        return {"device": device, "snapshots": snapshots, "provider_report": report}

    def _normalize(self, state: RadarLangGraphState) -> RadarLangGraphState:
        device = RadarDevice.model_validate(_require(state, "device", RadarDevice).model_dump())
        snapshots = [RadarVitalSnapshot.model_validate(item.model_dump()) for item in state.get("snapshots", [])]
        deduped = {item.snapshot_id: item for item in snapshots}
        ordered = sorted(deduped.values(), key=lambda item: item.measured_at)
        report = state.get("provider_report")
        normalized_report = RadarNightSummary.model_validate(report.model_dump()) if report is not None else None
        return {"device": device, "snapshots": ordered, "provider_report": normalized_report}

    def _quality_gate(self, state: RadarLangGraphState) -> RadarLangGraphState:
        context = _require(state, "context", ContextPacket)
        night_of = context.evidence_packet.data_quality.get("night_of")
        if isinstance(night_of, str):
            night_of = date.fromisoformat(night_of)
        if night_of is not None and not isinstance(night_of, (date, datetime)):
            raise TypeError("night_of must be an ISO date, date, datetime, or None.")
        summary = self.radar_data_agent.quality_gate.run(
            device=_require(state, "device", RadarDevice),
            snapshots=list(state.get("snapshots", [])),
            provider_report=state.get("provider_report"),
            night_of=night_of,
        )
        return {"night_summary": summary}

    def _night_summary(self, state: RadarLangGraphState) -> RadarLangGraphState:
        context = _require(state, "context", ContextPacket)
        result = self.radar_data_agent.build_result(
            context,
            device=_require(state, "device", RadarDevice),
            snapshots=list(state.get("snapshots", [])),
            night_summary=_require(state, "night_summary", RadarNightSummary),
        )
        return {"accepted_results": [*state.get("accepted_results", []), self._versioned(result, context)]}

    def _trend(self, state: RadarLangGraphState) -> RadarLangGraphState:
        context = _require(state, "context", ContextPacket)
        summary = _require(state, "night_summary", RadarNightSummary)
        summaries = [item for item in context.evidence_packet.night_summaries if item.night_of != summary.night_of]
        trend_context = ContextPacket(
            context_packet_id=f"context:{context.task_context.task_id}:TrendAnalysis",
            task_context=context.task_context.model_copy(update={"stage": WorkflowNodeName.TREND_ANALYSIS.value, "purpose": "analysis", "allowed_actions": ["compute_trend_metrics"]}),
            evidence_packet=EvidencePacket(night_summaries=[*summaries, summary]),
            safety_policy=context.safety_policy,
            a2a_messages=[
                request
                for item in state.get("accepted_results", [])
                if item.agent_name == RadarAgentName.RADAR_DATA
                for request in item.next_requests
                if request.receiver == RadarAgentName.TREND.value
            ],
        )
        result = self._versioned(self.trend_agent.run(trend_context), trend_context)
        return {"accepted_results": [*state.get("accepted_results", []), result]}

    def _risk(self, state: RadarLangGraphState) -> RadarLangGraphState:
        context = _require(state, "context", ContextPacket)
        results = list(state.get("accepted_results", []))
        trend_result = next(item for item in results if item.agent_name == RadarAgentName.TREND)
        risk_context = ContextPacket(
            context_packet_id=f"context:{context.task_context.task_id}:RiskSignalAssessment",
            task_context=context.task_context.model_copy(update={"stage": WorkflowNodeName.RISK_SIGNAL_ASSESSMENT.value, "purpose": "risk", "allowed_actions": ["classify_risk_signal"]}),
            evidence_packet=EvidencePacket(
                night_summaries=[_require(state, "night_summary", RadarNightSummary)],
                data_quality={
                    "trend_claims": [claim.model_dump(mode="json") for claim in trend_result.claims],
                    "text_inputs": context.evidence_packet.data_quality.get("text_inputs", []),
                },
            ),
            safety_policy=context.safety_policy,
            a2a_messages=[
                request
                for request in trend_result.next_requests
                if request.receiver == RadarAgentName.RISK_SIGNAL.value
            ],
        )
        result = self._versioned(self.risk_signal_agent.run(risk_context), risk_context)
        return {"accepted_results": [*results, result]}

    def _evidence_review(self, state: RadarLangGraphState) -> RadarLangGraphState:
        context = _require(state, "context", ContextPacket)
        results = list(state.get("accepted_results", []))
        summary = _require(state, "night_summary", RadarNightSummary)
        ledger = _build_evidence_ledger(context=context, accepted_results=results, night_summary=summary)
        resolution = ConflictResolver().resolve(
            task_id=context.task_context.task_id,
            accepted_results=results,
            evidence_ledger=ledger,
            night_summary=summary,
            safety_policy=context.safety_policy,
        )
        return {"evidence_ledger": resolution.evidence_ledger, "conflicts": resolution.conflicts}

    def _rag(self, state: RadarLangGraphState) -> RadarLangGraphState:
        context = _require(state, "context", ContextPacket)
        ledger = _require(state, "evidence_ledger", EvidenceLedger)
        rag_context = _downstream_context(context, stage=WorkflowNodeName.RAG_GROUNDING, purpose="rag", ledger=ledger, latest_summary=state.get("night_summary"), data_quality={"rag_query": _rag_query(ledger), "rag_roles": ["elder", "family", "doctor"]})
        result = self._versioned(self.rag_agent.run(rag_context), rag_context)
        grounded = _ground_ledger(ledger, result)
        return {"accepted_results": [*state.get("accepted_results", []), result], "evidence_ledger": grounded, "rag_context": _rag_context(result)}

    def _a2a(self, state: RadarLangGraphState) -> RadarLangGraphState:
        context = _require(state, "context", ContextPacket)
        messages = list(state.get("a2a_messages", []))
        requested = [request for result in state.get("accepted_results", []) for request in result.next_requests]
        for request in requested:
            if request.collaboration_round > self.max_a2a_rounds:
                raise RadarNodeAuthorizationError("A2A request exceeds configured collaboration round limit.")
            forwarded = self.a2a_bus.forward_from_orchestrator(request, approved_by_orchestrator=True)
            if request.receiver in {
                RadarAgentName.TREND.value,
                RadarAgentName.RISK_SIGNAL.value,
            }:
                forwarded = self.a2a_bus.mark_handled(forwarded)
            messages.append(forwarded)
        rounds = self._requested_a2a_round(state)
        questionnaire_candidates = _select_questionnaire_candidates(
            context,
            _require(state, "evidence_ledger", EvidenceLedger),
            state.get("night_summary"),
        )
        return {
            "a2a_messages": messages
            or self.a2a_bus.list_messages(task_id=context.task_context.task_id),
            "a2a_rounds": rounds,
            "questionnaire_candidates": questionnaire_candidates,
        }

    def _report(self, state: RadarLangGraphState) -> RadarLangGraphState:
        context = _require(state, "context", ContextPacket)
        ledger = _require(state, "evidence_ledger", EvidenceLedger)
        report_context = _downstream_context(context, stage=WorkflowNodeName.ROLE_REPORT_GENERATION, purpose="report", ledger=ledger, latest_summary=state.get("night_summary"), rag_context=state.get("rag_context", RagContext()))
        result = self._versioned(self.report_agent.run(report_context), report_context)
        validate_expression_agent_output(agent_name=RadarAgentName.REPORT, evidence_ledger=ledger, result=result)
        messages = list(state.get("a2a_messages", []))
        if "agent_unavailable" in result.safety_flags:
            messages.append(
                _agent_unavailable_message(
                    task_id=context.task_context.task_id,
                    agent_name=RadarAgentName.REPORT,
                    evidence_refs=result.evidence_refs,
                )
            )
        return {
            "accepted_results": [*state.get("accepted_results", []), result],
            "a2a_messages": messages,
        }

    def _alert(self, state: RadarLangGraphState) -> RadarLangGraphState:
        context = _require(state, "context", ContextPacket)
        ledger = _require(state, "evidence_ledger", EvidenceLedger)
        alert_context = _downstream_context(context, stage=WorkflowNodeName.ALERT_DECISION, purpose="alert", ledger=ledger, latest_summary=state.get("night_summary"))
        result = self._versioned(self.alert_care_agent.run(alert_context), alert_context)
        if result.output_payload.get("alert_care", {}).get("external_action_executed"):
            raise RadarNodeAuthorizationError("AlertDecision may review candidates but cannot publish an external alert.")
        return {"accepted_results": [*state.get("accepted_results", []), result]}

    def _memory(self, state: RadarLangGraphState) -> RadarLangGraphState:
        context = _require(state, "context", ContextPacket)
        ledger = _require(state, "evidence_ledger", EvidenceLedger)
        results = list(state.get("accepted_results", []))
        report = next(
            item for item in results if item.agent_name == RadarAgentName.REPORT
        )
        memory_context = _downstream_context(
            context,
            stage=WorkflowNodeName.MEMORY_CANDIDATE,
            purpose="memory",
            ledger=ledger,
            latest_summary=state.get("night_summary"),
            data_quality=_memory_data(context, results, report),
        )
        result = self._versioned(self.memory_agent.run(memory_context), memory_context)
        if result.output_payload.get("memory_proposal", {}).get("write_performed"):
            raise RadarNodeAuthorizationError("MemoryCandidate may propose but cannot write long-term memory.")
        return {"accepted_results": [*state.get("accepted_results", []), result]}

    def _human_confirmation(self, state: RadarLangGraphState) -> RadarLangGraphState:
        context = _require(state, "context", ContextPacket)
        results = list(state.get("accepted_results", []))
        base = OrchestratorDecision(
            task_id=context.task_context.task_id,
            node=WorkflowNodeName.HUMAN_CONFIRMATION,
            accepted_results=results[:3],
            confirmation_requests=_risk_confirmations(results),
            evidence_ledger=_require(state, "evidence_ledger", EvidenceLedger),
        )
        alert = next(item for item in results if item.agent_name == RadarAgentName.ALERT_CARE)
        memory = next(item for item in results if item.agent_name == RadarAgentName.MEMORY)
        return {"confirmation_requests": _confirmations(base, alert, memory)}

    def _publish(self, state: RadarLangGraphState) -> RadarLangGraphState:
        context = _require(state, "context", ContextPacket)
        decision = OrchestratorDecision(
            task_id=context.task_context.task_id,
            node=WorkflowNodeName.PUBLISH_ARTIFACTS,
            accepted_results=list(state.get("accepted_results", [])),
            a2a_messages=list(state.get("a2a_messages", [])),
            conflicts=list(state.get("conflicts", [])),
            confirmation_requests=list(state.get("confirmation_requests", [])),
            questionnaire_candidates=list(
                state.get("questionnaire_candidates", [])
            ),
            evidence_ledger=_require(state, "evidence_ledger", EvidenceLedger),
            decision_summary="Radar LangGraph completed all 14 authorized nodes.",
        )
        return {"decision": decision}

    def _versioned(self, result: AgentResult, context: ContextPacket) -> AgentResult:
        skill = SKILL_BY_AGENT[result.agent_name]
        self.last_prompt_bundles[result.agent_name] = build_prompt_bundle(skill, context)
        return result.model_copy(update={"skill_version": skill.version, "prompt_version": skill.version})

    def _requested_a2a_round(self, state: RadarLangGraphState) -> int:
        requested = [request.collaboration_round for result in state.get("accepted_results", []) for request in result.next_requests]
        return max(requested, default=0)

    def _save_checkpoint(self, state: RadarLangGraphState, *, failed_node: WorkflowNodeName | None) -> None:
        if self._active_run_id is None:
            return
        self.checkpoint_store.save(RadarWorkflowCheckpoint(run_id=self._active_run_id, state=deepcopy(state), failed_node=failed_node))


def build_radar_agent_langgraph(**kwargs: Any) -> Any:
    return RadarLangGraphWorkflow(**kwargs).build()


def _risk_confirmations(results: list[AgentResult]) -> list[HumanConfirmationRequest]:
    risk = next(item for item in results if item.agent_name == RadarAgentName.RISK_SIGNAL)
    return [HumanConfirmationRequest.model_validate(item) for item in risk.output_payload.get("risk_signal", {}).get("confirmation_requests", [])]


def _require(state: RadarLangGraphState, key: str, expected_type: type[Any]) -> Any:
    value = state.get(key)
    if not isinstance(value, expected_type):
        raise TypeError(f"State field {key!r} must be {expected_type.__name__}.")
    return value


def _load_langgraph_symbols() -> tuple[type[Any], Any]:
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:
        raise RadarLangGraphUnavailableError(
            "LangGraph is required for the radar graph path. Install with "
            "`python -m pip install -e \".[agent]\"`."
        ) from exc
    return StateGraph, END


__all__ = [
    "InMemoryRadarCheckpointStore",
    "RadarCheckpointStore",
    "RadarLangGraphState",
    "RadarLangGraphUnavailableError",
    "RadarLangGraphWorkflow",
    "RadarNodeAuthorizationError",
    "RadarNodeAuthorityGuard",
    "RadarNodeExecutionError",
    "RadarWorkflowCheckpoint",
    "build_radar_agent_langgraph",
]
