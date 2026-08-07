from __future__ import annotations

from enum import Enum
from typing import Protocol

from pydantic import Field

from sleepagent.radar_agent.agents import AgentResult, ContextPacket
from sleepagent.radar_agent.schemas import (
    A2AMessage,
    ConflictRecord,
    EvidenceLedger,
    HumanConfirmationRequest,
    RadarAgentSchema,
)
from sleepagent.radar_agent.questionnaire import QuestionnaireCandidate


class WorkflowNodeName(str, Enum):
    INGEST_RADAR_DATA = "IngestRadarData"
    NORMALIZE_CANONICAL_DATA = "NormalizeCanonicalData"
    DATA_QUALITY_GATE = "DataQualityGate"
    NIGHT_SUMMARY = "NightSummary"
    TREND_ANALYSIS = "TrendAnalysis"
    RISK_SIGNAL_ASSESSMENT = "RiskSignalAssessment"
    EVIDENCE_LEDGER_REVIEW = "EvidenceLedgerReview"
    RAG_GROUNDING = "RAGGrounding"
    A2A_COLLABORATION_ROUND = "A2ACollaborationRound"
    ROLE_REPORT_GENERATION = "RoleReportGeneration"
    ALERT_DECISION = "AlertDecision"
    MEMORY_CANDIDATE = "MemoryCandidate"
    HUMAN_CONFIRMATION = "HumanConfirmation"
    PUBLISH_ARTIFACTS = "PublishArtifacts"


class OrchestratorDecision(RadarAgentSchema):
    task_id: str = Field(..., min_length=1)
    node: WorkflowNodeName
    accepted_results: list[AgentResult] = Field(default_factory=list)
    a2a_messages: list[A2AMessage] = Field(default_factory=list)
    conflicts: list[ConflictRecord] = Field(default_factory=list)
    confirmation_requests: list[HumanConfirmationRequest] = Field(default_factory=list)
    questionnaire_candidates: list[QuestionnaireCandidate] = Field(
        default_factory=list,
        max_length=3,
    )
    evidence_ledger: EvidenceLedger | None = None
    decision_summary: str = ""


class OrchestratorAgent(Protocol):
    """Single-task orchestrator; lifecycle state belongs to WorkflowRuntime."""

    def run(self, context: ContextPacket) -> OrchestratorDecision:
        ...


WORKFLOW_NODE_ORDER: tuple[WorkflowNodeName, ...] = (
    WorkflowNodeName.INGEST_RADAR_DATA,
    WorkflowNodeName.NORMALIZE_CANONICAL_DATA,
    WorkflowNodeName.DATA_QUALITY_GATE,
    WorkflowNodeName.NIGHT_SUMMARY,
    WorkflowNodeName.TREND_ANALYSIS,
    WorkflowNodeName.RISK_SIGNAL_ASSESSMENT,
    WorkflowNodeName.EVIDENCE_LEDGER_REVIEW,
    WorkflowNodeName.RAG_GROUNDING,
    WorkflowNodeName.A2A_COLLABORATION_ROUND,
    WorkflowNodeName.ROLE_REPORT_GENERATION,
    WorkflowNodeName.ALERT_DECISION,
    WorkflowNodeName.MEMORY_CANDIDATE,
    WorkflowNodeName.HUMAN_CONFIRMATION,
    WorkflowNodeName.PUBLISH_ARTIFACTS,
)


__all__ = [
    "OrchestratorAgent",
    "OrchestratorDecision",
    "WORKFLOW_NODE_ORDER",
    "WorkflowNodeName",
]
