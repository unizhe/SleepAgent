from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from itertools import count

import pytest

from sleepagent.radar_agent.agents import AgentResult, ContextPacket, RadarAgentName
from sleepagent.radar_agent.evidence import EvidenceLedgerBuilder
from sleepagent.radar_agent.orchestrator import (
    ConflictResolver,
    ExpressionBoundaryViolation,
    MinimalOrchestratorAgent,
    OrchestratorDecision,
    WorkflowNodeName,
    validate_expression_agent_output,
)
from sleepagent.radar_agent.persistence import (
    RadarPersistenceStore,
    RadarSubject,
    RadarUserRoleBinding,
)
from sleepagent.radar_agent.schemas import (
    ConflictRecord,
    EvidenceClaim,
    EvidenceLedger,
    RadarDataQualityStatus,
    RadarDevice,
    RadarDeviceStatus,
    RadarNightSummary,
    ReviewStatus,
    RiskLevel,
)
from sleepagent.radar_agent.runtime import RadarAgentTask, TaskService


NOW = datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc)
NIGHT = date(2026, 7, 10)


def test_orchestrator_records_quality_conflict_before_risk_claims() -> None:
    orchestrator = MinimalOrchestratorAgent(
        radar_data_agent=QualityBlockedRadarDataAgent(),
        trend_agent=WatchTrendAgent(),
        risk_signal_agent=EscalatingRiskAgent(),
    )

    decision = orchestrator.run(_context("task-quality-conflict"))

    assert decision.conflicts
    conflict = decision.conflicts[0]
    assert conflict.final_status == "downgraded"
    assert conflict.requires_human_confirmation is False
    assert conflict.sources[0] == RadarAgentName.RADAR_DATA.value
    assert "Data quality has priority" in conflict.decision
    assert decision.evidence_ledger is not None
    assert decision.evidence_ledger.derived_metrics["risk_level"] == "uncertain"
    assert decision.evidence_ledger.review_status == ReviewStatus.NEEDS_HUMAN_REVIEW


def test_evidence_ledger_priority_records_unsupported_claim_conflict() -> None:
    claim = EvidenceClaim(
        claim_id="claim-unsupported",
        task_id="task-evidence-conflict",
        text="Unsupported escalation must not survive ledger review.",
        confidence=0.9,
        risk_level=RiskLevel.ESCALATE,
        generated_by=RadarAgentName.TREND.value,
        review_status=ReviewStatus.DRAFT,
    )
    builder = EvidenceLedgerBuilder(
        ledger_id="ledger-evidence-conflict",
        task_id="task-evidence-conflict",
    )
    builder.add_claim(claim)
    ledger = builder.build()

    outcome = ConflictResolver().resolve(
        task_id="task-evidence-conflict",
        accepted_results=[
            AgentResult(
                agent_name=RadarAgentName.TREND,
                claims=[claim],
                confidence=0.9,
            )
        ],
        evidence_ledger=ledger,
    )

    assert outcome.conflicts
    conflict = outcome.conflicts[0]
    assert conflict.sources == [RadarAgentName.TREND.value]
    assert conflict.final_status == "downgraded"
    assert conflict.requires_human_confirmation is True
    assert "Evidence Ledger has priority" in conflict.decision
    assert outcome.evidence_ledger.claims[0].review_status == (
        ReviewStatus.NEEDS_HUMAN_REVIEW
    )
    assert outcome.evidence_ledger.uncertainty == (
        "missing_evidence_refs; evidence_ledger_conflict"
    )


def test_unresolved_risk_conflict_uses_conservative_downgrade_not_majority_vote() -> None:
    ledger = EvidenceLedger(
        ledger_id="ledger-risk-conflict",
        task_id="task-risk-conflict",
        canonical_evidence_refs=["night-summary:risk"],
        derived_metrics={"risk_level": RiskLevel.ESCALATE.value},
        claims=[
            _claim(
                "risk-escalate-1",
                task_id="task-risk-conflict",
                generated_by=RadarAgentName.RISK_SIGNAL.value,
                risk_level=RiskLevel.ESCALATE,
            ),
            _claim(
                "risk-escalate-2",
                task_id="task-risk-conflict",
                generated_by=RadarAgentName.RAG.value,
                risk_level=RiskLevel.ESCALATE,
            ),
            _claim(
                "risk-watch",
                task_id="task-risk-conflict",
                generated_by=RadarAgentName.ALERT_CARE.value,
                risk_level=RiskLevel.WATCH,
            ),
        ],
        confidence=0.7,
        review_status=ReviewStatus.REVIEWED,
    )

    outcome = ConflictResolver().resolve(
        task_id="task-risk-conflict",
        evidence_ledger=ledger,
        accepted_results=[
            AgentResult(
                agent_name=RadarAgentName.RISK_SIGNAL,
                claims=[ledger.claims[0]],
                confidence=0.8,
            ),
            AgentResult(
                agent_name=RadarAgentName.RAG,
                claims=[ledger.claims[1]],
                confidence=0.8,
            ),
            AgentResult(
                agent_name=RadarAgentName.ALERT_CARE,
                claims=[ledger.claims[2]],
                confidence=0.6,
            ),
        ],
    )

    assert outcome.conflicts[0].final_status == "uncertain"
    assert outcome.conflicts[0].requires_human_confirmation is True
    assert "No majority vote is used" in outcome.conflicts[0].decision
    assert outcome.evidence_ledger.derived_metrics["risk_level"] == "watch"
    assert outcome.evidence_ledger.uncertainty == "risk_conflict_unresolved"


def test_safety_boundary_priority_preserves_urgent_boundary() -> None:
    ledger = EvidenceLedger(
        ledger_id="ledger-safety-conflict",
        task_id="task-safety-conflict",
        canonical_evidence_refs=["night-summary:safety"],
        derived_metrics={"risk_level": RiskLevel.WATCH.value},
        claims=[
            _claim(
                "risk-urgent",
                task_id="task-safety-conflict",
                generated_by=RadarAgentName.RISK_SIGNAL.value,
                risk_level=RiskLevel.URGENT_BOUNDARY,
            ),
            _claim(
                "risk-watch",
                task_id="task-safety-conflict",
                generated_by=RadarAgentName.RAG.value,
                risk_level=RiskLevel.WATCH,
            ),
        ],
        confidence=0.6,
        review_status=ReviewStatus.REVIEWED,
    )

    outcome = ConflictResolver().resolve(
        task_id="task-safety-conflict",
        evidence_ledger=ledger,
        accepted_results=[
            AgentResult(
                agent_name=RadarAgentName.RISK_SIGNAL,
                claims=[ledger.claims[0]],
                confidence=0.7,
            ),
            AgentResult(
                agent_name=RadarAgentName.RAG,
                claims=[ledger.claims[1]],
                confidence=0.6,
            ),
        ],
    )

    assert outcome.conflicts[0].final_status == "accepted"
    assert outcome.conflicts[0].requires_human_confirmation is True
    assert "Safety boundary has priority" in outcome.conflicts[0].decision
    assert outcome.evidence_ledger.derived_metrics["risk_level"] == "urgent_boundary"


def test_report_and_dialogue_outputs_are_expression_only() -> None:
    ledger = EvidenceLedger(
        ledger_id="ledger-expression",
        task_id="task-expression",
        canonical_evidence_refs=["night-summary:expression"],
        derived_metrics={"risk_level": RiskLevel.WATCH.value},
        claims=[
            _claim(
                "claim-expression",
                task_id="task-expression",
                generated_by=RadarAgentName.RISK_SIGNAL.value,
                risk_level=RiskLevel.WATCH,
            )
        ],
        confidence=0.7,
        review_status=ReviewStatus.REVIEWED,
    )

    valid_report = AgentResult(
        agent_name=RadarAgentName.REPORT,
        evidence_refs=["claim-expression"],
        output_payload={"content": "Family-facing wording.", "risk_level": "watch"},
    )
    assert (
        validate_expression_agent_output(
            agent_name=RadarAgentName.REPORT,
            evidence_ledger=ledger,
            result=valid_report,
        )
        == valid_report
    )

    with pytest.raises(ExpressionBoundaryViolation, match="risk level"):
        validate_expression_agent_output(
            agent_name=RadarAgentName.DIALOGUE,
            evidence_ledger=ledger,
            result=AgentResult(
                agent_name=RadarAgentName.DIALOGUE,
                evidence_refs=["claim-expression"],
                output_payload={"answer": "Changed fact.", "risk_level": "escalate"},
            ),
        )
    with pytest.raises(ExpressionBoundaryViolation, match="evidence claims"):
        validate_expression_agent_output(
            agent_name=RadarAgentName.REPORT,
            evidence_ledger=ledger,
            result=AgentResult(
                agent_name=RadarAgentName.REPORT,
                claims=[
                    _claim(
                        "claim-new",
                        task_id="task-expression",
                        generated_by=RadarAgentName.REPORT.value,
                        risk_level=RiskLevel.INFO,
                    )
                ],
            ),
        )


def test_task_service_persists_orchestrator_conflict_records() -> None:
    service, store = _service()
    task = _create(service)
    conflict = ConflictRecord(
        conflict_id="conflict-persisted",
        task_id=task.task_id,
        sources=[RadarAgentName.TREND.value, RadarAgentName.RISK_SIGNAL.value],
        summary="Risk conflict persisted from orchestrator decision.",
        decision="Conservative downgrade.",
        final_status="uncertain",
        requires_human_confirmation=True,
    )

    class FakeOrchestrator:
        def run(self, context: ContextPacket) -> OrchestratorDecision:
            return OrchestratorDecision(
                task_id=context.task_context.task_id,
                node=WorkflowNodeName.EVIDENCE_LEDGER_REVIEW,
                conflicts=[conflict],
                decision_summary="Conflict persisted.",
            )

    service.execute(task.task_id, FakeOrchestrator(), _context(task.task_id))

    assert store.list_conflicts(task.task_id) == [conflict]


class QualityBlockedRadarDataAgent:
    def run(self, context: ContextPacket) -> AgentResult:
        summary = RadarNightSummary(
            radar_device_id="radar-001",
            subject_id="elder-001",
            night_of=NIGHT,
            data_quality_status=RadarDataQualityStatus.UNUSABLE,
            confidence_label="not_interpretable",
            health_conclusion_allowed=False,
            data_coverage_ratio=0.2,
            blocked_reasons=["coverage_below_minimum"],
            source_report_ref="night-summary:quality-conflict",
            generated_at=NOW,
        )
        return AgentResult(
            agent_name=RadarAgentName.RADAR_DATA,
            claims=[
                _claim(
                    "radar-data-quality",
                    task_id=context.task_context.task_id,
                    generated_by=RadarAgentName.RADAR_DATA.value,
                    risk_level=RiskLevel.INFO,
                )
            ],
            evidence_refs=["night-summary:quality-conflict"],
            confidence=0.2,
            uncertainties=["coverage_below_minimum"],
            output_payload={"night_summary": summary.model_dump(mode="json")},
        )


class WatchTrendAgent:
    def run(self, context: ContextPacket) -> AgentResult:
        return AgentResult(
            agent_name=RadarAgentName.TREND,
            claims=[
                _claim(
                    "trend-watch",
                    task_id=context.task_context.task_id,
                    generated_by=RadarAgentName.TREND.value,
                    risk_level=RiskLevel.WATCH,
                )
            ],
            evidence_refs=["night-summary:quality-conflict"],
            confidence=0.7,
        )


class EscalatingRiskAgent:
    def run(self, context: ContextPacket) -> AgentResult:
        claim = _claim(
            "risk-escalate",
            task_id=context.task_context.task_id,
            generated_by=RadarAgentName.RISK_SIGNAL.value,
            risk_level=RiskLevel.ESCALATE,
        )
        return AgentResult(
            agent_name=RadarAgentName.RISK_SIGNAL,
            claims=[claim],
            evidence_refs=claim.evidence_refs,
            confidence=0.8,
            output_payload={
                "risk_signal": {
                    "task_id": context.task_context.task_id,
                    "risk_level": RiskLevel.ESCALATE.value,
                    "confirmation_requests": [],
                }
            },
        )


def _claim(
    claim_id: str,
    *,
    task_id: str,
    generated_by: str,
    risk_level: RiskLevel,
) -> EvidenceClaim:
    return EvidenceClaim(
        claim_id=claim_id,
        task_id=task_id,
        text=f"{claim_id} text",
        evidence_refs=["night-summary:quality-conflict"],
        confidence=0.7,
        risk_level=risk_level,
        generated_by=generated_by,
        review_status=ReviewStatus.REVIEWED,
    )


def _context(task_id: str) -> ContextPacket:
    return ContextPacket.model_validate(
        {
            "task_context": {
                "task_id": task_id,
                "trace_id": f"trace-{task_id}",
                "purpose": "orchestration",
                "allowed_actions": ["run_minimal_chain"],
            }
        }
    )


def _service() -> tuple[TaskService, RadarPersistenceStore]:
    store = RadarPersistenceStore.connect_sqlite(sqlite3.connect(":memory:"))
    store.save_subject(
        RadarSubject(
            subject_id="elder-001",
            display_name="Demo elder",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    store.save_role_binding(
        RadarUserRoleBinding(
            role_binding_id="family-binding-001",
            user_id="family-user-001",
            subject_id="elder-001",
            role="family",
            display_name="Demo family",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    store.save_device(
        RadarDevice(
            radar_device_id="radar-001",
            display_name="Bedroom radar",
            provider="replay",
            status=RadarDeviceStatus.ONLINE,
            bound_subject_id="elder-001",
            registered_at=NOW,
            updated_at=NOW,
        )
    )
    ids = count(1)
    return (
        TaskService(
            store,
            clock=lambda: NOW,
            id_factory=lambda: f"{next(ids):04d}",
            require_authorization=False,
        ),
        store,
    )


def _create(service: TaskService) -> RadarAgentTask:
    return service.create_task(
        subject_id="elder-001",
        radar_device_id="radar-001",
        role="family",
        role_binding_ids=["family-binding-001"],
        scenario="normal_night",
    )
