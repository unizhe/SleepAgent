from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from sleepagent.domain.care_actions import (
    CARE_ACTION_POLICY_VERSION,
    CareActionCandidateV2,
    CareActionGovernanceError,
    CareActionProposal,
    CareActionType,
    CareActionUrgency,
    CareApprovalGrant,
    CareAudience,
    CareProposalState,
    DeterministicCareActionPolicy,
    care_action_taxonomy,
)
from sleepagent.application.care_actions import build_care_action_proposal
from sleepagent.runtime.contracts import (
    AgentId,
    CareActionCandidate,
    CareStrategy,
    EvidenceClaim,
    EvidencePacket,
    EvidenceSemantic,
    EvidenceSourceKind,
    SourceScope,
    SourceScopeKind,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def _candidate(**updates: object) -> CareActionCandidateV2:
    values: dict[str, object] = {
        "candidate_id": "candidate-1",
        "subject_id": "subject-a",
        "source_analysis_revision_id": "analysis-1",
        "source_shared_analysis_sha256": "a" * 64,
        "source_night_finalization_revision_id": "finalization-1",
        "source_care_strategy_invocation_id": "invocation-1",
        "source_care_strategy_version": "care-strategy.v1",
        "source_care_work_product_ref": "work-product-1",
        "action_type": "recommend_consistent_wake_time",
        "catalog_action_id": "consistent-wake-time",
        "catalog_action_version": 1,
        "intent": "routine_adjustment",
        "rationale_evidence_refs": ("claim-1",),
        "urgency": "normal",
        "audience": "elder",
        "parameters": {"tolerance_minutes": 30},
        "created_at": NOW,
        "display_explanation": "A bounded display explanation.",
    }
    values.update(updates)
    return CareActionCandidateV2.create(**values)


def _proposal(candidate: CareActionCandidateV2 | None = None) -> CareActionProposal:
    selected = candidate or _candidate()
    decision = DeterministicCareActionPolicy().evaluate(
        selected,
        source_is_current=True,
        source_is_hard_finalized=True,
        evidence_is_sufficient=True,
    )
    return CareActionProposal.create(selected, decision)


def test_taxonomy_is_closed_non_medical_and_channel_free() -> None:
    taxonomy = care_action_taxonomy()
    assert {item["action_type"] for item in taxonomy} == {
        "recommend_consistent_wake_time",
        "recommend_morning_light",
        "request_manual_follow_up",
        "request_morning_review_feedback",
    }
    encoded = repr(taxonomy).casefold()
    for forbidden in (
        "prescribe",
        "medication",
        "diagnose",
        "dispatch",
        "send_email",
        "sms",
        "device_control",
    ):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    "parameters",
    (
        {"email": "model@example.invalid"},
        {"phone_number": "+10000000000"},
        {"channel": "sms"},
        {"recipient_address": "untrusted"},
        {"delivery": {"recipient": "nested-untrusted"}},
    ),
)
def test_arbitrary_model_recipient_or_channel_fails_closed(
    parameters: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="delivery destination"):
        _candidate(parameters=parameters)


def test_unknown_or_mismatched_action_semantics_fail_closed() -> None:
    with pytest.raises(ValidationError, match="unsupported"):
        _candidate(catalog_action_id="model-invented-action")
    with pytest.raises(ValidationError, match="do not match"):
        _candidate(action_type="request_manual_follow_up")


@pytest.mark.parametrize(
    ("policy_input", "reason"),
    (
        ({"source_is_current": False}, "source_analysis_stale"),
        ({"source_is_hard_finalized": False}, "source_not_hard_finalized"),
        ({"evidence_is_sufficient": False}, "evidence_insufficient"),
    ),
)
def test_deterministic_policy_denies_invalid_sources(
    policy_input: dict[str, bool],
    reason: str,
) -> None:
    values = {
        "source_is_current": True,
        "source_is_hard_finalized": True,
        "evidence_is_sufficient": True,
        **policy_input,
    }
    result = DeterministicCareActionPolicy().evaluate(_candidate(), **values)
    assert result.eligible is False
    assert result.reason_code == reason
    assert result.policy_version == CARE_ACTION_POLICY_VERSION


def test_urgent_candidate_preserves_zero_model_fast_path() -> None:
    result = DeterministicCareActionPolicy().evaluate(
        _candidate(urgency=CareActionUrgency.URGENT_SAFETY),
        source_is_current=True,
        source_is_hard_finalized=True,
        evidence_is_sufficient=True,
    )
    assert result.eligible is False
    assert result.reason_code == "urgent_zero_model_boundary"


def test_proposal_state_machine_is_fail_closed() -> None:
    awaiting = _proposal()
    assert awaiting.state is CareProposalState.AWAITING_APPROVAL
    approved = awaiting.transition(CareProposalState.APPROVED)
    rejected = awaiting.transition(CareProposalState.REJECTED)
    expired = awaiting.transition(CareProposalState.EXPIRED)
    assert approved.transition(CareProposalState.REVOKED).state is CareProposalState.REVOKED
    for terminal in (rejected, expired, approved.transition(CareProposalState.REVOKED)):
        with pytest.raises(CareActionGovernanceError, match="invalid"):
            terminal.transition(CareProposalState.APPROVED)


def test_semantic_idempotency_reuses_exact_source_but_changes_on_revision() -> None:
    first = _proposal()
    retry = _proposal()
    changed = _proposal(_candidate(source_analysis_revision_id="analysis-2"))
    assert retry.proposal_id == first.proposal_id
    assert retry.proposal_semantic_hash == first.proposal_semantic_hash
    assert changed.proposal_id != first.proposal_id


def test_grant_is_distinct_exact_bound_expiring_and_revocable() -> None:
    approved = _proposal().transition(CareProposalState.APPROVED)
    grant = CareApprovalGrant.issue(
        approved,
        approver_actor_id="elder-1",
        approver_role=CareAudience.ELDER,
        approver_binding_id="binding-1",
        authorization_epoch=4,
        idempotency_key="approve-1",
        issued_at=NOW + timedelta(minutes=1),
    )
    assert grant.proposal_id == approved.proposal_id
    assert grant.is_usable(
        NOW + timedelta(minutes=2),
        subject_id="subject-a",
        action_type=CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME,
        authorization_scope=approved.authorization_scope,
        proposal_semantic_hash=approved.proposal_semantic_hash,
    )
    assert not grant.is_usable(NOW + timedelta(minutes=2), subject_id="subject-b")
    assert not grant.is_usable(
        NOW + timedelta(minutes=2),
        action_type=CareActionType.REQUEST_MANUAL_FOLLOW_UP,
    )
    assert not grant.is_usable(grant.expires_at)
    assert not grant.revoke().is_usable(NOW + timedelta(minutes=2))


def test_approval_grant_requires_exact_human_role_and_approved_state() -> None:
    awaiting = _proposal()
    with pytest.raises(CareActionGovernanceError, match="approved"):
        CareApprovalGrant.issue(
            awaiting,
            approver_actor_id="family-1",
            approver_role=CareAudience.FAMILY,
            approver_binding_id="binding-1",
            authorization_epoch=1,
            idempotency_key="approve-1",
            issued_at=NOW,
        )
    approved = awaiting.transition(CareProposalState.APPROVED)
    with pytest.raises(CareActionGovernanceError, match="role"):
        CareApprovalGrant.issue(
            approved,
            approver_actor_id="family-1",
            approver_role=CareAudience.FAMILY,
            approver_binding_id="binding-1",
            authorization_epoch=1,
            idempotency_key="approve-1",
            issued_at=NOW,
        )


def _accepted_shared(*, care: bool = True, risk_state: str = "info") -> object:
    evidence = EvidencePacket(
        packet_id="evidence-1",
        source_scope=SourceScope(
            kind=SourceScopeKind.CURRENT_NIGHT,
            as_of=NOW,
            timezone_name="Asia/Shanghai",
            date_start=NOW.date(),
            date_end=NOW.date(),
        ),
        claims=[
            EvidenceClaim(
                claim_id="claim-1",
                semantic=EvidenceSemantic.OBSERVED_FACT,
                statement="Structured accepted observation.",
                source_kind=EvidenceSourceKind.CANONICAL_OBSERVATION,
                evidence_refs=["observation-1"],
                confidence=0.9,
            )
        ],
    )
    strategy = CareStrategy(
        strategy_id="strategy-1",
        disposition="propose",
        evidence_packet_refs=["evidence-work-1"],
        primary_action=CareActionCandidate.create(
            candidate_id="candidate-1",
            candidate_version=1,
            care_action_id="consistent-wake-time",
            care_action_version=1,
            title="Display-only explanation.",
            rationale_evidence_refs=["claim-1"],
            parameters={"tolerance_minutes": 30},
            confirmation_required=True,
            activatable=True,
        ),
    )
    return SimpleNamespace(
        care=(
            SimpleNamespace(
                payload=strategy.model_dump(mode="json"),
                work_product_ref="care-work-1",
            )
            if care
            else None
        ),
        evidence=SimpleNamespace(payload=evidence.model_dump(mode="json")),
        agent_invocations=(
            SimpleNamespace(
                agent_id=AgentId.CARE_STRATEGY,
                invocation_id="care-invocation-1",
                agent_version="care-strategy.v1",
            ),
        ),
        source=SimpleNamespace(risk_state=risk_state, quality_state="good"),
        shared_analysis_sha256="b" * 64,
    )


def test_only_accepted_structured_care_strategy_creates_a_proposal() -> None:
    result = build_care_action_proposal(
        _accepted_shared(),  # type: ignore[arg-type]
        subject_id="subject-a",
        analysis_revision_id="analysis-1",
        night_finalization_revision_id="finalization-1",
        created_at=NOW,
        source_is_current=True,
        source_is_hard_finalized=True,
    )
    assert result.reason_code == "eligible"
    assert result.proposal is not None
    assert result.proposal.candidate.source_care_strategy_invocation_id == (
        "care-invocation-1"
    )


def test_report_prose_or_urgent_safety_cannot_create_a_proposal() -> None:
    no_care = build_care_action_proposal(
        _accepted_shared(care=False),  # type: ignore[arg-type]
        subject_id="subject-a",
        analysis_revision_id="analysis-1",
        night_finalization_revision_id="finalization-1",
        created_at=NOW,
        source_is_current=True,
        source_is_hard_finalized=True,
    )
    urgent = build_care_action_proposal(
        _accepted_shared(risk_state="urgent_boundary"),  # type: ignore[arg-type]
        subject_id="subject-a",
        analysis_revision_id="analysis-1",
        night_finalization_revision_id="finalization-1",
        created_at=NOW,
        source_is_current=True,
        source_is_hard_finalized=True,
    )
    assert no_care.proposal is None
    assert no_care.reason_code == "no_care_strategy"
    assert urgent.proposal is None
    assert urgent.reason_code == "urgent_zero_model_boundary"
