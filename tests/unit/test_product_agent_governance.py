from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from sleepagent.runtime.contracts import (
    AgentEnvelope,
    AgentId,
    AuthenticatedBinding,
    CareActionCandidate,
    CareDeliveryDecision,
    CareDeliveryModality,
    CareDeliveryTiming,
    CareStrategy,
    CommunicationDraft,
    CommunicationSemanticBinding,
    CoordinationCandidate,
    EvidenceClaim,
    EvidencePacket,
    EvidenceSemantic,
    EvidenceSourceKind,
    FactSnapshot,
    InvocationOutcome,
    InterruptionBurden,
    MemoryChangeCandidate,
    SafetyDecision,
    SafetyVerdict,
    SourceScope,
    SourceScopeKind,
    ToolEffect,
    ToolReceipt,
    WorkProductStatus,
)
from sleepagent.runtime.governance import (
    AcceptanceError,
    CareActionCatalog,
    CareActionDefinition,
    ConfirmationError,
    DeterministicCommitController,
    InMemoryCareContextStore,
    InMemoryMemoryContextStore,
    StaleStateError,
    accept_care,
    accept_communication,
    accept_evidence,
    accept_safety,
    publication_postflight,
    safety_trigger_reasons,
)
from sleepagent.runtime.hitl import (
    HITL_POLICY_VERSION,
    ActionProposal,
    DecisionExplanation,
    HumanDecisionChoice,
    HumanDecisionError,
    HumanDecisionRequest,
    HumanDecisionService,
    HumanDecisionStatus,
    VerifiedApprovalCapability,
)


NOW = datetime(2026, 7, 26, 7, 0, tzinfo=timezone.utc)
VALID_UNTIL = NOW + timedelta(days=30)


def snapshot(*, active_constraint_codes: tuple[str, ...] = ()) -> FactSnapshot:
    scope = SourceScope(
        kind=SourceScopeKind.CURRENT_NIGHT,
        as_of=NOW,
        timezone_name="Asia/Shanghai",
        date_start=date(2026, 7, 26),
        date_end=date(2026, 7, 26),
        valid_night_count=1,
    )
    return FactSnapshot.create(
        fact_snapshot_id="snapshot-1",
        binding=AuthenticatedBinding(
            actor_id="actor-1",
            subject_id="subject-1",
            role="elder",
            authorization_scope=("read_sleep_data",),
        ),
        source_scope=scope,
        canonical_data_version="canonical:v1",
        active_constraint_codes=active_constraint_codes,
        source_refs=("night:2026-07-26",),
        created_at=NOW,
    )


def _approved_decision(
    *,
    action_kind: str,
    action_scope: str,
    target_id: str,
    target_hash: str,
    fact_snapshot: FactSnapshot | None = None,
    expires_at: datetime = VALID_UNTIL,
    policy_version: str = HITL_POLICY_VERSION,
) -> tuple[HumanDecisionService, HumanDecisionRequest]:
    bound_snapshot = fact_snapshot or snapshot()
    service = HumanDecisionService()
    service.policy.version = policy_version
    request = service.create(
        ActionProposal(
            proposal_id=f"proposal:{action_scope}:{target_id}",
            episode_id="episode-1",
            subject_id=bound_snapshot.binding.subject_id,
            proposer_actor_id=bound_snapshot.binding.actor_id,
            action_kind=action_kind,
            action_scope=action_scope,
            target_id=target_id,
            target_hash=target_hash,
            fact_snapshot_id=bound_snapshot.fact_snapshot_id,
            fact_snapshot_hash=bound_snapshot.fact_snapshot_hash,
            policy_version=policy_version,
            payload={"target_id": target_id},
            explanation=DecisionExplanation(
                what_will_change=f"Execute {action_scope}",
                why_now="The exact target is ready for accountable execution.",
                who_will_receive_or_be_affected=bound_snapshot.binding.subject_id,
                duration_or_frequency="One bounded execution.",
                how_to_revoke="Revoke before execution begins.",
            ),
            created_at=NOW,
            expires_at=expires_at,
        )
    )
    approved = service.decide(
        request.decision_id,
        actor_id=bound_snapshot.binding.actor_id,
        actor_role=bound_snapshot.binding.role,
        choice=HumanDecisionChoice.APPROVE,
        target_hash=target_hash,
        now=NOW + timedelta(minutes=1),
    )
    assert approved.status == HumanDecisionStatus.APPROVED
    return service, approved


def _approved_capability(
    *,
    action_kind: str,
    action_scope: str,
    target_id: str,
    target_hash: str,
    idempotency_key: str,
    fact_snapshot: FactSnapshot | None = None,
) -> tuple[HumanDecisionService, VerifiedApprovalCapability]:
    bound_snapshot = fact_snapshot or snapshot()
    service, request = _approved_decision(
        action_kind=action_kind,
        action_scope=action_scope,
        target_id=target_id,
        target_hash=target_hash,
        fact_snapshot=bound_snapshot,
    )
    capability = service.acquire_verified_capability(
        request.decision_id,
        expected_proposal_id=request.proposal.proposal_id,
        expected_subject_id=request.proposal.subject_id,
        expected_target_id=request.proposal.target_id,
        expected_target_hash=request.proposal.target_hash,
        expected_action_scope=request.proposal.action_scope,
        expected_fact_snapshot_id=bound_snapshot.fact_snapshot_id,
        expected_fact_snapshot_hash=bound_snapshot.fact_snapshot_hash,
        expected_policy_version=HITL_POLICY_VERSION,
        idempotency_key=idempotency_key,
        now=NOW + timedelta(minutes=2),
    )
    return service, capability


def _assert_authority_refs(
    tool_receipt: ToolReceipt,
    capability: VerifiedApprovalCapability,
) -> None:
    grant = capability.grant
    assert tool_receipt.source_refs == [
        f"human-decision:{grant.decision_id}",
        f"approval-grant:{grant.grant_id}:{grant.grant_hash}",
    ]


def receipt() -> ToolReceipt:
    snap = snapshot()
    return ToolReceipt(
        tool_invocation_id="tool-night",
        tool_name="radar.get_night_evidence",
        tool_version="v1",
        caller=AgentId.EVIDENCE_REASONING.value,
        fact_snapshot_id=snap.fact_snapshot_id,
        fact_snapshot_hash=snap.fact_snapshot_hash,
        input_hash="a" * 64,
        effect=ToolEffect.READ_ONLY,
        outcome=InvocationOutcome.SUCCEEDED,
        observed_at=NOW,
        source_refs=["night:2026-07-26"],
    )


def envelope(agent_id: AgentId, payload, *, refs=(), revision=1) -> AgentEnvelope:
    snap = snapshot()
    return AgentEnvelope(
        episode_id="episode-1",
        invocation_id=f"invoke-{agent_id.value}-{revision}",
        fact_snapshot_id=snap.fact_snapshot_id,
        fact_snapshot_hash=snap.fact_snapshot_hash,
        episode_state_revision=revision,
        source_scope=snap.source_scope,
        input_work_product_refs=list(refs),
        target_type=agent_id.value,
        target_id=f"target-{agent_id.value}-{revision}",
        target_hash=("b" if revision == 1 else "c") * 64,
        agent_id=agent_id,
        agent_version="v1",
        skill_id="test",
        skill_version="v1",
        schema_version="v1",
        policy_version="product-safety.v3",
        status=WorkProductStatus.COMPLETED,
        summary="done",
        output_payload=payload,
    )


def evidence_packet(*, low_confidence=False) -> EvidencePacket:
    return EvidencePacket(
        packet_id="evidence-1",
        source_scope=snapshot().source_scope,
        claims=[
            EvidenceClaim(
                claim_id="claim-1",
                semantic=(
                    EvidenceSemantic.INFERENCE
                    if low_confidence
                    else EvidenceSemantic.OBSERVED_FACT
                ),
                statement="昨夜记录覆盖充分",
                source_kind=EvidenceSourceKind.CANONICAL_OBSERVATION,
                evidence_refs=["night:2026-07-26"],
                confidence=0.6 if low_confidence else 0.9,
                alternative_explanations=(
                    ["设备佩戴差异"] if low_confidence else []
                ),
                date_start=date(2026, 7, 26),
                date_end=date(2026, 7, 26),
            )
        ],
        coverage_ratio=0.9,
    )


def test_evidence_gate_accepts_grounded_claim_and_rejects_stale_snapshot() -> None:
    accepted = accept_evidence(
        envelope(AgentId.EVIDENCE_REASONING, evidence_packet()),
        snapshot=snapshot(),
        tool_receipts=[receipt()],
    )
    assert accepted.agent_id == AgentId.EVIDENCE_REASONING
    stale = snapshot().model_copy(update={"fact_snapshot_hash": "f" * 64})
    with pytest.raises(AcceptanceError, match="hash"):
        accept_evidence(
            envelope(AgentId.EVIDENCE_REASONING, evidence_packet()),
            snapshot=stale,
            tool_receipts=[receipt()],
        )


def test_evidence_gate_rejects_unsupported_personal_claim() -> None:
    packet = evidence_packet()
    packet.claims[0].evidence_refs = ["made-up"]
    with pytest.raises(AcceptanceError, match="support"):
        accept_evidence(
            envelope(AgentId.EVIDENCE_REASONING, packet),
            snapshot=snapshot(),
            tool_receipts=[receipt()],
        )


def test_evidence_gate_preserves_authorized_observer_report_semantic() -> None:
    observer_ref = "habit-answer:episode-1:habit.observed_snoring:1"
    packet = EvidencePacket(
        packet_id="observer-evidence",
        source_scope=snapshot().source_scope,
        claims=[
            EvidenceClaim(
                claim_id="observer-claim",
                semantic=EvidenceSemantic.OBSERVER_REPORTED,
                statement="家属报告近期亲自观察到夜间行为",
                source_kind=EvidenceSourceKind.AUTHORIZED_OBSERVER_REPORT,
                evidence_refs=[observer_ref],
                confidence=0.8,
            )
        ],
    )
    capture_receipt = receipt().model_copy(
        update={
            "tool_invocation_id": "tool-habit-capture",
            "tool_name": "questionnaire.capture_profile",
            "source_refs": [observer_ref],
        }
    )
    accepted = accept_evidence(
        envelope(AgentId.EVIDENCE_REASONING, packet),
        snapshot=snapshot(),
        tool_receipts=[capture_receipt],
    )
    assert accepted.payload["claims"][0]["semantic"] == "observer_reported"
    packet.claims[0].statement = "睡眠习惯综合分 82，因此确诊"
    with pytest.raises(AcceptanceError, match="score"):
        accept_evidence(
            envelope(AgentId.EVIDENCE_REASONING, packet),
            snapshot=snapshot(),
            tool_receipts=[capture_receipt],
        )


def test_evidence_gate_binds_reviewed_observer_input_source() -> None:
    observer_ref = "authorized_observer_report:reviewed-family-input"
    packet = EvidencePacket(
        packet_id="observer-user-input",
        source_scope=snapshot().source_scope,
        claims=[
            EvidenceClaim(
                claim_id="observer-user-input-claim",
                semantic=EvidenceSemantic.OBSERVER_REPORTED,
                statement="家属报告昨晚观察到入睡时间较晚",
                source_kind=EvidenceSourceKind.AUTHORIZED_OBSERVER_REPORT,
                evidence_refs=[observer_ref],
                confidence=0.7,
            )
        ],
    )
    accepted = accept_evidence(
        envelope(AgentId.EVIDENCE_REASONING, packet),
        snapshot=snapshot(),
        tool_receipts=[receipt()],
        authorized_user_input_refs={observer_ref},
    )
    assert accepted.payload["claims"][0]["semantic"] == "observer_reported"

    with pytest.raises(AcceptanceError, match="authorized capture"):
        accept_evidence(
            envelope(AgentId.EVIDENCE_REASONING, packet),
            snapshot=snapshot(),
            tool_receipts=[receipt()],
        )


def care_action() -> CareActionCandidate:
    return CareActionCandidate.create(
        candidate_id="care-1",
        candidate_version=1,
        care_action_id="consistent-wake-time",
        care_action_version=1,
        title="连续五天固定起床时间",
        rationale_evidence_refs=["claim-1"],
        parameters={"tolerance_minutes": 30},
        duration_days=5,
        stop_conditions=["明显不适时停止"],
        activatable=True,
    )


def test_care_gate_requires_accepted_evidence_and_catalog_bounds() -> None:
    evidence = accept_evidence(
        envelope(AgentId.EVIDENCE_REASONING, evidence_packet()),
        snapshot=snapshot(),
        tool_receipts=[receipt()],
    )
    strategy = CareStrategy(
        strategy_id="strategy-1",
        disposition="propose",
        evidence_packet_refs=[evidence.work_product_ref],
        primary_action=care_action(),
    )
    accepted = accept_care(
        envelope(
            AgentId.CARE_STRATEGY,
            strategy,
            refs=[evidence.work_product_ref],
        ),
        snapshot=snapshot(),
        accepted_evidence=evidence,
        catalog=CareActionCatalog(),
    )
    assert accepted.payload["primary_action"]["candidate_id"] == "care-1"
    bad = care_action().model_copy(update={"parameters": {"tolerance_minutes": 90}})
    with pytest.raises(AcceptanceError, match="bounds"):
        accept_care(
            envelope(
                AgentId.CARE_STRATEGY,
                strategy.model_copy(update={"primary_action": bad}),
                refs=[evidence.work_product_ref],
            ),
            snapshot=snapshot(),
            accepted_evidence=evidence,
            catalog=CareActionCatalog(),
        )


def test_family_delivery_requires_coordination_candidate_and_policy() -> None:
    evidence = accept_evidence(
        envelope(AgentId.EVIDENCE_REASONING, evidence_packet()),
        snapshot=snapshot(),
        tool_receipts=[receipt()],
    )
    delivery = CareDeliveryDecision(
        timing=CareDeliveryTiming.IMMEDIATE,
        modality=CareDeliveryModality.LIGHT,
        interruption_burden=InterruptionBurden.LOW,
        notify_family=True,
        device_policy_ref="device-delivery-policy.v1",
        coordination_policy_ref="coordination-policy.v1",
        preference_evidence_refs=("claim-1",),
    )
    action = CareActionCandidate.create(
        candidate_id="care-night-support",
        candidate_version=1,
        care_action_id="nighttime-gentle-support",
        care_action_version=1,
        title="夜间轻量支持",
        rationale_evidence_refs=["claim-1"],
        delivery=delivery,
        activatable=True,
    )
    strategy = CareStrategy(
        strategy_id="strategy-night-support",
        disposition="propose",
        evidence_packet_refs=[evidence.work_product_ref],
        primary_action=action,
    )
    with pytest.raises(AcceptanceError, match="CoordinationCandidate"):
        accept_care(
            envelope(
                AgentId.CARE_STRATEGY,
                strategy,
                refs=[evidence.work_product_ref],
            ),
            snapshot=snapshot(),
            accepted_evidence=evidence,
            catalog=CareActionCatalog(),
        )

    coordinated = strategy.model_copy(
        update={
            "coordination_candidates": [
                CoordinationCandidate(
                    candidate_id="coordination-family-1",
                    recipient_role="family",
                    reason="夜间活动需要家属关注",
                    timing="immediate",
                    dedupe_key="night-support:family:subject-1",
                )
            ]
        }
    )
    accepted = accept_care(
        envelope(
            AgentId.CARE_STRATEGY,
            coordinated,
            refs=[evidence.work_product_ref],
        ),
        snapshot=snapshot(),
        accepted_evidence=evidence,
        catalog=CareActionCatalog(),
    )
    assert accepted.payload["primary_action"]["delivery"]["notify_family"] is True


def test_care_gate_rejects_active_catalog_contraindication() -> None:
    constrained_snapshot = snapshot(
        active_constraint_codes=("avoid-fixed-wake-time",)
    )
    evidence = accept_evidence(
        envelope(AgentId.EVIDENCE_REASONING, evidence_packet()).model_copy(
            update={
                "fact_snapshot_hash": constrained_snapshot.fact_snapshot_hash,
            }
        ),
        snapshot=constrained_snapshot,
        tool_receipts=[
            receipt().model_copy(
                update={
                    "fact_snapshot_hash": constrained_snapshot.fact_snapshot_hash,
                }
            )
        ],
    )
    strategy = CareStrategy(
        strategy_id="strategy-contraindicated",
        disposition="propose",
        evidence_packet_refs=[evidence.work_product_ref],
        primary_action=care_action(),
    )
    care_envelope = envelope(
        AgentId.CARE_STRATEGY,
        strategy,
        refs=[evidence.work_product_ref],
    ).model_copy(update={"fact_snapshot_hash": constrained_snapshot.fact_snapshot_hash})
    catalog = CareActionCatalog(
        [
            CareActionDefinition(
                care_action_id="consistent-wake-time",
                version=1,
                allowed_parameters={"tolerance_minutes": (0, 60)},
                contraindication_codes=["avoid-fixed-wake-time"],
            )
        ]
    )

    with pytest.raises(AcceptanceError, match="active constraint"):
        accept_care(
            care_envelope,
            snapshot=constrained_snapshot,
            accepted_evidence=evidence,
            catalog=catalog,
        )


def test_safety_trigger_is_conditional_and_target_bound() -> None:
    evidence = accept_evidence(
        envelope(
            AgentId.EVIDENCE_REASONING,
            evidence_packet(low_confidence=True),
        ),
        snapshot=snapshot(),
        tool_receipts=[receipt()],
    )
    assert safety_trigger_reasons(
        target=evidence, episode_type="morning_review"
    ) == ["conflicting_or_low_confidence_personal_claim"]
    decision = SafetyDecision(
        verdict=SafetyVerdict.APPROVE,
        review_target_type=evidence.agent_id.value,
        review_target_id=evidence.target_id,
        review_target_hash=evidence.target_hash,
        fact_snapshot_hash=evidence.fact_snapshot_hash,
        reviewed_episode_state_revision=evidence.episode_state_revision,
        policy_version="product-safety.v3",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    accepted = accept_safety(
        envelope(AgentId.SAFETY_REVIEW, decision),
        snapshot=snapshot(),
        target=evidence,
    )
    assert accepted.payload["verdict"] == SafetyVerdict.APPROVE.value
    stale = decision.model_copy(update={"review_target_hash": "f" * 64})
    with pytest.raises(AcceptanceError, match="hash"):
        accept_safety(
            envelope(AgentId.SAFETY_REVIEW, stale),
            snapshot=snapshot(),
            target=evidence,
        )


def test_communication_gate_and_postflight_prevent_semantic_drift() -> None:
    evidence = accept_evidence(
        envelope(AgentId.EVIDENCE_REASONING, evidence_packet()),
        snapshot=snapshot(),
        tool_receipts=[receipt()],
    )
    draft = CommunicationDraft(
        draft_id="draft-1",
        audience_role="elder",
        text="昨夜记录覆盖充分。",
        claim_refs=["claim-1"],
        semantic_bindings=[
            CommunicationSemanticBinding(
                binding_id="binding-claim-1",
                source_kind="evidence_claim",
                source_ref="claim-1",
                rendered_text="昨夜记录覆盖充分。",
            )
        ],
        context_notice="依据昨夜数据。",
    )
    communication = accept_communication(
        envelope(
            AgentId.SLEEP_CARE,
            draft,
            refs=[evidence.work_product_ref],
        ),
        snapshot=snapshot(),
        accepted_evidence=evidence,
    )
    assert communication.agent_id == AgentId.SLEEP_CARE
    publication_postflight(
        draft,
        accepted_evidence=evidence,
        accepted_care=None,
        safety=None,
        safety_required=False,
    )
    drift = draft.model_copy(update={"claim_refs": ["invented"]})
    with pytest.raises(Exception, match="unsupported"):
        publication_postflight(
            drift,
            accepted_evidence=evidence,
            accepted_care=None,
            safety=None,
            safety_required=False,
        )


def test_communication_rejects_number_not_in_reviewed_knowledge() -> None:
    draft = CommunicationDraft(
        draft_id="knowledge-draft",
        audience_role="elder",
        text="一般建议是保持 12 小时睡眠。",
        semantic_bindings=[
            CommunicationSemanticBinding(
                binding_id="knowledge-binding",
                source_kind="general_knowledge",
                source_ref="knowledge:reviewed:1",
                rendered_text="一般建议是保持 12 小时睡眠。",
                preserved_numbers=["12"],
            )
        ],
        context_notice="这是一般知识，不是个人结论。",
    )

    with pytest.raises(AcceptanceError, match="upstream number"):
        accept_communication(
            envelope(AgentId.SLEEP_CARE, draft),
            snapshot=snapshot(),
            reviewed_knowledge_refs={"knowledge:reviewed:1"},
            reviewed_knowledge_payloads={
                "knowledge:reviewed:1": {
                    "reviewed_text": "多数成年人通常需要 7 到 9 小时睡眠。"
                }
            },
        )


def test_communication_binding_canonicalizes_numeric_provenance_metadata() -> None:
    binding = CommunicationSemanticBinding(
        binding_id="canonical-number-binding",
        source_kind="evidence_claim",
        source_ref="claim:1",
        rendered_text="Compared with 7 nights, the value changed by 12%.",
        preserved_numbers=["model-supplied-wrong-value"],
    )

    assert binding.preserved_numbers == ["12%", "7"]


@pytest.mark.parametrize(
    ("field", "tampered_value"),
    (
        ("expected_target_id", "care-tampered"),
        ("expected_target_hash", "f" * 64),
        ("expected_fact_snapshot_id", "snapshot-tampered"),
        ("expected_fact_snapshot_hash", "f" * 64),
        ("expected_policy_version", "tampered-policy"),
    ),
)
def test_hds_rejects_tampered_capability_binding(
    field: str,
    tampered_value: str,
) -> None:
    action = care_action()
    snap = snapshot()
    service, request = _approved_decision(
        action_kind="care",
        action_scope="activate_care",
        target_id=action.candidate_id,
        target_hash=action.candidate_hash,
        fact_snapshot=snap,
    )
    expected = {
        "expected_proposal_id": request.proposal.proposal_id,
        "expected_subject_id": request.proposal.subject_id,
        "expected_target_id": request.proposal.target_id,
        "expected_target_hash": request.proposal.target_hash,
        "expected_action_scope": request.proposal.action_scope,
        "expected_fact_snapshot_id": snap.fact_snapshot_id,
        "expected_fact_snapshot_hash": snap.fact_snapshot_hash,
        "expected_policy_version": HITL_POLICY_VERSION,
    }
    expected[field] = tampered_value

    with pytest.raises(HumanDecisionError, match="bound"):
        service.acquire_verified_capability(
            request.decision_id,
            **expected,
            idempotency_key="care:tampered",
            now=NOW + timedelta(minutes=2),
        )


def test_hds_rejects_expired_approval_before_capability_acquisition() -> None:
    action = care_action()
    snap = snapshot()
    service, request = _approved_decision(
        action_kind="care",
        action_scope="activate_care",
        target_id=action.candidate_id,
        target_hash=action.candidate_hash,
        fact_snapshot=snap,
        expires_at=NOW + timedelta(seconds=90),
    )

    with pytest.raises(HumanDecisionError, match="expired"):
        service.acquire_verified_capability(
            request.decision_id,
            expected_proposal_id=request.proposal.proposal_id,
            expected_subject_id=request.proposal.subject_id,
            expected_target_id=request.proposal.target_id,
            expected_target_hash=request.proposal.target_hash,
            expected_action_scope=request.proposal.action_scope,
            expected_fact_snapshot_id=snap.fact_snapshot_id,
            expected_fact_snapshot_hash=snap.fact_snapshot_hash,
            expected_policy_version=HITL_POLICY_VERSION,
            idempotency_key="care:expired",
            now=NOW + timedelta(minutes=2),
        )
    assert service.get(request.decision_id).status == HumanDecisionStatus.EXPIRED


def test_commit_recovery_uses_policy_frozen_at_hds_linearization() -> None:
    action = care_action()
    snap = snapshot()
    frozen_policy = "product-safety.v2"
    service, request = _approved_decision(
        action_kind="care",
        action_scope="activate_care",
        target_id=action.candidate_id,
        target_hash=action.candidate_hash,
        fact_snapshot=snap,
        policy_version=frozen_policy,
    )
    capability = service.acquire_verified_capability(
        request.decision_id,
        expected_proposal_id=request.proposal.proposal_id,
        expected_subject_id=request.proposal.subject_id,
        expected_target_id=request.proposal.target_id,
        expected_target_hash=request.proposal.target_hash,
        expected_action_scope=request.proposal.action_scope,
        expected_fact_snapshot_id=snap.fact_snapshot_id,
        expected_fact_snapshot_hash=snap.fact_snapshot_hash,
        expected_policy_version=frozen_policy,
        idempotency_key="care:policy-upgrade-recovery",
        now=NOW + timedelta(minutes=2),
    )

    receipt = DeterministicCommitController().activate_care(
        action=action,
        subject_id="subject-1",
        expected_version=0,
        fact_snapshot=snap,
        idempotency_key="care:policy-upgrade-recovery",
        approval_capability=capability,
    )

    assert receipt.outcome == InvocationOutcome.SUCCEEDED
    _assert_authority_refs(receipt, capability)


def test_commit_controller_rejects_fabricated_or_wrong_key_capability() -> None:
    action = care_action()
    snap = snapshot()
    _service, capability = _approved_capability(
        action_kind="care",
        action_scope="activate_care",
        target_id=action.candidate_id,
        target_hash=action.candidate_hash,
        idempotency_key="care:authorized",
        fact_snapshot=snap,
    )
    forged = object.__new__(VerifiedApprovalCapability)
    object.__setattr__(forged, "_grant", capability.grant)
    controller = DeterministicCommitController()

    with pytest.raises(ConfirmationError, match="stale|bound"):
        controller.activate_care(
            action=action,
            subject_id="subject-1",
            expected_version=0,
            fact_snapshot=snap,
            idempotency_key="care:authorized",
            approval_capability=forged,
        )
    with pytest.raises(ConfirmationError, match="stale|bound"):
        controller.activate_care(
            action=action,
            subject_id="subject-1",
            expected_version=0,
            fact_snapshot=snap,
            idempotency_key="care:wrong-key",
            approval_capability=capability,
        )
    assert controller.care_store.get("subject-1").version == 0


def test_commit_controller_enforces_single_action_and_idempotency() -> None:
    store = InMemoryCareContextStore()
    controller = DeterministicCommitController(care_store=store)
    action = care_action()
    snap = snapshot()
    service, capability = _approved_capability(
        action_kind="care",
        action_scope="activate_care",
        target_id=action.candidate_id,
        target_hash=action.candidate_hash,
        idempotency_key="care:1",
        fact_snapshot=snap,
    )
    first = controller.activate_care(
        action=action,
        subject_id="subject-1",
        expected_version=0,
        fact_snapshot=snap,
        idempotency_key="care:1",
        approval_capability=capability,
    )
    replay = controller.activate_care(
        action=action,
        subject_id="subject-1",
        expected_version=0,
        fact_snapshot=snap,
        idempotency_key="care:1",
        approval_capability=capability,
    )
    assert first == replay
    assert first.outcome == InvocationOutcome.SUCCEEDED
    assert store.get("subject-1").version == 1
    _assert_authority_refs(first, capability)
    _assert_authority_refs(replay, capability)
    decision = service.record_execution_result(
        capability,
        status=HumanDecisionStatus.COMMITTED,
        receipt_ref=first.tool_invocation_id,
        now=NOW + timedelta(minutes=3),
    )
    assert decision.status == HumanDecisionStatus.COMMITTED


def test_care_transition_preserves_cross_day_lifecycle_history() -> None:
    store = InMemoryCareContextStore()
    controller = DeterministicCommitController(care_store=store)
    action = care_action()
    snap = snapshot()
    activation_service, activation_capability = _approved_capability(
        action_kind="care",
        action_scope="activate_care",
        target_id=action.candidate_id,
        target_hash=action.candidate_hash,
        idempotency_key="care:activate",
        fact_snapshot=snap,
    )
    activation_receipt = controller.activate_care(
        action=action,
        subject_id="subject-1",
        expected_version=0,
        fact_snapshot=snap,
        idempotency_key="care:activate",
        approval_capability=activation_capability,
    )
    _assert_authority_refs(activation_receipt, activation_capability)
    activation_service.record_execution_result(
        activation_capability,
        status=HumanDecisionStatus.COMMITTED,
        receipt_ref=activation_receipt.tool_invocation_id,
        now=NOW + timedelta(minutes=3),
    )
    strategy = CareStrategy(
        strategy_id="strategy-pause-1",
        disposition="pause",
        evidence_packet_refs=["evidence:followup:1"],
        transition_confirmation_required=True,
    )
    strategy_hash = "d" * 64
    transition_service, transition_capability = _approved_capability(
        action_kind="care",
        action_scope="transition_care",
        target_id=strategy.strategy_id,
        target_hash=strategy_hash,
        idempotency_key="care:pause",
        fact_snapshot=snap,
    )

    receipt = controller.transition_care(
        strategy=strategy,
        strategy_target_hash=strategy_hash,
        subject_id="subject-1",
        expected_version=1,
        fact_snapshot=snap,
        idempotency_key="care:pause",
        approval_capability=transition_capability,
    )

    state = store.get("subject-1")
    assert receipt.outcome == InvocationOutcome.SUCCEEDED
    assert state.version == 2
    assert state.active_primary_action is None
    assert [item.disposition for item in state.transition_history] == [
        "propose",
        "pause",
    ]
    assert state.transition_history[-1].from_candidate_id == action.candidate_id
    assert state.transition_history[-1].evidence_packet_refs == [
        "evidence:followup:1"
    ]
    _assert_authority_refs(receipt, transition_capability)
    transition_decision = transition_service.record_execution_result(
        transition_capability,
        status=HumanDecisionStatus.COMMITTED,
        receipt_ref=receipt.tool_invocation_id,
        now=NOW + timedelta(minutes=3),
    )
    assert transition_decision.status == HumanDecisionStatus.COMMITTED


def test_memory_is_a_versioned_service_not_an_agent() -> None:
    store = InMemoryMemoryContextStore()
    candidate = MemoryChangeCandidate(
        candidate_id="memory-1",
        operation="create",
        subject_id="subject-1",
        memory_type="preference",
        concept_id="sleep.weekend_wake_preference",
        value_schema_id="bounded_string.v1",
        typed_value="希望周末晚起半小时",
        provenance_type="elder_confirmed",
        source_ref="user_report:1",
        sensitivity_class="personal",
        allowed_roles=(AgentId.SLEEP_CARE, AgentId.EVIDENCE_REASONING),
        allowed_purposes=(
            "personal_evidence_context",
            "explicit_memory_review",
            "explicit_memory_change",
            "explicit_memory_forget",
        ),
        explicit_user_authorization=True,
    )
    updated = store.apply(
        candidate,
        expected_version=0,
        confirmed=True,
        fact_snapshot=snapshot(),
        confirmation_ref="confirmation:memory-1",
    )
    assert updated.version == 1
    assert updated.items[0].source_ref == "user_report:1"
    with pytest.raises(StaleStateError):
        store.apply(
            candidate,
            expected_version=0,
            confirmed=True,
            fact_snapshot=snapshot(),
            confirmation_ref="confirmation:memory-1",
        )
