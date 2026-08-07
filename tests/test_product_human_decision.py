from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sleepagent.radar_agent.persistence.store import RadarPersistenceStore
from sleepagent.radar_agent.product_agent.hitl import (
    HITL_POLICY_VERSION,
    ActionProposal,
    DecisionExplanation,
    DecisionRoute,
    HumanDecisionChoice,
    HumanDecisionError,
    HumanDecisionService,
    HumanDecisionStatus,
    InMemoryHumanDecisionRepository,
    PersistentHumanDecisionRepository,
)


NOW = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)


def proposal(
    *,
    action_kind: str = "memory",
    action_scope: str = "commit_memory",
    target_hash: str = "a" * 64,
    metadata: dict[str, object] | None = None,
) -> ActionProposal:
    return ActionProposal(
        proposal_id=f"proposal:{action_kind}",
        episode_id="episode-hitl-1",
        subject_id="elder-1",
        proposer_actor_id="family-1",
        action_kind=action_kind,
        action_scope=action_scope,
        target_id=f"target:{action_kind}",
        target_hash=target_hash,
        fact_snapshot_hash="f" * 64,
        policy_version=HITL_POLICY_VERSION,
        payload={"value": "exact candidate"},
        explanation=DecisionExplanation(
            what_will_change="保存这一条候选。",
            why_now="候选已生成，尚未执行。",
            who_will_receive_or_be_affected="老人本人。",
            duration_or_frequency="只批准当前版本一次。",
            how_to_revoke="执行前可撤回。",
            exact_changes=["候选 A"],
        ),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=20),
        metadata=metadata or {},
    )


def test_elder_owns_memory_decision_and_grant_is_exact() -> None:
    service = HumanDecisionService(InMemoryHumanDecisionRepository())
    request = service.create(proposal())

    assert request.route == DecisionRoute.SINGLE_CONFIRM
    assert [item.role for item in request.requirements] == ["elder"]
    with pytest.raises(HumanDecisionError, match="not authorized"):
        service.decide(
            request.decision_id,
            actor_id="family-1",
            actor_role="family",
            choice=HumanDecisionChoice.APPROVE,
            target_hash=request.proposal.target_hash,
            now=NOW + timedelta(minutes=1),
        )
    with pytest.raises(HumanDecisionError, match="stale"):
        service.decide(
            request.decision_id,
            actor_id="elder-1",
            actor_role="elder",
            choice=HumanDecisionChoice.APPROVE,
            target_hash="b" * 64,
            now=NOW + timedelta(minutes=1),
        )

    approved = service.decide(
        request.decision_id,
        actor_id="elder-1",
        actor_role="elder",
        choice=HumanDecisionChoice.APPROVE,
        target_hash=request.proposal.target_hash,
        now=NOW + timedelta(minutes=1),
    )
    grant = service.approval_grant(
        request.decision_id, now=NOW + timedelta(minutes=2)
    )

    assert approved.status == HumanDecisionStatus.APPROVED
    assert grant.approver_role == "elder"
    assert grant.target_hash == request.proposal.target_hash
    assert grant.fact_snapshot_hash == request.proposal.fact_snapshot_hash
    assert len(grant.grant_hash) == 64


def test_external_doctor_material_requires_elder_and_professional() -> None:
    service = HumanDecisionService()
    request = service.create(
        proposal(
            action_kind="external_action",
            action_scope="share_doctor_material",
            metadata={"professional_review_required": True},
        )
    )
    assert request.route == DecisionRoute.DUAL_REVIEW

    partial = service.decide(
        request.decision_id,
        actor_id="elder-1",
        actor_role="elder",
        choice=HumanDecisionChoice.APPROVE,
        target_hash=request.proposal.target_hash,
        now=NOW + timedelta(minutes=1),
    )
    assert partial.status == HumanDecisionStatus.PARTIALLY_APPROVED
    with pytest.raises(HumanDecisionError, match="not approved"):
        service.approval_grant(
            request.decision_id, now=NOW + timedelta(minutes=1)
        )

    approved = service.decide(
        request.decision_id,
        actor_id="doctor-1",
        actor_role="doctor",
        choice=HumanDecisionChoice.APPROVE,
        target_hash=request.proposal.target_hash,
        now=NOW + timedelta(minutes=2),
    )
    assert approved.status == HumanDecisionStatus.APPROVED


def test_hard_block_cannot_be_approved_and_approval_can_expire_or_revoke() -> None:
    service = HumanDecisionService()
    blocked = service.create(
        proposal(action_scope="override_permission_denial")
    )
    assert blocked.status == HumanDecisionStatus.HARD_BLOCKED
    with pytest.raises(HumanDecisionError, match="hard_blocked"):
        service.decide(
            blocked.decision_id,
            actor_id="elder-1",
            actor_role="elder",
            choice=HumanDecisionChoice.APPROVE,
            target_hash=blocked.proposal.target_hash,
            now=NOW + timedelta(minutes=1),
        )

    expiring = service.create(proposal(target_hash="c" * 64))
    service.decide(
        expiring.decision_id,
        actor_id="elder-1",
        actor_role="elder",
        choice=HumanDecisionChoice.APPROVE,
        target_hash=expiring.proposal.target_hash,
        now=NOW + timedelta(minutes=1),
    )
    with pytest.raises(HumanDecisionError, match="expired"):
        service.approval_grant(
            expiring.decision_id, now=NOW + timedelta(minutes=21)
        )
    assert (
        service.repository.get(expiring.decision_id).status
        == HumanDecisionStatus.EXPIRED
    )

    revocable = service.create(proposal(target_hash="d" * 64))
    service.decide(
        revocable.decision_id,
        actor_id="elder-1",
        actor_role="elder",
        choice=HumanDecisionChoice.APPROVE,
        target_hash=revocable.proposal.target_hash,
        now=NOW + timedelta(minutes=1),
    )
    revoked = service.revoke(
        revocable.decision_id,
        actor_id="elder-1",
        actor_role="elder",
        now=NOW + timedelta(minutes=2),
    )
    assert revoked.status == HumanDecisionStatus.REVOKED


def test_decision_and_audit_events_survive_repository_restart() -> None:
    connection = sqlite3.connect(":memory:")
    store = RadarPersistenceStore.connect_sqlite(connection)
    first = HumanDecisionService(PersistentHumanDecisionRepository(store))
    request = first.create(proposal())
    first.decide(
        request.decision_id,
        actor_id="elder-1",
        actor_role="elder",
        choice=HumanDecisionChoice.APPROVE,
        target_hash=request.proposal.target_hash,
        now=NOW + timedelta(minutes=1),
    )

    restarted = HumanDecisionService(PersistentHumanDecisionRepository(store))
    restored = restarted.repository.get(request.decision_id)
    event_json = store.list_product_human_decision_event_json(
        request.decision_id
    )

    assert restored.status == HumanDecisionStatus.APPROVED
    assert restored.decisions[0].actor_id == "elder-1"
    assert len(event_json) == 2


def test_authority_is_revalidated_when_grant_is_minted() -> None:
    checks: list[tuple[str, str]] = []

    def validate(
        _request,
        actor_id,
        actor_role,
        _role_binding_id,
        _authorization_id,
    ) -> None:
        checks.append((actor_id, actor_role))

    service = HumanDecisionService(authority_validator=validate)
    request = service.create(proposal())
    service.decide(
        request.decision_id,
        actor_id="elder-1",
        actor_role="elder",
        choice=HumanDecisionChoice.APPROVE,
        target_hash=request.proposal.target_hash,
        now=NOW + timedelta(minutes=1),
    )
    service.approval_grant(
        request.decision_id,
        now=NOW + timedelta(minutes=2),
    )

    assert checks == [("elder-1", "elder"), ("elder-1", "elder")]
