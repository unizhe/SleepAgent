from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sleepagent.radar_agent.agents import AlertCareAgent, ContextPacket, EvidencePacket, TaskContext
from sleepagent.radar_agent.confirmation import (
    automatic_actions,
    confirmation_request,
    confirmation_rule,
)
from sleepagent.radar_agent.orchestrator import OrchestratorDecision, WorkflowNodeName
from sleepagent.radar_agent.persistence import RadarPersistenceStore, RadarSubject
from sleepagent.radar_agent.runtime import IdempotencyConflict, RadarTaskStatus, TaskService
from sleepagent.radar_agent.schemas import (
    EvidenceClaim,
    EvidenceLedger,
    HumanConfirmationRequest,
    RadarDevice,
    RadarDeviceStatus,
    ReviewStatus,
    RiskLevel,
)


NOW = datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc)


class StaticOrchestrator:
    def __init__(self, decision: OrchestratorDecision) -> None:
        self.decision = decision

    def run(self, context: ContextPacket) -> OrchestratorDecision:
        return self.decision


def test_matrix_classifies_auto_family_user_and_doctor_actions() -> None:
    assert automatic_actions(risk_level="info", data_quality_status="partial") == [
        "publish_daily_elder_report",
        "publish_daily_family_report",
        "publish_info_notice",
        "publish_data_quality_notice",
    ]
    for action in (
        "enable_persistent_family_reminder",
        "push_supplemental_questionnaire",
        "enable_care_plan",
        "write_long_term_memory",
    ):
        assert confirmation_rule(action).allowed_roles == ("family",)
    for action in (
        "export_doctor_material",
        "send_doctor_material",
        "create_medical_evaluation_card",
    ):
        assert confirmation_rule(action).allowed_roles == ("elder", "family")
    assert confirmation_rule("doctor_annotation").blocks_daily_flow is False
    assert confirmation_rule("doctor_annotation").allowed_roles == ("doctor",)


def test_info_and_data_quality_notices_auto_publish_and_complete_daily_task() -> None:
    service, store, task = _runtime()
    context = _context(task.task_id, task.trace_id, RiskLevel.INFO, "partial")
    alert = AlertCareAgent().run(context)
    decision = OrchestratorDecision(
        task_id=task.task_id,
        node=WorkflowNodeName.PUBLISH_ARTIFACTS,
        accepted_results=[alert],
        evidence_ledger=context.evidence_packet.evidence_ledger,
    )

    service.execute(task.task_id, StaticOrchestrator(decision), context)

    assert service.get_task(task.task_id).status == RadarTaskStatus.COMPLETED
    assert store.list_confirmations(task.task_id) == []
    published = {
        event.payload["action_type"]
        for event in service.list_events(task.task_id)
        if event.event_type == "action.auto_published"
    }
    assert published == {
        "publish_daily_elder_report",
        "publish_daily_family_report",
        "publish_info_notice",
        "publish_data_quality_notice",
    }


def test_watch_actions_need_family_confirmation_but_daily_flow_completes() -> None:
    service, store, task = _runtime()
    context = _context(task.task_id, task.trace_id, RiskLevel.WATCH, "good")
    alert = AlertCareAgent().run(context)
    requests = [
        HumanConfirmationRequest.model_validate(item)
        for item in alert.output_payload["alert_care"]["confirmation_requests"]
    ]
    requests.append(
        confirmation_request(
            task_id=task.task_id,
            action_type="write_long_term_memory",
            evidence_refs=["night:001"],
        )
    )
    decision = OrchestratorDecision(
        task_id=task.task_id,
        node=WorkflowNodeName.PUBLISH_ARTIFACTS,
        accepted_results=[alert],
        confirmation_requests=requests,
        evidence_ledger=context.evidence_packet.evidence_ledger,
    )

    service.execute(task.task_id, StaticOrchestrator(decision), context)

    saved = store.list_confirmations(task.task_id)
    assert service.get_task(task.task_id).status == RadarTaskStatus.COMPLETED
    assert {item.action_type for item in saved} == {
        "enable_persistent_family_reminder",
        "push_supplemental_questionnaire",
        "enable_care_plan",
        "write_long_term_memory",
    }
    assert all(item.allowed_roles == ["family"] for item in saved)
    assert all(item.status == "pending" and not item.blocks_daily_flow for item in saved)


def test_escalate_material_send_and_evaluation_card_allow_elder_or_family() -> None:
    context = _context("task-escalate", "trace-escalate", RiskLevel.ESCALATE, "good")
    decision = AlertCareAgent().run(context).output_payload["alert_care"]
    requests = [
        HumanConfirmationRequest.model_validate(item)
        for item in decision["confirmation_requests"]
    ]

    assert {item.action_type for item in requests} == {
        "export_doctor_material",
        "send_doctor_material",
        "create_medical_evaluation_card",
    }
    assert all(item.allowed_roles == ["elder", "family"] for item in requests)


@pytest.mark.parametrize(
    "action_type",
    [
        "export_doctor_material",
        "send_doctor_material",
        "create_medical_evaluation_card",
    ],
)
def test_user_or_family_actions_cannot_execute_before_approval(
    action_type: str,
) -> None:
    service, _, task = _runtime()
    service.transition_task(task.task_id, RadarTaskStatus.RUNNING)
    request = confirmation_request(
        task_id=task.task_id,
        action_type=action_type,
        evidence_refs=["night:001"],
    )
    service.request_confirmation(task.task_id, request)
    with pytest.raises(PermissionError, match="approved"):
        service.complete_confirmation_action(
            task.task_id,
            request.confirmation_id,
            actor_id="executor",
            actor_role="system",
            execution_ref=f"result:{action_type}",
        )
    service.resolve_confirmation(
        task.task_id,
        request.confirmation_id,
        approved=True,
        actor_id="family-user",
        actor_role="family",
    )
    completed = service.complete_confirmation_action(
        task.task_id,
        request.confirmation_id,
        actor_id="executor",
        actor_role="system",
        execution_ref=f"result:{action_type}",
    )
    assert completed.execution_status == "completed"


def test_urgent_notice_is_immediate_but_external_notification_records_delivery() -> None:
    service, store, task = _runtime()
    context = _context(
        task.task_id, task.trace_id, RiskLevel.URGENT_BOUNDARY, "good"
    )
    alert = AlertCareAgent().run(context)
    payload = alert.output_payload["alert_care"]
    request = HumanConfirmationRequest.model_validate(
        payload["confirmation_requests"][0]
    )
    decision = OrchestratorDecision(
        task_id=task.task_id,
        node=WorkflowNodeName.PUBLISH_ARTIFACTS,
        accepted_results=[alert],
        confirmation_requests=[request],
        evidence_ledger=context.evidence_packet.evidence_ledger,
    )
    service.execute(task.task_id, StaticOrchestrator(decision), context)

    assert payload["urgent_safety_notice_displayed"] is True
    assert "show_urgent_safety_notice" in payload["automatic_actions"]
    assert service.get_task(task.task_id).status == RadarTaskStatus.COMPLETED
    approved = service.resolve_confirmation(
        task.task_id,
        request.confirmation_id,
        approved=True,
        actor_id="delivery-system",
        actor_role="system",
    )
    delivered = service.complete_confirmation_action(
        task.task_id,
        request.confirmation_id,
        actor_id="notification-gateway",
        actor_role="system",
        execution_ref="delivery:urgent:001",
        delivery_status="delivered",
    )

    assert approved.confirmation_kind == "delivery_record"
    assert delivered.execution_status == "completed"
    assert delivered.delivery_status == "delivered"
    assert delivered.delivered_at == NOW
    assert store.get_confirmation(request.confirmation_id) == delivered


def test_approve_reject_expire_and_revoke_are_authorized_and_idempotent() -> None:
    service, store, task = _runtime()
    service.transition_task(task.task_id, RadarTaskStatus.RUNNING)

    approval = confirmation_request(
        task_id=task.task_id,
        action_type="export_doctor_material",
        evidence_refs=["night:001"],
    )
    requested = service.request_confirmation(task.task_id, approval)
    assert service.request_confirmation(task.task_id, approval) == requested
    with pytest.raises(PermissionError):
        service.resolve_confirmation(
            task.task_id,
            approval.confirmation_id,
            approved=True,
            actor_id="doctor-user",
            actor_role="doctor",
        )
    approved = service.resolve_confirmation(
        task.task_id,
        approval.confirmation_id,
        approved=True,
        actor_id="family-user",
        actor_role="family",
    )
    assert service.resolve_confirmation(
        task.task_id,
        approval.confirmation_id,
        approved=True,
        actor_id="family-user",
        actor_role="family",
    ) == approved
    with pytest.raises(ValueError, match="terminal"):
        service.resolve_confirmation(
            task.task_id,
            approval.confirmation_id,
            approved=False,
            actor_id="elder-user",
            actor_role="elder",
        )
    with pytest.raises(PermissionError):
        service.revoke_confirmation(
            task.task_id,
            approval.confirmation_id,
            actor_id="doctor-user",
            actor_role="doctor",
            reason="无权限撤销",
        )
    revoked = service.revoke_confirmation(
        task.task_id,
        approval.confirmation_id,
        actor_id="family-user",
        actor_role="family",
        reason="不再发送材料",
    )
    assert service.revoke_confirmation(
        task.task_id,
        approval.confirmation_id,
        actor_id="family-user",
        actor_role="family",
        reason="不再发送材料",
    ) == revoked
    with pytest.raises(PermissionError):
        service.complete_confirmation_action(
            task.task_id,
            approval.confirmation_id,
            actor_id="executor",
            actor_role="system",
            execution_ref="should-not-run",
        )

    rejected_request = confirmation_request(
        task_id=task.task_id,
        action_type="enable_care_plan",
        evidence_refs=["night:001"],
    )
    service.request_confirmation(task.task_id, rejected_request)
    rejected = service.resolve_confirmation(
        task.task_id,
        rejected_request.confirmation_id,
        approved=False,
        actor_id="family-user",
        actor_role="family",
    )
    assert rejected.status == "rejected"
    assert service.resolve_confirmation(
        task.task_id,
        rejected_request.confirmation_id,
        approved=False,
        actor_id="family-user",
        actor_role="family",
    ) == rejected

    expiring = confirmation_request(
        task_id=task.task_id,
        action_type="push_supplemental_questionnaire",
        evidence_refs=["night:001"],
    ).model_copy(update={"created_at": NOW - timedelta(hours=2)})
    service.request_confirmation(task.task_id, expiring)
    expired = service.expire_confirmations(task.task_id, before=NOW)
    assert [item.confirmation_id for item in expired] == [expiring.confirmation_id]
    assert service.expire_confirmations(task.task_id, before=NOW) == []
    assert len(
        [
            event
            for event in service.list_events(task.task_id)
            if event.event_type == "confirmation.requested"
            and event.payload["confirmation_id"] == approval.confirmation_id
        ]
    ) == 1
    assert store.get_confirmation(expiring.confirmation_id).status == "expired"


def test_idempotency_conflict_and_action_completion_permissions() -> None:
    service, _, task = _runtime()
    service.transition_task(task.task_id, RadarTaskStatus.RUNNING)
    request = confirmation_request(
        task_id=task.task_id,
        action_type="enable_persistent_family_reminder",
        evidence_refs=["night:001"],
    )
    service.request_confirmation(task.task_id, request)
    conflicting = request.model_copy(update={"evidence_refs": ["night:other"]})
    with pytest.raises(IdempotencyConflict):
        service.request_confirmation(task.task_id, conflicting)
    service.resolve_confirmation(
        task.task_id,
        request.confirmation_id,
        approved=True,
        actor_id="family-user",
        actor_role="family",
    )
    with pytest.raises(PermissionError):
        service.complete_confirmation_action(
            task.task_id,
            request.confirmation_id,
            actor_id="doctor-user",
            actor_role="doctor",
            execution_ref="reminder:001",
        )
    completed = service.complete_confirmation_action(
        task.task_id,
        request.confirmation_id,
        actor_id="care-executor",
        actor_role="system",
        execution_ref="reminder:001",
    )
    assert service.complete_confirmation_action(
        task.task_id,
        request.confirmation_id,
        actor_id="care-executor",
        actor_role="system",
        execution_ref="reminder:001",
    ) == completed
    with pytest.raises(ValueError, match="another result"):
        service.complete_confirmation_action(
            task.task_id,
            request.confirmation_id,
            actor_id="care-executor",
            actor_role="system",
            execution_ref="reminder:002",
        )
    with pytest.raises(ValueError, match="executed"):
        service.revoke_confirmation(
            task.task_id,
            request.confirmation_id,
            actor_id="family-user",
            actor_role="family",
            reason="执行后不可撤销",
        )


def test_doctor_annotation_is_doctor_only_and_never_blocks_daily_flow() -> None:
    service, _, task = _runtime()
    service.transition_task(task.task_id, RadarTaskStatus.RUNNING)
    service.transition_task(task.task_id, RadarTaskStatus.COMPLETED)
    request = confirmation_request(
        task_id=task.task_id,
        action_type="doctor_annotation",
        evidence_refs=["report:doctor:001"],
    )
    service.request_confirmation(task.task_id, request)

    assert service.get_task(task.task_id).status == RadarTaskStatus.COMPLETED
    with pytest.raises(PermissionError):
        service.resolve_confirmation(
            task.task_id,
            request.confirmation_id,
            approved=True,
            actor_id="family-user",
            actor_role="family",
        )
    annotation = service.resolve_confirmation(
        task.task_id,
        request.confirmation_id,
        approved=True,
        actor_id="doctor-user",
        actor_role="doctor",
    )
    assert annotation.confirmation_kind == "doctor_annotation"
    assert service.get_task(task.task_id).status == RadarTaskStatus.COMPLETED


def _runtime():
    store = RadarPersistenceStore.connect_sqlite(sqlite3.connect(":memory:"))
    store.save_subject(RadarSubject(subject_id="elder-matrix", display_name="Elder"))
    store.save_device(
        RadarDevice(
            radar_device_id="radar-matrix",
            display_name="Radar",
            provider="replay",
            status=RadarDeviceStatus.ONLINE,
            bound_subject_id="elder-matrix",
        )
    )
    service = TaskService(
        store,
        clock=lambda: NOW,
        validate_bindings=False,
        require_authorization=False,
    )
    task = service.create_task(
        subject_id="elder-matrix",
        radar_device_id="radar-matrix",
        role="family",
    )
    return service, store, task


def _context(
    task_id: str,
    trace_id: str,
    risk: RiskLevel,
    data_quality_status: str,
) -> ContextPacket:
    claim = EvidenceClaim(
        claim_id=f"claim:{risk.value}",
        task_id=task_id,
        text=f"Risk signal is {risk.value}.",
        evidence_refs=["night:001"],
        confidence=0.8,
        risk_level=risk,
        caveats=["Non-diagnostic observation."],
        generated_by="risk_signal",
        review_status=ReviewStatus.REVIEWED,
    )
    ledger = EvidenceLedger(
        ledger_id=f"ledger:{task_id}",
        task_id=task_id,
        canonical_evidence_refs=["night:001"],
        derived_metrics={
            "risk_level": risk.value,
            "data_quality_status": data_quality_status,
        },
        claims=[claim],
        confidence=0.8,
        caveats=["Observation only."],
        review_status=ReviewStatus.REVIEWED,
    )
    return ContextPacket(
        task_context=TaskContext(
            task_id=task_id,
            trace_id=trace_id,
            role="family",
            purpose="alert",
        ),
        evidence_packet=EvidencePacket(evidence_ledger=ledger),
    )
