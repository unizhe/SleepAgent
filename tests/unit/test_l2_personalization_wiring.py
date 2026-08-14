from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sleepagent.api.product_contracts import (
    HabitChangeRequest,
    MemoryChangeRequest,
)
from sleepagent.domain.habit import HabitFact, HabitOperation
from sleepagent.runtime.contracts import (
    AgentId,
    AuthenticatedBinding,
    ContextPacket,
    EvidenceSemantic,
    EvidenceSourceKind,
    EpisodeStatus,
    EpisodeType,
    FactSnapshot,
    MemoryChangeCandidate,
    SourceScope,
    SourceScopeKind,
    TrustedContextItem,
    TrustLabel,
    stable_hash,
)
from sleepagent.runtime.deterministic_model import (
    DeterministicReplayStructuredAgentModel,
)
from sleepagent.runtime.factory import build_deterministic_product_runtime_bundle
from sleepagent.runtime.invocation import (
    CareStrategyModelOutput,
    EvidenceReasoningModelOutput,
)
from sleepagent.runtime.memory import (
    GovernedMemoryItemV2,
    GovernedMemoryState,
    MemoryReadReceipt,
    MemoryPurpose,
    MemoryQueryIntent,
    ProvenanceType,
    SensitivityClass,
    resolve_memory_query,
    select_memory_slice,
)
from sleepagent.runtime.results import (
    PinnedPersonalizationContext,
    ProductEpisodeRunRequest,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


def _scope() -> SourceScope:
    return SourceScope(
        kind=SourceScopeKind.HISTORICAL_RANGE,
        as_of=NOW,
        timezone_name="Asia/Shanghai",
        date_start=date(2026, 8, 11),
        date_end=date(2026, 8, 13),
        valid_night_count=3,
    )


def _fact() -> HabitFact:
    return HabitFact(
        fact_id="habit-fact:delivery",
        fact_hash="a" * 64,
        revision=1,
        operation=HabitOperation.REMEMBER,
        subject_id="subject-1",
        concept_id="habit.delivery_modality_preference",
        concept_version="1.0.0",
        value="语音",
        value_hash="b" * 64,
        confirmed_at=NOW,
        valid_until=NOW + timedelta(days=90),
        confirmation_ref="confirmation:habit:1",
        change_id="habit-change:1",
    )


def _care_memory_state() -> GovernedMemoryState:
    item = GovernedMemoryItemV2(
        memory_id="memory:care-preference",
        subject_id="subject-1",
        memory_type="communication_preference",
        concept_id="sleep.preference.care_delivery",
        value_schema_id="enum.v1",
        typed_value="morning_voice",
        provenance_type=ProvenanceType.ELDER_CONFIRMED,
        source_ref="user_report:care-preference",
        source_scope_kind=SourceScopeKind.HISTORICAL_RANGE,
        version=1,
        recorded_at=NOW,
        valid_from=NOW,
        sensitivity_class=SensitivityClass.PERSONAL,
        allowed_roles=(AgentId.CARE_STRATEGY,),
        allowed_purposes=(MemoryPurpose.CARE_PREFERENCE_CONTEXT,),
        confirmation_ref="confirmation:memory:1",
        retention_policy_version="sleepagent-retention.v1",
    )
    return GovernedMemoryState(
        subject_id="subject-1",
        version=1,
        revisions=(item,),
    )


def _care_receipt() -> MemoryReadReceipt:
    query = resolve_memory_query(
        MemoryQueryIntent(
            purpose=MemoryPurpose.CARE_PREFERENCE_CONTEXT,
            concept_ids=("sleep.preference.care_delivery",),
            source_scope_kind=SourceScopeKind.HISTORICAL_RANGE,
        ),
        invocation_id="episode-1:care",
        actor_id="workload:worker",
        actor_role="system",
        subject_id="subject-1",
        requesting_agent=AgentId.CARE_STRATEGY,
        authorization_scope=("memory:read",),
        as_of=NOW,
        privacy_epoch=1,
        authorization_epoch=1,
    )
    return select_memory_slice(query, _care_memory_state(), now=NOW)


def test_memory_care_access_is_purpose_scoped_and_safety_is_excluded() -> None:
    receipt = _care_receipt()

    assert len(receipt.items) == 1
    assert receipt.items[0].trust_label == TrustLabel.USER_MEMORY_UNTRUSTED_DATA
    assert receipt.items[0].verified_evidence is False
    assert receipt.items[0].verified_medical_fact is False

    with pytest.raises(PermissionError, match="only Care"):
        resolve_memory_query(
            MemoryQueryIntent(
                purpose=MemoryPurpose.CARE_PREFERENCE_CONTEXT,
                concept_ids=("sleep.preference.care_delivery",),
                source_scope_kind=SourceScopeKind.HISTORICAL_RANGE,
            ),
            invocation_id="episode-1:evidence",
            actor_id="workload:worker",
            actor_role="system",
            subject_id="subject-1",
            requesting_agent=AgentId.EVIDENCE_REASONING,
            authorization_scope=("memory:read",),
            as_of=NOW,
            privacy_epoch=1,
            authorization_epoch=1,
        )
    with pytest.raises(ValidationError, match="Safety"):
        MemoryChangeCandidate(
            candidate_id="memory:unsafe",
            operation="create",
            subject_id="subject-1",
            memory_type="preference",
            concept_id="sleep.preference.unsafe",
            value_schema_id="enum.v1",
            typed_value="anything",
            provenance_type="elder_confirmed",
            source_ref="user_report:unsafe",
            sensitivity_class="personal",
            allowed_roles=(AgentId.SAFETY_REVIEW,),
            allowed_purposes=("personal_evidence_context",),
            explicit_user_authorization=True,
        )


def test_user_reported_health_memory_cannot_become_clinical_evidence() -> None:
    health_memory = GovernedMemoryItemV2(
        memory_id="memory:reported-health",
        subject_id="subject-1",
        memory_type="routine",
        concept_id="sleep.context.reported_health",
        value_schema_id="bounded_string.v1",
        typed_value="用户说医生曾提到严重睡眠呼吸暂停",
        provenance_type=ProvenanceType.ELDER_CONFIRMED,
        source_ref="user_report:reported-health",
        source_scope_kind=SourceScopeKind.HISTORICAL_RANGE,
        version=1,
        recorded_at=NOW,
        valid_from=NOW,
        sensitivity_class=SensitivityClass.SENSITIVE_PERSONAL,
        allowed_roles=(AgentId.EVIDENCE_REASONING,),
        allowed_purposes=(MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT,),
        confirmation_ref="confirmation:reported-health",
        retention_policy_version="sleepagent-retention.v1",
    )
    query = resolve_memory_query(
        MemoryQueryIntent(
            purpose=MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT,
            concept_ids=(health_memory.concept_id,),
            source_scope_kind=SourceScopeKind.HISTORICAL_RANGE,
        ),
        invocation_id="episode-health:evidence",
        actor_id="workload:worker",
        actor_role="system",
        subject_id="subject-1",
        requesting_agent=AgentId.EVIDENCE_REASONING,
        authorization_scope=("memory:read",),
        as_of=NOW,
        privacy_epoch=1,
        authorization_epoch=1,
    )
    receipt = select_memory_slice(
        query,
        GovernedMemoryState(
            subject_id="subject-1",
            version=1,
            revisions=(health_memory,),
        ),
        now=NOW,
    )
    assert receipt.items[0].verified_evidence is False
    assert receipt.items[0].verified_medical_fact is False

    packet = ContextPacket(
        context_packet_id="context:reported-health",
        episode_id="episode-health",
        invocation_id="evidence:reported-health",
        agent_id=AgentId.EVIDENCE_REASONING,
        objective="interpret",
        fact_snapshot_id="snapshot-health",
        fact_snapshot_hash="c" * 64,
        source_scope=_scope(),
        items=(
            TrustedContextItem(
                key="tool:trend.calculate_metrics",
                trust_label=TrustLabel.TOOL_OUTPUT_UNTRUSTED,
                value={"trend": "stable"},
                source_refs=("trend:health-boundary",),
            ),
            TrustedContextItem(
                key="personalization:memory_slice",
                trust_label=TrustLabel.USER_MEMORY_UNTRUSTED_DATA,
                value={
                    "items": [
                        item.model_dump(mode="json") for item in receipt.items
                    ],
                    "untrusted_personal_context": True,
                    "verified_evidence": False,
                    "verified_medical_fact": False,
                },
                source_refs=(receipt.receipt_id,),
            ),
        ),
    )
    output = DeterministicReplayStructuredAgentModel().generate(
        messages=[{"role": "user", "content": packet.model_dump_json()}],
        schema=EvidenceReasoningModelOutput,
        prompt_version="test.v1",
        context_packet_id=packet.context_packet_id,
    ).output_payload
    assert all(
        health_memory.revision_ref not in claim.evidence_refs
        for claim in output.claims
    )
    assert all(
        claim.source_kind != EvidenceSourceKind.CONFIRMED_MEMORY
        for claim in output.claims
    )


def test_pinned_personalization_changes_behavior_without_clinical_promotion() -> None:
    model = DeterministicReplayStructuredAgentModel()
    habit = _fact()
    evidence_packet = ContextPacket(
        context_packet_id="context:evidence",
        episode_id="episode-1",
        invocation_id="evidence:1",
        agent_id=AgentId.EVIDENCE_REASONING,
        objective="interpret",
        fact_snapshot_id="snapshot-1",
        fact_snapshot_hash="c" * 64,
        source_scope=_scope(),
        items=(
            TrustedContextItem(
                key="tool:trend.calculate_metrics",
                trust_label=TrustLabel.TOOL_OUTPUT_UNTRUSTED,
                value={"trend": "stable"},
                source_refs=("trend:1",),
            ),
            TrustedContextItem(
                key="personalization:habit_profile",
                trust_label=TrustLabel.CONFIRMED_HABIT,
                value={
                    "profile_version": 1,
                    "profile_hash": "d" * 64,
                    "facts": [
                        {
                            "fact_id": habit.fact_id,
                            "fact_ref": habit.fact_id,
                            "concept_id": habit.concept_id,
                            "value": habit.value,
                            "clinical_truth": False,
                        }
                    ],
                },
                source_refs=(habit.fact_id,),
            ),
        ),
    )
    evidence = model.generate(
        messages=[{"role": "user", "content": evidence_packet.model_dump_json()}],
        schema=EvidenceReasoningModelOutput,
        prompt_version="test.v1",
        context_packet_id=evidence_packet.context_packet_id,
    )
    habit_claim = evidence.output_payload.claims[-1]
    assert habit_claim.semantic == EvidenceSemantic.USER_REPORTED
    assert habit_claim.source_kind == EvidenceSourceKind.CONFIRMED_HABIT
    assert "不代表临床正常或诊断结论" in habit_claim.statement

    receipt = _care_receipt()
    care_packet = ContextPacket(
        context_packet_id="context:care",
        episode_id="episode-1",
        invocation_id="care:1",
        agent_id=AgentId.CARE_STRATEGY,
        objective="care",
        fact_snapshot_id="snapshot-1",
        fact_snapshot_hash="c" * 64,
        source_scope=_scope(),
        items=(
            TrustedContextItem(
                key="accepted:evidence_packet",
                trust_label=TrustLabel.ACCEPTED_WORK_PRODUCT,
                value=evidence.output_payload,
                source_refs=("work-product:evidence",),
            ),
            TrustedContextItem(
                key="personalization:memory_slice",
                trust_label=TrustLabel.USER_MEMORY_UNTRUSTED_DATA,
                value={
                    "items": [item.model_dump(mode="json") for item in receipt.items],
                    "untrusted_personal_context": True,
                    "verified_evidence": False,
                    "verified_medical_fact": False,
                },
                source_refs=(receipt.receipt_id,),
            ),
        ),
    )
    care = model.generate(
        messages=[{"role": "user", "content": care_packet.model_dump_json()}],
        schema=CareStrategyModelOutput,
        prompt_version="test.v1",
        context_packet_id=care_packet.context_packet_id,
    )
    assert care.output_payload.primary_action is not None
    assert care.output_payload.primary_action.parameters == {
        "tolerance_minutes": 30,
    }
    assert care.output_payload.primary_action.title == (
        "按早晨语音偏好，保持较稳定的起床安排"
    )


def test_product_request_rejects_personalization_snapshot_drift() -> None:
    habit = _fact()
    receipt = _care_receipt()
    profile_hash = stable_hash(
        {
            "subject_id": "subject-1",
            "profile_version": 1,
            "fact_hashes": [habit.fact_hash],
        }
    )
    pinned = PinnedPersonalizationContext(
        subject_id="subject-1",
        habit_profile_version=1,
        habit_profile_hash=profile_hash,
        habit_facts=(habit,),
        memory_state_version=1,
        memory_read_receipts=(receipt,),
    )
    snapshot = FactSnapshot.create(
        fact_snapshot_id="snapshot-1",
        binding=AuthenticatedBinding(
            actor_id="workload:worker",
            subject_id="subject:opaque",
            role="elder",
            authorization_scope=("draft_material",),
        ),
        source_scope=_scope(),
        canonical_data_version="facts.v1",
        memory_context_version=1,
        habit_profile_version=1,
        habit_profile_hash=profile_hash,
        memory_read_receipt_refs=(receipt.receipt_id,),
        memory_read_receipt_hashes=(str(receipt.receipt_hash),),
        source_refs=(habit.fact_id, receipt.handles[0].handle_id),
        created_at=NOW,
    )
    request = ProductEpisodeRunRequest(
        episode_id="episode-1",
        episode_type=EpisodeType.MORNING_REVIEW,
        objective="review",
        fact_snapshot=snapshot,
        personalization=pinned,
    )
    assert request.personalization == pinned

    with pytest.raises(ValidationError, match="exactly match FactSnapshot"):
        ProductEpisodeRunRequest(
            episode_id="episode-1",
            episode_type=EpisodeType.MORNING_REVIEW,
            objective="review",
            fact_snapshot=snapshot.model_copy(
                update={"memory_context_version": 2}
            ),
            personalization=pinned,
        )


def test_real_runner_changes_behavior_only_with_pinned_l2_context() -> None:
    habit = _fact()
    receipt = _care_receipt()
    profile_hash = stable_hash(
        {
            "subject_id": "subject-1",
            "profile_version": 1,
            "fact_hashes": [habit.fact_hash],
        }
    )
    pinned = PinnedPersonalizationContext(
        subject_id="subject-1",
        habit_profile_version=1,
        habit_profile_hash=profile_hash,
        habit_facts=(habit,),
        memory_state_version=1,
        memory_read_receipts=(receipt,),
    )
    binding = AuthenticatedBinding(
        actor_id="workload:worker",
        subject_id="subject:opaque",
        role="elder",
        authorization_scope=("read_sleep_data", "draft_material"),
    )
    tool_inputs = {
        "radar.get_night_evidence": {"source_refs": ["night:1"]},
        "radar.get_range_evidence": {"source_refs": ["range:1"]},
        "radar.assess_data_quality": {
            "coverage_ratio": 0.9,
            "source_refs": ["night:1", "range:1"],
        },
        "trend.calculate_metrics": {
            "values": [7.0, 6.8, 6.5],
            "source_refs": ["range:1"],
        },
        "risk.classify_signal": {
            "data": {
                "risk_state": "no_reviewed_signal",
                "data_sufficiency": "sufficient",
                "health_escalation_allowed": False,
                "reason_codes": ["no_reviewed_signal_in_source_scope"],
            },
            "source_refs": ["night:1", "range:1"],
            "trend_signals": [
                {
                    "risk_level": "watch",
                    "confidence": 0.75,
                    "source_refs": ["range:1"],
                }
            ],
            "trend_observation": {
                "quality_status": "good",
                "confidence_label": "normal",
                "health_conclusion_allowed": True,
                "source_refs": ["range:1"],
            },
        },
        "care.read_catalog": {},
        "care.read_state": {},
        "artifact.render": {"content": "draft"},
    }
    class CapturingPersonalizationModel(
        DeterministicReplayStructuredAgentModel
    ):
        memory_context_items: list[TrustedContextItem]

        def __init__(self) -> None:
            super().__init__()
            self.memory_context_items = []

        def generate(self, **kwargs):
            if kwargs["schema"] is CareStrategyModelOutput:
                packet = ContextPacket.model_validate_json(
                    kwargs["messages"][-1]["content"]
                )
                self.memory_context_items.extend(
                    item
                    for item in packet.items
                    if item.key == "personalization:memory_slice"
                )
            return super().generate(**kwargs)

    model = CapturingPersonalizationModel()
    runner = build_deterministic_product_runtime_bundle(model=model).runner
    baseline = runner.run(
        ProductEpisodeRunRequest(
            episode_id="episode-runner-baseline",
            episode_type=EpisodeType.MORNING_REVIEW,
            objective="review",
            fact_snapshot=FactSnapshot.create(
                fact_snapshot_id="snapshot-runner-baseline",
                binding=binding,
                source_scope=_scope(),
                canonical_data_version="facts.v1",
                source_refs=("night:1", "range:1"),
                created_at=NOW,
            ),
            audience_role="elder",
            tool_inputs=tool_inputs,
        )
    )
    snapshot = FactSnapshot.create(
        fact_snapshot_id="snapshot-runner-personalized",
        binding=binding,
        source_scope=_scope(),
        canonical_data_version="facts.v1",
        memory_context_version=1,
        habit_profile_version=1,
        habit_profile_hash=profile_hash,
        memory_read_receipt_refs=(receipt.receipt_id,),
        memory_read_receipt_hashes=(str(receipt.receipt_hash),),
        source_refs=(
            "night:1",
            "range:1",
            habit.fact_id,
            receipt.receipt_id,
            receipt.handles[0].handle_id,
        ),
        created_at=NOW,
    )
    result = runner.run(
        ProductEpisodeRunRequest(
            episode_id="episode-runner-personalized",
            episode_type=EpisodeType.MORNING_REVIEW,
            objective="review",
            fact_snapshot=snapshot,
            audience_role="elder",
            personalization=pinned,
            tool_inputs=tool_inputs,
        )
    )

    assert baseline.receipt.status == EpisodeStatus.WAITING_CONFIRMATION
    assert baseline.publication is not None
    assert "个人习惯基线" not in baseline.publication.text
    assert result.receipt.status == EpisodeStatus.WAITING_CONFIRMATION
    assert result.publication is not None
    assert "个人习惯基线" in result.publication.text
    evidence = next(
        item
        for item in result.accepted_work_products
        if item.agent_id == AgentId.EVIDENCE_REASONING
    )
    assert any(
        claim["source_kind"] == EvidenceSourceKind.CONFIRMED_HABIT.value
        and claim["evidence_refs"] == [habit.fact_id]
        for claim in evidence.payload["claims"]
    )
    expected_handle = receipt.handles[0].handle_id
    memory_context = model.memory_context_items[-1]
    assert expected_handle in memory_context.source_refs
    assert memory_context.value["items"][0]["retrieval_handle"] == expected_handle
    baseline_care = next(
        item
        for item in baseline.accepted_work_products
        if item.agent_id == AgentId.CARE_STRATEGY
    )
    personalized_care = next(
        item
        for item in result.accepted_work_products
        if item.agent_id == AgentId.CARE_STRATEGY
    )
    assert baseline_care.payload["primary_action"]["parameters"] == {
        "tolerance_minutes": 30,
    }
    assert personalized_care.payload["primary_action"]["parameters"] == {
        "tolerance_minutes": 30,
    }
    assert baseline_care.payload["primary_action"]["title"] == (
        "保持较稳定的起床安排"
    )
    assert personalized_care.payload["primary_action"]["title"] == (
        "按早晨语音偏好，保持较稳定的起床安排"
    )


def test_public_l2_commands_enforce_exact_change_shapes() -> None:
    with pytest.raises(ValidationError, match="exact target"):
        HabitChangeRequest(
            operation="forget",
            concept_id="habit.primary_goal",
            confirmation_actor_id="elder-1",
        )
    with pytest.raises(ValidationError, match="exact target"):
        MemoryChangeRequest(
            operation="correct",
            memory_id="memory:preference",
            concept_id="sleep.preference.care_delivery",
            memory_type="preference",
            value_schema_id="enum.v1",
            typed_value="morning_voice",
            sensitivity_class="personal",
            source_text="Please use morning voice reminders",
            allowed_roles=("care_strategy",),
            allowed_purposes=("care_preference_context",),
        )
