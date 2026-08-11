from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import sleepagent.product_runtime.confirmation as confirmation_projection
from sleepagent.product_runtime.confirmation import automatic_actions, confirmation_rule
from sleepagent.persistence import RadarPersistenceStore, RadarSubject
from sleepagent.product_runtime.hitl import (
    HITL_POLICY_VERSION,
    ActionProposal,
    DecisionExplanation,
    HumanDecisionChoice,
    HumanDecisionError,
    HumanDecisionRequest,
    HumanDecisionService,
    HumanDecisionStatus,
    PersistentHumanDecisionRepository,
)
from sleepagent.product_runtime.task_runtime import TaskService
from sleepagent.product_runtime.schemas import (
    HumanConfirmationRequest,
    RadarDevice,
    RadarDeviceStatus,
)


NOW = datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc)
CONFIRMATION_MUTATORS = (
    "request_confirmation",
    "resolve_confirmation",
    "expire_confirmations",
    "revoke_confirmation",
    "complete_confirmation_action",
)


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


def test_task_confirmation_surface_is_read_only_projection() -> None:
    service, store, task = _runtime()

    assert all(not hasattr(TaskService, name) for name in CONFIRMATION_MUTATORS)
    assert not hasattr(store, "save_confirmation")
    assert not hasattr(confirmation_projection, "confirmation_request")
    assert service.list_confirmations(task.task_id) == []


def test_hds_projection_writer_persists_only_authoritative_state() -> None:
    service, store, task = _runtime()
    decisions = HumanDecisionService(PersistentHumanDecisionRepository(store))
    authority = decisions.create(_proposal(task.task_id))
    projection = _projection(authority)

    assert store.save_hds_confirmation_projection(projection) == projection
    assert store.save_hds_confirmation_projection(projection) == projection
    assert store.get_confirmation(projection.confirmation_id) == projection
    assert service.list_confirmations(task.task_id) == [projection]
    assert decisions.get(authority.decision_id).status == HumanDecisionStatus.PENDING


def test_projection_cannot_forge_approval_or_execution_capability() -> None:
    _, store, task = _runtime()
    decisions = HumanDecisionService(PersistentHumanDecisionRepository(store))
    authority = decisions.create(_proposal(task.task_id))
    projection = _projection(authority)
    store.save_hds_confirmation_projection(projection)

    forged_approval = projection.model_copy(
        update={
            "status": "approved",
            "resolved_at": NOW + timedelta(minutes=1),
            "resolved_by": "forged-actor",
        }
    )
    with pytest.raises(ValueError, match="not HDS-derived"):
        store.save_hds_confirmation_projection(forged_approval)

    forged_execution = projection.model_copy(
        update={
            "execution_status": "completed",
            "execution_ref": "forged-receipt",
            "executed_at": NOW + timedelta(minutes=1),
        }
    )
    with pytest.raises(ValueError, match="execution status mismatch"):
        store.save_hds_confirmation_projection(forged_execution)

    with pytest.raises(HumanDecisionError, match="not approved"):
        decisions.acquire_verified_capability(
            authority.decision_id,
            expected_proposal_id=authority.proposal.proposal_id,
            expected_subject_id=authority.proposal.subject_id,
            expected_target_id=authority.proposal.target_id,
            expected_target_hash=authority.proposal.target_hash,
            expected_action_scope=authority.proposal.action_scope,
            expected_fact_snapshot_id=authority.proposal.fact_snapshot_id,
            expected_fact_snapshot_hash=authority.proposal.fact_snapshot_hash,
            expected_policy_version=authority.proposal.policy_version,
            idempotency_key="forged-projection-cannot-authorize",
            now=NOW + timedelta(minutes=2),
        )
    assert store.get_confirmation(projection.confirmation_id) == projection
    assert decisions.get(authority.decision_id).status == HumanDecisionStatus.PENDING


def test_projection_rejects_old_revision_and_decision_rebinding() -> None:
    _, store, task = _runtime()
    decisions = HumanDecisionService(PersistentHumanDecisionRepository(store))
    first = decisions.create(_proposal(task.task_id, suffix="first"))
    pending = _projection(first, confirmation_id="confirmation:stable")
    store.save_hds_confirmation_projection(pending)

    approved = decisions.decide(
        first.decision_id,
        actor_id="elder-matrix",
        actor_role="elder",
        choice=HumanDecisionChoice.APPROVE,
        target_hash=first.proposal.target_hash,
        now=NOW + timedelta(minutes=1),
    )
    approved_projection = _projection(
        approved,
        confirmation_id=pending.confirmation_id,
    )
    store.save_hds_confirmation_projection(approved_projection)

    with pytest.raises(ValueError, match="decision revision mismatch"):
        store.save_hds_confirmation_projection(pending)

    second = decisions.create(_proposal(task.task_id, suffix="second"))
    rebound = _projection(second, confirmation_id=pending.confirmation_id)
    with pytest.raises(ValueError, match="identity cannot be rebound"):
        store.save_hds_confirmation_projection(rebound)

    assert store.get_confirmation(pending.confirmation_id) == approved_projection


def test_projection_writer_refuses_status_rollback_from_legacy_row() -> None:
    _, store, task = _runtime()
    decisions = HumanDecisionService(PersistentHumanDecisionRepository(store))
    authority = decisions.create(
        _proposal(
            task.task_id,
            suffix="dual",
            action_kind="external_action",
            action_scope="send_doctor_material",
            professional_review_required=True,
        )
    )
    pending = _projection(authority, confirmation_id="confirmation:legacy")
    legacy_approved = HumanConfirmationRequest.model_validate(
        pending.model_copy(
            update={
                "status": "approved",
                "resolved_at": NOW,
                "resolved_by": "legacy-authority",
            }
        ).model_dump(mode="python")
    )
    store.connection.execute(
        """
        INSERT INTO radar_human_confirmations (
          confirmation_id, task_id, action_type, requested_role,
          status, confirmation_json, created_at, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            legacy_approved.confirmation_id,
            legacy_approved.task_id,
            legacy_approved.action_type,
            legacy_approved.requested_role,
            legacy_approved.status,
            legacy_approved.model_dump_json(),
            legacy_approved.created_at.isoformat(),
            legacy_approved.resolved_at.isoformat(),
        ),
    )
    store.connection.commit()

    partial = decisions.decide(
        authority.decision_id,
        actor_id="elder-matrix",
        actor_role="elder",
        choice=HumanDecisionChoice.APPROVE,
        target_hash=authority.proposal.target_hash,
        now=NOW + timedelta(minutes=1),
    )
    assert partial.status == HumanDecisionStatus.PARTIALLY_APPROVED
    with pytest.raises(ValueError, match="status cannot regress"):
        store.save_hds_confirmation_projection(
            _projection(partial, confirmation_id=legacy_approved.confirmation_id)
        )


def test_projection_requires_an_existing_authoritative_decision() -> None:
    _, store, task = _runtime()
    decisions = HumanDecisionService(PersistentHumanDecisionRepository(store))
    authority = decisions.create(_proposal(task.task_id))
    projection = _projection(authority).model_copy(
        update={"decision_id": "decision:missing"}
    )

    with pytest.raises(ValueError, match="requires an authoritative decision"):
        store.save_hds_confirmation_projection(projection)


def _proposal(
    task_id: str,
    *,
    suffix: str = "one",
    action_kind: str = "memory",
    action_scope: str = "commit_memory",
    professional_review_required: bool = False,
) -> ActionProposal:
    hash_character = {"first": "a", "second": "b", "dual": "c"}.get(
        suffix,
        "d",
    )
    return ActionProposal(
        proposal_id=f"proposal:{suffix}",
        task_id=task_id,
        episode_id=f"episode:{suffix}",
        subject_id="elder-matrix",
        proposer_actor_id="family-matrix",
        action_kind=action_kind,
        action_scope=action_scope,
        target_id=f"target:{suffix}",
        target_hash=hash_character * 64,
        fact_snapshot_id=f"snapshot:{suffix}",
        fact_snapshot_hash="f" * 64,
        policy_version=HITL_POLICY_VERSION,
        payload={"candidate": suffix},
        explanation=DecisionExplanation(
            what_will_change=f"Apply candidate {suffix}.",
            why_now="The candidate is ready but has not been executed.",
            who_will_receive_or_be_affected="The elder owner.",
            duration_or_frequency="One exact version only.",
            how_to_revoke="Revoke through HDS before execution.",
            exact_changes=[suffix],
        ),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=20),
        metadata={
            "professional_review_required": professional_review_required,
        },
    )


def _projection(
    decision: HumanDecisionRequest,
    *,
    confirmation_id: str | None = None,
) -> HumanConfirmationRequest:
    status = {
        HumanDecisionStatus.PENDING: "pending",
        HumanDecisionStatus.PARTIALLY_APPROVED: "pending",
        HumanDecisionStatus.APPROVED: "approved",
        HumanDecisionStatus.EXECUTING: "approved",
        HumanDecisionStatus.COMMITTED: "approved",
        HumanDecisionStatus.EXECUTION_FAILED: "approved",
        HumanDecisionStatus.OUTCOME_UNKNOWN: "approved",
        HumanDecisionStatus.REJECTED: "rejected",
        HumanDecisionStatus.HARD_BLOCKED: "rejected",
        HumanDecisionStatus.EXPIRED: "expired",
        HumanDecisionStatus.REVOKED: "revoked",
        HumanDecisionStatus.SUPERSEDED: "revoked",
    }[decision.status]
    execution_status = {
        HumanDecisionStatus.COMMITTED: "completed",
        HumanDecisionStatus.EXECUTION_FAILED: "failed",
        HumanDecisionStatus.OUTCOME_UNKNOWN: "failed",
    }.get(decision.status, "not_started")
    resolved_by = (
        decision.decisions[-1].actor_id
        if decision.decisions
        else "human-decision-service"
    )
    resolved_confirmation_id = confirmation_id or f"confirmation:{decision.decision_id}"
    return HumanConfirmationRequest(
        confirmation_id=resolved_confirmation_id,
        decision_id=decision.decision_id,
        decision_revision=decision.revision,
        task_id=decision.proposal.task_id or "missing-task",
        action_type=(
            f"product_{decision.proposal.action_kind}:"
            f"{decision.proposal.action_scope}"
        ),
        requested_role="elder",
        allowed_roles=[
            requirement.role
            for requirement in decision.requirements
            if requirement.role in {"elder", "family", "doctor", "system"}
        ],
        reason=decision.proposal.explanation.what_will_change,
        evidence_refs=[
            decision.proposal.target_id,
            decision.proposal.target_hash,
        ],
        status=status,
        idempotency_key=resolved_confirmation_id,
        created_at=decision.created_at,
        resolved_at=decision.resolved_at if status != "pending" else None,
        resolved_by=(
            resolved_by
            if status in {"approved", "rejected", "expired"}
            else None
        ),
        revoked_at=decision.resolved_at if status == "revoked" else None,
        revoked_by=resolved_by if status == "revoked" else None,
        execution_status=execution_status,
        execution_ref=decision.execution_receipt_ref,
        executed_at=(
            decision.updated_at
            if execution_status in {"completed", "failed"}
            else None
        ),
    )


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
