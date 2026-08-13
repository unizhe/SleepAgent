from __future__ import annotations

import pickle
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from pydantic import ValidationError

import sleepagent.runtime.hitl as hitl_module
from sleepagent.runtime.contracts import stable_hash
from sleepagent.runtime.hitl import (
    HITL_POLICY_VERSION,
    ActionProposal,
    ApprovalRequirement,
    DecisionExplanation,
    DecisionRoute,
    HumanDecisionChoice,
    HumanDecisionError,
    HumanDecisionPolicy,
    HumanDecisionService,
    HumanDecisionStatus,
    HumanRiskLevel,
    InMemoryHumanDecisionRepository,
    VerifiedApprovalCapability,
)


NOW = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)


def proposal(
    *,
    proposal_id: str = "proposal:memory:1",
    action_kind: str = "memory",
    action_scope: str = "commit_memory",
    target_id: str | None = None,
    target_hash: str = "a" * 64,
    fact_snapshot_id: str = "snapshot-hitl-1",
    fact_snapshot_hash: str = "f" * 64,
    expires_at: datetime | None = None,
    metadata: dict[str, object] | None = None,
) -> ActionProposal:
    return ActionProposal(
        proposal_id=proposal_id,
        episode_id="episode-hitl-1",
        subject_id="elder-1",
        proposer_actor_id="family-1",
        action_kind=action_kind,
        action_scope=action_scope,
        target_id=target_id or f"target:{action_kind}",
        target_hash=target_hash,
        fact_snapshot_id=fact_snapshot_id,
        fact_snapshot_hash=fact_snapshot_hash,
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
        expires_at=expires_at or NOW + timedelta(minutes=20),
        metadata=metadata or {},
    )


def approve(
    service: HumanDecisionService,
    decision_id: str,
    *,
    actor_id: str = "elder-1",
    actor_role: str = "elder",
    target_hash: str = "a" * 64,
    now: datetime = NOW + timedelta(minutes=1),
) -> None:
    service.decide(
        decision_id,
        actor_id=actor_id,
        actor_role=actor_role,
        choice=HumanDecisionChoice.APPROVE,
        target_hash=target_hash,
        role_binding_id=f"binding:{actor_id}:{actor_role}",
        authorization_id=f"authorization:{actor_id}",
        now=now,
    )


def acquisition_values(request, *, idempotency_key: str = "commit:1") -> dict[str, str]:
    item = request.proposal
    return {
        "expected_proposal_id": item.proposal_id,
        "expected_subject_id": item.subject_id,
        "expected_target_id": item.target_id,
        "expected_target_hash": item.target_hash,
        "expected_action_scope": item.action_scope,
        "expected_fact_snapshot_id": item.fact_snapshot_id,
        "expected_fact_snapshot_hash": item.fact_snapshot_hash,
        "expected_policy_version": item.policy_version,
        "idempotency_key": idempotency_key,
    }


def acquire(
    service: HumanDecisionService,
    request,
    *,
    idempotency_key: str = "commit:1",
    now: datetime = NOW + timedelta(minutes=2),
) -> VerifiedApprovalCapability:
    return service.acquire_verified_capability(
        request.decision_id,
        **acquisition_values(request, idempotency_key=idempotency_key),
        now=now,
    )


def exact_capability_values(capability: VerifiedApprovalCapability) -> dict[str, str]:
    grant = capability.grant
    return {
        "decision_id": grant.decision_id,
        "proposal_id": grant.proposal_id,
        "actor_id": grant.approver_actor_id,
        "actor_role": grant.approver_role,
        "subject_id": grant.subject_id,
        "target_id": grant.target_id,
        "target_hash": grant.target_hash,
        "action_scope": grant.action_scope,
        "fact_snapshot_id": grant.fact_snapshot_id,
        "fact_snapshot_hash": grant.fact_snapshot_hash,
        "policy_version": grant.policy_version,
        "idempotency_key": grant.idempotency_key,
    }


def test_create_is_deterministic_idempotent_and_repository_is_private() -> None:
    repository = InMemoryHumanDecisionRepository()
    service = HumanDecisionService(repository)
    candidate = proposal()

    first = service.create(candidate)
    second = service.create(candidate.model_copy(deep=True))

    assert first == second
    assert first.decision_id.startswith("decision-")
    assert first.revision == 0
    assert len(repository.list_events(first.decision_id)) == 1
    assert service.get(first.decision_id) == first
    assert service.list(subject_id="elder-1") == [first]
    assert not hasattr(service, "repository")

    changed = candidate.model_copy(update={"target_hash": "b" * 64})
    with pytest.raises(HumanDecisionError, match="different content"):
        service.create(changed)


def test_action_proposal_requires_complete_fact_snapshot_binding() -> None:
    values = proposal().model_dump(mode="python")
    values.pop("fact_snapshot_id")
    with pytest.raises(ValidationError, match="fact_snapshot_id"):
        ActionProposal.model_validate(values)


def test_elder_approval_acquires_exact_persisted_grant() -> None:
    service = HumanDecisionService()
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

    approve(service, request.decision_id)
    capability = acquire(service, request)
    grant = capability.grant
    persisted = service.get(request.decision_id)

    assert persisted.status == HumanDecisionStatus.EXECUTING
    assert persisted.revision == 2
    assert persisted.active_grant == grant
    assert grant.decision_revision == persisted.revision
    assert grant.proposal_hash == stable_hash(request.proposal)
    assert grant.approver_role == "elder"
    assert grant.target_hash == request.proposal.target_hash
    assert grant.fact_snapshot_id == request.proposal.fact_snapshot_id
    assert grant.fact_snapshot_hash == request.proposal.fact_snapshot_hash
    assert grant.approving_records == tuple(persisted.decisions)
    assert len(grant.grant_hash) == 64
    capability.validate_exact_binding(**exact_capability_values(capability))


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("expected_proposal_id", "proposal:wrong"),
        ("expected_subject_id", "elder-wrong"),
        ("expected_target_id", "target:wrong"),
        ("expected_target_hash", "b" * 64),
        ("expected_action_scope", "wrong_scope"),
        ("expected_fact_snapshot_id", "snapshot-wrong"),
        ("expected_fact_snapshot_hash", "e" * 64),
        ("expected_policy_version", "policy-wrong"),
    ],
)
def test_acquire_rejects_every_wrong_binding_without_consuming(
    field: str,
    wrong: str,
) -> None:
    service = HumanDecisionService()
    request = service.create(proposal())
    approve(service, request.decision_id)
    values = acquisition_values(request)
    values[field] = wrong

    with pytest.raises(HumanDecisionError, match="not bound"):
        service.acquire_verified_capability(
            request.decision_id,
            **values,
            now=NOW + timedelta(minutes=2),
        )

    unchanged = service.get(request.decision_id)
    assert unchanged.status == HumanDecisionStatus.APPROVED
    assert unchanged.revision == 1
    assert unchanged.active_grant is None


def test_capability_rejects_wrong_actor_role_and_is_non_wire() -> None:
    service = HumanDecisionService()
    request = service.create(proposal())
    approve(service, request.decision_id)
    capability = acquire(service, request)
    values = exact_capability_values(capability)
    values["actor_id"] = "family-1"

    with pytest.raises(HumanDecisionError, match="not bound"):
        capability.validate_exact_binding(**values)
    with pytest.raises(TypeError, match="only be issued"):
        VerifiedApprovalCapability(capability.grant)
    with pytest.raises(TypeError, match="not serializable"):
        pickle.dumps(capability)
    assert not hasattr(VerifiedApprovalCapability, "_issue")
    assert not hasattr(hitl_module, "_issue_verified_capability")
    assert not hasattr(hitl_module, "_CAPABILITY_ISSUER")


def test_raw_grant_and_object_forgery_cannot_authorize_result() -> None:
    service = HumanDecisionService()
    request = service.create(proposal())
    approve(service, request.decision_id)
    capability = acquire(service, request)
    raw_grant = service.get(request.decision_id).active_grant
    assert raw_grant is not None

    with pytest.raises(HumanDecisionError, match="verified capability"):
        service.record_execution_result(
            raw_grant,  # type: ignore[arg-type]
            status=HumanDecisionStatus.COMMITTED,
            receipt_ref="receipt:forged",
        )

    forged = object.__new__(VerifiedApprovalCapability)
    object.__setattr__(forged, "_grant", raw_grant)
    with pytest.raises(HumanDecisionError, match="not issued"):
        forged.validate_exact_binding(**exact_capability_values(capability))
    with pytest.raises(HumanDecisionError, match="not acquired from this"):
        service.record_execution_result(
            forged,
            status=HumanDecisionStatus.COMMITTED,
            receipt_ref="receipt:forged",
        )


def test_expiry_and_revoke_win_before_acquisition() -> None:
    expired_service = HumanDecisionService()
    expired_request = expired_service.create(proposal(proposal_id="proposal:expiry"))
    approve(expired_service, expired_request.decision_id)
    with pytest.raises(HumanDecisionError, match="expired"):
        acquire(
            expired_service,
            expired_request,
            now=NOW + timedelta(minutes=21),
        )
    assert (
        expired_service.get(expired_request.decision_id).status
        == HumanDecisionStatus.EXPIRED
    )

    revoked_service = HumanDecisionService()
    revoked_request = revoked_service.create(proposal(proposal_id="proposal:revoke"))
    approve(revoked_service, revoked_request.decision_id)
    revoked = revoked_service.revoke(
        revoked_request.decision_id,
        actor_id="elder-1",
        actor_role="elder",
        now=NOW + timedelta(minutes=2),
    )
    assert revoked.status == HumanDecisionStatus.REVOKED
    with pytest.raises(HumanDecisionError, match="not approved"):
        acquire(revoked_service, revoked_request)


def test_executing_is_expiry_and_revoke_linearization_point() -> None:
    service = HumanDecisionService()
    request = service.create(proposal(expires_at=NOW + timedelta(minutes=3)))
    approve(service, request.decision_id)
    capability = acquire(service, request, now=NOW + timedelta(minutes=2))

    assert (
        service.expire(request.decision_id, now=NOW + timedelta(minutes=30)).status
        == HumanDecisionStatus.EXECUTING
    )
    with pytest.raises(HumanDecisionError, match="no longer be revoked"):
        service.revoke(
            request.decision_id,
            actor_id="elder-1",
            actor_role="elder",
            now=NOW + timedelta(minutes=30),
        )
    capability.validate_exact_binding(
        **exact_capability_values(capability),
        now=NOW + timedelta(minutes=30),
    )


def test_monotonic_clock_cannot_approve_at_or_after_expiry() -> None:
    service = HumanDecisionService()
    request = service.create(
        proposal(
            proposal_id="proposal:tiny-ttl",
            expires_at=NOW + timedelta(microseconds=1),
        )
    )

    with pytest.raises(HumanDecisionError, match="expired"):
        approve(
            service,
            request.decision_id,
            now=NOW - timedelta(seconds=1),
        )
    assert service.get(request.decision_id).status == HumanDecisionStatus.EXPIRED


def test_approve_versus_revoke_has_one_cas_winner() -> None:
    repository = InMemoryHumanDecisionRepository()
    approving_service = HumanDecisionService(repository)
    revoking_service = HumanDecisionService(repository)
    request = approving_service.create(proposal())
    approve(approving_service, request.decision_id)
    barrier = Barrier(2)

    def acquire_side() -> str:
        barrier.wait()
        try:
            acquire(approving_service, request, idempotency_key="race:acquire")
            return "acquired"
        except HumanDecisionError:
            return "lost"

    def revoke_side() -> str:
        barrier.wait()
        try:
            revoking_service.revoke(
                request.decision_id,
                actor_id="elder-1",
                actor_role="elder",
                now=NOW + timedelta(minutes=2),
            )
            return "revoked"
        except HumanDecisionError:
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        acquire_future = pool.submit(acquire_side)
        revoke_future = pool.submit(revoke_side)
        outcomes = {acquire_future.result(), revoke_future.result()}

    assert outcomes in ({"acquired", "lost"}, {"revoked", "lost"})
    assert approving_service.get(request.decision_id).status in {
        HumanDecisionStatus.EXECUTING,
        HumanDecisionStatus.REVOKED,
    }


def test_different_key_double_acquire_has_one_winner_and_same_key_retries() -> None:
    repository = InMemoryHumanDecisionRepository()
    left = HumanDecisionService(repository)
    right = HumanDecisionService(repository)
    request = left.create(proposal())
    approve(left, request.decision_id)
    barrier = Barrier(2)

    def attempt(service: HumanDecisionService, key: str):
        barrier.wait()
        try:
            return key, acquire(service, request, idempotency_key=key)
        except HumanDecisionError as exc:
            return key, exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt, left, "commit:left")
        second = pool.submit(attempt, right, "commit:right")
        outcomes = [first.result(), second.result()]

    winners = [item for item in outcomes if isinstance(item[1], VerifiedApprovalCapability)]
    losers = [item for item in outcomes if isinstance(item[1], HumanDecisionError)]
    assert len(winners) == 1
    assert len(losers) == 1
    winning_key, winning_capability = winners[0]
    losing_service = right if winning_key == "commit:left" else left

    equivalent = acquire(
        losing_service,
        request,
        idempotency_key=winning_key,
        now=NOW + timedelta(minutes=3),
    )
    assert equivalent.grant == winning_capability.grant
    with pytest.raises(HumanDecisionError, match="already acquired"):
        acquire(
            losing_service,
            request,
            idempotency_key="commit:different",
            now=NOW + timedelta(minutes=3),
        )


@pytest.mark.parametrize(
    ("status", "receipt_ref", "failure_reason"),
    [
        (HumanDecisionStatus.COMMITTED, "receipt:committed", None),
        (HumanDecisionStatus.EXECUTION_FAILED, None, "gateway failed"),
        (HumanDecisionStatus.OUTCOME_UNKNOWN, "receipt:unknown", "timeout"),
    ],
)
def test_terminal_result_and_same_key_replay_are_idempotent(
    status: HumanDecisionStatus,
    receipt_ref: str | None,
    failure_reason: str | None,
) -> None:
    service = HumanDecisionService()
    request = service.create(proposal(proposal_id=f"proposal:{status.value}"))
    approve(service, request.decision_id)
    capability = acquire(service, request)
    completed = service.record_execution_result(
        capability,
        status=status,
        receipt_ref=receipt_ref,
        failure_reason=failure_reason,
        now=NOW + timedelta(minutes=3),
    )

    retried_capability = acquire(
        service,
        request,
        now=NOW + timedelta(minutes=30),
    )
    replayed = service.record_execution_result(
        retried_capability,
        status=status,
        receipt_ref=receipt_ref,
        failure_reason=failure_reason,
        now=NOW + timedelta(minutes=30),
    )

    assert replayed == completed
    assert replayed.revision == 3
    with pytest.raises(HumanDecisionError, match="already recorded differently"):
        service.record_execution_result(
            retried_capability,
            status=status,
            receipt_ref="receipt:changed",
            failure_reason=failure_reason,
        )


def test_repository_cas_rejects_stale_revision_and_non_advancing_clock() -> None:
    repository = InMemoryHumanDecisionRepository()
    service = HumanDecisionService(repository)
    request = service.create(proposal())
    stale = repository.get(request.decision_id)

    non_advancing = stale.model_copy(
        update={
            "status": HumanDecisionStatus.REVOKED,
            "resolved_at": stale.updated_at,
            "revision": stale.revision + 1,
        }
    )
    with pytest.raises(HumanDecisionError, match="updated_at must advance"):
        repository.compare_and_set(
            non_advancing,
            expected_status=stale.status,
            expected_updated_at=stale.updated_at,
            expected_revision=stale.revision,
        )

    approve(service, request.decision_id)
    stale_update = stale.model_copy(
        update={
            "status": HumanDecisionStatus.REVOKED,
            "updated_at": stale.updated_at + timedelta(seconds=1),
            "resolved_at": stale.updated_at + timedelta(seconds=1),
            "revision": stale.revision + 1,
        }
    )
    with pytest.raises(HumanDecisionError, match="changed concurrently"):
        repository.compare_and_set(
            stale_update,
            expected_status=stale.status,
            expected_updated_at=stale.updated_at,
            expected_revision=stale.revision,
        )


def test_multiple_actors_can_satisfy_same_role_count() -> None:
    class TwoElderPolicy(HumanDecisionPolicy):
        def classify(self, _proposal):
            return (
                HumanRiskLevel.R3,
                DecisionRoute.DUAL_REVIEW,
                [
                    ApprovalRequirement(
                        requirement_id="two-elders",
                        role="elder",
                        count=2,
                    )
                ],
            )

    service = HumanDecisionService(policy=TwoElderPolicy())
    request = service.create(proposal())
    approve(service, request.decision_id, actor_id="elder-1")
    assert (
        service.get(request.decision_id).status
        == HumanDecisionStatus.PARTIALLY_APPROVED
    )
    approve(
        service,
        request.decision_id,
        actor_id="elder-2",
        now=NOW + timedelta(minutes=2),
    )
    assert service.get(request.decision_id).status == HumanDecisionStatus.APPROVED


def test_hard_block_and_dual_professional_review_remain_fail_closed() -> None:
    service = HumanDecisionService()
    blocked = service.create(
        proposal(
            proposal_id="proposal:blocked",
            action_scope="override_permission_denial",
        )
    )
    assert blocked.status == HumanDecisionStatus.HARD_BLOCKED
    with pytest.raises(HumanDecisionError, match="hard_blocked"):
        approve(service, blocked.decision_id)

    dual = service.create(
        proposal(
            proposal_id="proposal:doctor-review",
            action_kind="external_action",
            action_scope="share_doctor_material",
            metadata={"professional_review_required": True},
        )
    )
    approve(service, dual.decision_id)
    with pytest.raises(HumanDecisionError, match="not approved"):
        acquire(service, dual)
    approve(
        service,
        dual.decision_id,
        actor_id="doctor-1",
        actor_role="doctor",
        now=NOW + timedelta(minutes=2),
    )
    assert service.get(dual.decision_id).status == HumanDecisionStatus.APPROVED
