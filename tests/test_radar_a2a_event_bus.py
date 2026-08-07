from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from itertools import count

import pytest

from sleepagent.radar_agent.a2a import (
    A2AApprovalRequired,
    A2AEventBus,
    A2APolicyError,
    A2ARoundLimitExceeded,
    InMemoryA2AMailbox,
)
from sleepagent.radar_agent.agents import (
    AgentResult,
    ContextPacket,
    RadarAgentName,
    RadarDataAgent,
)
from sleepagent.radar_agent.orchestrator import MinimalOrchestratorAgent
from sleepagent.radar_agent.persistence import (
    RadarPersistenceStore,
    RadarSubject,
    RadarUserRoleBinding,
)
from sleepagent.radar_agent.provider import ReplayRadarProvider
from sleepagent.radar_agent.schemas import (
    A2AMessage,
    RadarDevice,
    RadarDeviceStatus,
    RiskLevel,
)
from sleepagent.radar_agent.runtime import RadarAgentTask, RadarTaskStatus, TaskService


NOW = datetime(2026, 7, 11, 9, 0, tzinfo=timezone.utc)


def test_event_bus_persists_message_status_transitions_to_a2a_log() -> None:
    store = _store_with_tasks("task-a2a")
    bus = A2AEventBus(InMemoryA2AMailbox(store=store))
    message = _message("task-a2a")

    queued = bus.forward_from_orchestrator(message)
    delivered = bus.deliver_to(task_id="task-a2a", receiver="risk_signal")[0]
    handled = bus.mark_handled(delivered)

    assert queued.message_status == "queued"
    assert delivered.message_status == "delivered"
    assert handled.message_status == "handled"
    assert store.list_a2a_messages("task-a2a") == [handled]

    with pytest.raises(A2APolicyError, match="Sub-agents cannot publish"):
        bus.publish_from_agent(_message("task-a2a", message_id="a2a-direct"))


def test_collaboration_round_is_limited_to_two_rounds() -> None:
    A2AMessage(
        message_id="a2a-round-2",
        task_id="task-a2a",
        sender="trend",
        receiver="risk_signal",
        intent="second_round_challenge",
        collaboration_round=2,
    )

    with pytest.raises(ValueError, match="less than or equal to 2"):
        A2AMessage(
            message_id="a2a-round-3",
            task_id="task-a2a",
            sender="trend",
            receiver="risk_signal",
            intent="third_round_loop",
            collaboration_round=3,
        )

    bus = A2AEventBus(max_collaboration_rounds=1)
    with pytest.raises(A2ARoundLimitExceeded, match="1-2 rounds"):
        bus.forward_from_orchestrator(
            A2AMessage(
                message_id="a2a-bus-round-2",
                task_id="task-a2a",
                sender="trend",
                receiver="risk_signal",
                intent="second_round_when_runtime_allows_one",
                collaboration_round=2,
            )
        )


def test_minimal_orchestrator_routes_agent_requests_through_event_bus() -> None:
    risk_agent = RecordingRiskAgent()
    orchestrator = MinimalOrchestratorAgent(
        radar_data_agent=RadarDataAgent(ReplayRadarProvider(scenario="normal_night")),
        trend_agent=MessagingTrendAgent(),
        risk_signal_agent=risk_agent,
    )

    decision = orchestrator.run(_context("task-orchestrated-a2a"))

    assert risk_agent.contexts[0].a2a_messages
    assert risk_agent.contexts[0].a2a_messages[0].message_status == "delivered"
    assert decision.a2a_messages[0].routed_by == "orchestrator"
    assert decision.a2a_messages[0].message_status == "handled"


def test_cross_task_share_requires_workflow_runtime_and_orchestrator_approval() -> None:
    service, store = _service()
    source = _create(service)
    target = _create(service)

    with pytest.raises(A2AApprovalRequired, match="Orchestrator approval"):
        service.approve_cross_task_share(
            source.task_id,
            target.task_id,
            sender="memory",
            receiver="trend",
            intent="share_trend_summary",
            shared_artifact_type="trend_summary",
            payload={"summary": "7-day out-of-bed count is rising."},
            approved_by_orchestrator=False,
        )

    cross_task_without_runtime = A2AMessage(
        message_id="a2a-no-runtime",
        task_id=source.task_id,
        target_task_id=target.task_id,
        sender="memory",
        receiver="trend",
        intent="share_trend_summary",
        requires_approval=True,
        shared_artifact_type="trend_summary",
        payload={"summary": "7-day out-of-bed count is rising."},
    )
    with pytest.raises(A2AApprovalRequired, match="WorkflowRuntime approval"):
        A2AEventBus().forward_from_orchestrator(cross_task_without_runtime)

    message = service.approve_cross_task_share(
        source.task_id,
        target.task_id,
        sender="memory",
        receiver="trend",
        intent="share_trend_summary",
        shared_artifact_type="trend_summary",
        payload={"summary": "7-day out-of-bed count is rising."},
        evidence_refs=["ledger:trend-summary"],
        approved_by_orchestrator=True,
    )

    assert message.requires_approval is True
    assert message.target_task_id == target.task_id
    assert message.shared_artifact_type == "trend_summary"
    assert store.list_a2a_messages(source.task_id) == [message]
    assert any(
        event.event_type == "a2a.cross_task_share_approved"
        for event in service.list_events(source.task_id)
    )


def test_cross_task_share_blocks_sensitive_or_disallowed_payloads() -> None:
    service, _ = _service()
    source = _create(service)
    target = _create(service)

    with pytest.raises(ValueError, match="A2A messages cannot share"):
        service.approve_cross_task_share(
            source.task_id,
            target.task_id,
            sender="memory",
            receiver="trend",
            intent="share_sensitive_payload",
            shared_artifact_type="trend_summary",
            payload={"raw_radar_stream": [{"heart_rate": 62}]},
            approved_by_orchestrator=True,
        )

    with pytest.raises(ValueError, match="Input should be"):
        service.approve_cross_task_share(
            source.task_id,
            target.task_id,
            sender="memory",
            receiver="trend",
            intent="share_disallowed_artifact",
            shared_artifact_type="raw_radar_stream",
            payload={"summary": "Not allowed as cross-task artifact type."},
            approved_by_orchestrator=True,
        )


class MessagingTrendAgent:
    def run(self, context: ContextPacket) -> AgentResult:
        return AgentResult(
            agent_name=RadarAgentName.TREND,
            evidence_refs=["night-summary:task-orchestrated-a2a"],
            confidence=0.7,
            next_requests=[
                A2AMessage(
                    message_id="a2a-trend-risk",
                    sender=RadarAgentName.TREND.value,
                    receiver=RadarAgentName.RISK_SIGNAL.value,
                    task_id=context.task_context.task_id,
                    intent="request_risk_review",
                    evidence_refs=["night-summary:task-orchestrated-a2a"],
                    confidence=0.7,
                    risk_level=RiskLevel.WATCH,
                )
            ],
        )


class RecordingRiskAgent:
    def __init__(self) -> None:
        self.contexts: list[ContextPacket] = []

    def run(self, context: ContextPacket) -> AgentResult:
        self.contexts.append(context)
        return AgentResult(
            agent_name=RadarAgentName.RISK_SIGNAL,
            evidence_refs=["night-summary:task-orchestrated-a2a"],
            confidence=0.6,
        )


def _message(task_id: str, *, message_id: str = "a2a-001") -> A2AMessage:
    return A2AMessage(
        message_id=message_id,
        task_id=task_id,
        sender="trend",
        receiver="risk_signal",
        intent="review_watch_signal",
        evidence_refs=["claim-001"],
        confidence=0.76,
        risk_level=RiskLevel.WATCH,
        created_at=NOW,
    )


def _context(task_id: str) -> ContextPacket:
    return ContextPacket.model_validate(
        {
            "task_context": {
                "task_id": task_id,
                "trace_id": f"trace-{task_id}",
                "purpose": "orchestration",
                "allowed_actions": ["run_minimal_chain"],
            },
            "evidence_packet": {
                "data_quality": {"night_of": "2026-07-09"},
            },
        }
    )


def _service() -> tuple[TaskService, RadarPersistenceStore]:
    store = _store_with_tasks()
    ids = count(1)
    service = TaskService(
        store,
        clock=lambda: NOW,
        id_factory=lambda: f"{next(ids):04d}",
        require_authorization=False,
    )
    return service, store


def _create(service: TaskService) -> RadarAgentTask:
    return service.create_task(
        subject_id="elder-001",
        radar_device_id="radar-001",
        role="family",
        role_binding_ids=["family-binding-001"],
        scenario="normal_night",
    )


def _store_with_tasks(*task_ids: str) -> RadarPersistenceStore:
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
    for task_id in task_ids:
        store.save_task(
            RadarAgentTask(
                task_id=task_id,
                trace_id=f"trace-{task_id}",
                subject_id="elder-001",
                radar_device_id="radar-001",
                role="family",
                scenario="normal_night",
                status=RadarTaskStatus.RUNNING,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    return store
