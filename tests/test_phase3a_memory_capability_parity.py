from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from sleepagent.radar_agent.agents import (
    ContextPacket,
    EvidencePacket,
    MemoryAgent,
    TaskContext,
)
from sleepagent.radar_agent.product_agent.contracts import AgentId
from sleepagent.radar_agent.product_agent.skill_methods.memory import (
    MemoryCapabilityInput,
    MemoryChangeRequest,
    SleepCareMemorySkill,
)
from sleepagent.radar_agent.schemas import (
    EvidenceClaim,
    EvidenceLedger,
    RadarDataQualityStatus,
    RadarNightSummary,
    ReviewStatus,
    RiskLevel,
)


def test_memory_capabilities_are_routed_to_governed_memory_digest_and_care_state() -> None:
    context = _context()
    legacy = MemoryAgent().run(context).output_payload["memory_proposal"]
    legacy_types = {item["memory_type"] for item in legacy["candidates"]}

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

    assert legacy_types == {"trend", "preference", "care_event"}
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


def _context() -> ContextPacket:
    ref = "night-summary:phase3a-memory"
    summary = RadarNightSummary(
        radar_device_id="radar-phase3a",
        subject_id="elder-phase3a",
        night_of=date(2026, 7, 10),
        data_coverage_ratio=0.92,
        data_quality_status=RadarDataQualityStatus.GOOD,
        source_report_ref=ref,
    )
    claim = EvidenceClaim(
        claim_id="claim-phase3a-memory",
        task_id="task-phase3a-memory",
        text="Accepted trend evidence.",
        evidence_refs=[ref],
        confidence=0.8,
        risk_level=RiskLevel.WATCH,
        generated_by="evidence_reasoning",
        review_status=ReviewStatus.REVIEWED,
    )
    ledger = EvidenceLedger(
        ledger_id="ledger-phase3a-memory",
        task_id="task-phase3a-memory",
        canonical_evidence_refs=[ref],
        derived_metrics={
            "risk_level": "watch",
            "data_quality_status": "good",
        },
        claims=[claim],
        confidence=0.8,
        review_status=ReviewStatus.REVIEWED,
    )
    return ContextPacket(
        task_context=TaskContext(
            task_id="task-phase3a-memory",
            trace_id="trace-phase3a-memory",
            purpose="memory",
        ),
        evidence_packet=EvidencePacket(
            night_summaries=[summary],
            evidence_ledger=ledger,
            data_quality={
                "trend_result": {
                    "windows": {
                        key: [
                            {
                                "metric_name": "sleep_minutes",
                                "status": "computed",
                                "value": 380,
                                "evidence_refs": [ref],
                            }
                        ]
                        for key in ("7", "30", "90")
                    }
                },
                "preference_updates": {
                    "expression_style": "简洁温和"
                },
                "care_events": [
                    {
                        "event_type": "care_plan",
                        "status": "candidate",
                        "occurred_at": datetime(
                            2026, 7, 10, tzinfo=timezone.utc
                        ).isoformat(),
                    }
                ],
            },
        ),
    )
