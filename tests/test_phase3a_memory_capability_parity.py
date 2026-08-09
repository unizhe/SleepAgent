from __future__ import annotations

import pytest

from sleepagent.radar_agent.product_agent.contracts import AgentId
from sleepagent.radar_agent.product_agent.skill_methods.memory import (
    MemoryCapabilityInput,
    MemoryChangeRequest,
    SleepCareMemorySkill,
)
from tests.golden_fixtures import load_phase3a_capability_goldens


def test_memory_capabilities_are_routed_to_governed_memory_digest_and_care_state() -> None:
    expected = load_phase3a_capability_goldens()["memory_routing"]

    routed = SleepCareMemorySkill().route(
        MemoryCapabilityInput(
            subject_id="elder-phase3a",
            accepted_evidence_refs=("evidence:phase3a-memory",),
            risk_level="watch",
            data_quality_status="good",
            preference_changes=(
                MemoryChangeRequest(
                    concept_id="communication.style",
                    memory_type="communication_preference",
                    value_schema_id="bounded_string.v1",
                    typed_value="简洁温和",
                    source_ref="user_report:phase3a-memory",
                    explicit_user_authorization=True,
                ),
            ),
            trend_analysis_refs=("trend:phase3a:7-30-90",),
            care_event_refs=("care-event:phase3a:plan",),
        )
    )

    assert expected["candidate_count"] == 3
    assert set(expected["candidate_types"]) == {
        "trend",
        "preference",
        "care_event",
    }
    assert len(routed.memory_change_candidates) == 1
    assert routed.memory_change_candidates[0].memory_type == (
        "communication_preference"
    )
    assert routed.analysis_artifact_refs == ["trend:phase3a:7-30-90"]
    assert routed.care_state_event_refs == ["care-event:phase3a:plan"]
    assert routed.write_performed is False
    assert routed.memory_change_candidates[0].candidate_hash
    assert routed.memory_change_candidates[0].confirmation_required is True


def test_memory_skill_is_sleepcare_owned_and_never_promotes_trend_or_care_to_profile() -> None:
    routed = SleepCareMemorySkill().route(
        MemoryCapabilityInput(
            subject_id="elder-phase3a",
            accepted_evidence_refs=("evidence:phase3a",),
            risk_level="info",
            data_quality_status="good",
            trend_analysis_refs=("trend:artifact",),
            care_event_refs=("care:event",),
        )
    )

    assert SleepCareMemorySkill.owner is AgentId.SLEEP_CARE
    assert SleepCareMemorySkill.skill_id == "propose_memory_change"
    assert routed.memory_change_candidates == []
    assert routed.analysis_artifact_refs == ["trend:artifact"]
    assert routed.care_state_event_refs == ["care:event"]
    assert not hasattr(routed, "agent_name")


def test_urgent_or_unusable_context_cannot_create_memory_candidates_or_digest_routes() -> None:
    for risk, quality in (
        ("urgent_boundary", "good"),
        ("watch", "unusable"),
    ):
        routed = SleepCareMemorySkill().route(
            MemoryCapabilityInput(
                subject_id="elder-phase3a",
                accepted_evidence_refs=("evidence:phase3a",),
                risk_level=risk,
                data_quality_status=quality,
                preference_changes=(
                    MemoryChangeRequest(
                        concept_id="communication.style",
                        memory_type="communication_preference",
                        value_schema_id="bounded_string.v1",
                        typed_value="brief",
                        source_ref="user_report:phase3a",
                        explicit_user_authorization=True,
                    ),
                ),
                trend_analysis_refs=("trend:blocked",),
                care_event_refs=("care:blocked",),
            )
        )

        assert routed.memory_change_candidates == []
        assert routed.analysis_artifact_refs == []
        assert routed.care_state_event_refs == []
        assert routed.rejected_reasons


def test_raw_ingress_provenance_cannot_be_promoted_to_generic_memory() -> None:
    routed = SleepCareMemorySkill().route(
        MemoryCapabilityInput(
            subject_id="elder-phase3a",
            accepted_evidence_refs=("evidence:phase3a",),
            risk_level="info",
            data_quality_status="good",
            preference_changes=(
                MemoryChangeRequest(
                    concept_id="communication.style",
                    memory_type="communication_preference",
                    value_schema_id="bounded_string.v1",
                    typed_value="brief",
                    source_ref=(
                        "raw_ingress:live:raw-1:sha256:" + "a" * 64
                    ),
                    explicit_user_authorization=True,
                ),
            ),
        )
    )

    assert routed.memory_change_candidates == []
    assert routed.rejected_reasons == [
        "raw_source_not_memory_eligible:communication.style"
    ]


def test_accepted_evidence_provenance_requires_exact_accepted_ref() -> None:
    routed = SleepCareMemorySkill().route(
        MemoryCapabilityInput(
            subject_id="elder-phase3a",
            accepted_evidence_refs=("evidence:accepted",),
            risk_level="info",
            data_quality_status="good",
            preference_changes=(
                MemoryChangeRequest(
                    concept_id="communication.style",
                    memory_type="communication_preference",
                    value_schema_id="bounded_string.v1",
                    typed_value="brief",
                    provenance_type="accepted_evidence",
                    source_ref="evidence:unrelated",
                    explicit_user_authorization=True,
                ),
            ),
        )
    )

    assert routed.memory_change_candidates == []
    assert routed.rejected_reasons == [
        "accepted_evidence_source_not_bound:communication.style"
    ]


def test_memory_replace_targets_existing_governed_memory_id() -> None:
    target_id = "memory:existing-preference"
    routed = SleepCareMemorySkill().route(
        MemoryCapabilityInput(
            subject_id="elder-phase3a",
            accepted_evidence_refs=("evidence:accepted",),
            risk_level="info",
            data_quality_status="good",
            preference_changes=(
                MemoryChangeRequest(
                    operation="replace",
                    target_candidate_id=target_id,
                    concept_id="communication.style",
                    memory_type="communication_preference",
                    value_schema_id="bounded_string.v1",
                    typed_value="warm and brief",
                    source_ref="user_report:replace-preference",
                    explicit_user_authorization=True,
                ),
            ),
        )
    )

    assert routed.memory_change_candidates[0].candidate_id == target_id

    with pytest.raises(ValueError, match="target_candidate_id"):
        MemoryChangeRequest(
            operation="forget",
            concept_id="communication.style",
            memory_type="communication_preference",
            value_schema_id="bounded_string.v1",
            typed_value="forgotten",
            source_ref="user_report:forget-preference",
            explicit_user_authorization=True,
        )


def test_memory_skill_rejects_empty_evidence_handles_and_cannot_claim_write() -> None:
    routed = SleepCareMemorySkill().route(
        MemoryCapabilityInput(
            subject_id="elder-phase3a",
            accepted_evidence_refs=("",),
            risk_level="info",
            data_quality_status="good",
        )
    )

    assert routed.rejected_reasons == ["accepted_evidence_refs_missing"]
    with pytest.raises(ValueError, match="write_performed"):
        type(routed)(write_performed=True)
