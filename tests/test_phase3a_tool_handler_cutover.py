from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from sleepagent.radar_agent.product_agent import (
    AgentId,
    AuthenticatedBinding,
    FactSnapshot,
    InvocationOutcome,
    ProductToolExecutionContext,
    ProductToolExecutor,
    SourceScope,
    SourceScopeKind,
)
from sleepagent.radar_agent.product_agent.contracts import (
    EvidencePacket as ProductEvidencePacket,
    MultifactorSafetyInput,
)
from sleepagent.radar_agent.schemas import (
    ContextPacket,
    EvidenceLedger,
    EvidencePacket,
    RadarNightSummary,
    ReviewStatus,
    TaskContext,
)
from sleepagent.sleep_domain.contracts import DataMode
from sleepagent.sleep_domain.product_data import ProductRevisionFacts


NOW = datetime(2026, 8, 7, 8, 0, tzinfo=timezone.utc)


def test_product_urgent_handler_uses_full_canonical_policy_term_set() -> None:
    result = _execute(
        "risk.match_urgent_boundary",
        {"text": "I have shortness of breath now"},
    )

    assert result.receipt.outcome is InvocationOutcome.SUCCEEDED
    assert result.receipt.output["urgent"] is True
    assert result.receipt.output["matched_term"] == "shortness of breath"
    assert result.receipt.source_refs[0].startswith(
        "user-text:urgent-boundary:"
    )


def test_agent_cannot_self_attest_risk_facts() -> None:
    context = ProductToolExecutionContext(
        caller=AgentId.SAFETY_REVIEW,
        fact_snapshot=_snapshot(),
        episode_id="episode-phase3a-handler",
    )
    executor = ProductToolExecutor()

    risk = executor.execute(
        "risk.classify_signal",
        {
            "data": {
                "risk_state": "no_reviewed_signal",
                "data_sufficiency": "sufficient",
                "reason_codes": ["caller_supplied_normal"],
            },
            "source_refs": ["evidence:handler"],
        },
        context=context,
    )
    assert risk.receipt.outcome is InvocationOutcome.FAILED

    with pytest.raises(Exception, match="risk.match_urgent_boundary"):
        executor.execute(
            "risk.match_urgent_boundary",
            {"text": "caller supplied chest pain"},
            context=context,
        )


def test_exact_risk_refs_must_be_bound_to_fact_snapshot() -> None:
    result = _execute(
        "risk.classify_signal",
        {
            "data": {
                "risk_state": "no_reviewed_signal",
                "data_sufficiency": "sufficient",
                "reason_codes": ["no_reviewed_signal"],
            },
            "source_refs": ["risk:not-authorized"],
        },
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_product_risk_handler_uses_exact_revision_state_not_scalar_score() -> None:
    result = _execute(
        "risk.classify_signal",
        {
            "score": 0.4,
            "data": {
                "risk_state": "reviewed_signal",
                "data_sufficiency": "sufficient",
                "health_escalation_allowed": True,
                "reason_codes": ["approved_vendor_alert"],
            },
            "source_refs": ["current_risk:risk-1"],
        },
    )

    assert result.receipt.outcome is InvocationOutcome.SUCCEEDED
    assert result.receipt.output["risk_level"] == "escalate"
    assert result.receipt.output["reason_codes"] == ["approved_vendor_alert"]
    assert result.receipt.output["safety_required"] is True
    assert result.receipt.source_refs == ["current_risk:risk-1"]


def test_product_revision_facts_binds_risk_to_revision_data_sufficiency() -> None:
    facts = ProductRevisionFacts(
        night_episode_id="night-1",
        night_episode_revision_id="revision-1",
        night_episode_revision_number=1,
        subject_id="subject-1",
        data_mode=DataMode.REPLAY,
        timezone_name="Asia/Shanghai",
        local_sleep_date="2026-08-07",
        data_sufficiency="sufficient",
        canonical_observations=(),
        deterministic_quality={"data_mode": "replay", "coverage_ratio": 1.0},
        deterministic_risk={
            "data_mode": "replay",
            "risk_state": "no_reviewed_signal",
            "reason_codes": ["no_reviewed_signal_in_source_scope"],
        },
        conflict_summaries=(),
        provenance_references=("night_episode_revision:replay:revision-1",),
        canonical_data_version="c" * 64,
    )

    risk_input = facts.tool_inputs()["risk.classify_signal"]
    assert risk_input["data"]["data_sufficiency"] == "sufficient"
    assert "score" not in risk_input


def test_product_risk_handler_keeps_multifactor_runtime_compatibility_fields() -> None:
    factors = MultifactorSafetyInput(
        absolute_red_flag=True,
        absolute_red_flag_codes=("fall_with_injury",),
        absolute_red_flag_requires_urgent=True,
        source_refs=("event:urgent",),
    )
    result = _execute(
        "risk.classify_signal",
        {"safety_factors": factors.model_dump(mode="json")},
    )

    assert result.receipt.output["risk_level"] == "escalate"
    assert result.receipt.output["personalization_effect"] == "explanation_only"
    assert result.receipt.output["urgent_required"] is True


def test_missing_risk_facts_fail_closed_as_uncertain_not_normal() -> None:
    result = _execute("risk.classify_signal", {})

    assert result.receipt.outcome is InvocationOutcome.SUCCEEDED
    assert result.receipt.output["risk_level"] == "uncertain"
    assert result.receipt.output["quality_status"] == "missing"
    assert result.receipt.output["quality_blocks_escalation"] is True
    assert result.receipt.output["should_stop_sleep_trend_explanation"] is True


def test_product_knowledge_handler_derives_reviewed_trust_from_service() -> None:
    result = _execute(
        "knowledge.retrieve_reviewed",
        {"query": "设备离线", "roles": ["family"], "limit": 3},
    )

    assert result.receipt.outcome is InvocationOutcome.SUCCEEDED
    assert result.receipt.output["sources"]
    assert all(
        source["review_status"] == "reviewed"
        for source in result.receipt.output["sources"]
    )
    assert result.receipt.source_refs == result.receipt.output["citation_ids"]


def test_product_knowledge_handler_rejects_unauthorized_role_request() -> None:
    result = _execute(
        "knowledge.retrieve_reviewed",
        {"query": "doctor-only material", "roles": ["doctor"]},
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_product_knowledge_handler_derives_subject_from_snapshot() -> None:
    result = _execute(
        "knowledge.retrieve_reviewed",
        {
            "query": "history",
            "roles": ["family"],
            "subject_id": "another-subject",
        },
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_product_artifact_handler_reaches_single_audience_render_tool() -> None:
    ledger = EvidenceLedger(
        ledger_id="ledger:handler",
        task_id="task:handler",
        canonical_evidence_refs=["evidence:handler"],
        derived_metrics={"risk_level": "info"},
        review_status=ReviewStatus.REVIEWED,
    )
    packet = ContextPacket(
        task_context=TaskContext(
            task_id="task:handler",
            trace_id="trace:handler",
            role="family",
            purpose="report",
        ),
        evidence_packet=EvidencePacket(evidence_ledger=ledger),
    )
    result = _execute(
        "artifact.render",
        {
            "context": packet.model_dump(mode="json"),
            "evidence_ledger": ledger.model_dump(mode="json"),
            "audience_role": "family",
        },
    )

    assert result.receipt.outcome is InvocationOutcome.SUCCEEDED
    assert result.receipt.output["artifact"]["role"] == "family"
    assert result.receipt.output["committed"] is False
    assert result.receipt.output["exported"] is False


def test_product_artifact_handler_binds_task_to_current_episode() -> None:
    ledger = EvidenceLedger(
        ledger_id="ledger:wrong-episode",
        task_id="task:artifact",
        canonical_evidence_refs=["evidence:handler"],
        derived_metrics={"risk_level": "info"},
        review_status=ReviewStatus.REVIEWED,
    )
    packet = ContextPacket(
        task_context=TaskContext(
            task_id="task:artifact",
            trace_id="trace:artifact",
            role="family",
            purpose="report",
        ),
        evidence_packet=EvidencePacket(evidence_ledger=ledger),
    )

    result = _execute(
        "artifact.render",
        {
            "context": packet.model_dump(mode="json"),
            "evidence_ledger": ledger.model_dump(mode="json"),
            "audience_role": "family",
        },
        episode_id="another-episode",
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_product_artifact_handler_rejects_ledger_outside_fact_snapshot() -> None:
    ledger = EvidenceLedger(
        ledger_id="ledger:outside-snapshot",
        task_id="task:outside-snapshot",
        canonical_evidence_refs=["evidence:not-authorized"],
        derived_metrics={"risk_level": "info"},
        review_status=ReviewStatus.REVIEWED,
    )
    packet = ContextPacket(
        task_context=TaskContext(
            task_id="task:outside-snapshot",
            trace_id="trace:outside-snapshot",
            role="family",
            purpose="report",
        ),
        evidence_packet=EvidencePacket(evidence_ledger=ledger),
    )

    result = _execute(
        "artifact.render",
        {
            "context": packet.model_dump(mode="json"),
            "evidence_ledger": ledger.model_dump(mode="json"),
            "audience_role": "family",
        },
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_product_artifact_handler_requires_draft_material_for_doctor() -> None:
    ledger = EvidenceLedger(
        ledger_id="ledger:doctor-scope",
        task_id="task:doctor-scope",
        canonical_evidence_refs=["evidence:handler"],
        derived_metrics={"risk_level": "info"},
        review_status=ReviewStatus.REVIEWED,
    )
    packet = ContextPacket(
        task_context=TaskContext(
            task_id="task:doctor-scope",
            trace_id="trace:doctor-scope",
            role="family",
            purpose="report",
        ),
        evidence_packet=EvidencePacket(evidence_ledger=ledger),
    )

    result = _execute(
        "artifact.render",
        {
            "context": packet.model_dump(mode="json"),
            "evidence_ledger": ledger.model_dump(mode="json"),
            "audience_role": "doctor",
        },
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_product_artifact_basis_binds_accepted_evidence_without_render_claim() -> None:
    snapshot = _snapshot()
    accepted_hash = "a" * 64
    result = _execute(
        "artifact.render",
        {
            "episode_id": "episode:product-artifact-basis",
            "accepted_evidence_ref": f"work-product:evidence:{accepted_hash}",
            "accepted_evidence_hash": accepted_hash,
            "evidence_packet": ProductEvidencePacket(
                packet_id="evidence:product-artifact-basis",
                source_scope=snapshot.source_scope,
            ).model_dump(mode="json"),
            "audience_role": "family",
        },
        episode_id="episode:product-artifact-basis",
    )

    assert result.receipt.outcome is InvocationOutcome.SUCCEEDED
    assert result.receipt.output["basis_prepared"] is True
    assert result.receipt.output["rendered"] is False
    assert result.receipt.output["committed"] is False
    assert result.receipt.output["exported"] is False
    assert result.receipt.output["source_refs"] == [
        f"work-product:evidence:{accepted_hash}"
    ]


def test_product_artifact_basis_rejects_doctor_without_draft_scope() -> None:
    snapshot = _snapshot()
    accepted_hash = "b" * 64
    result = _execute(
        "artifact.render",
        {
            "episode_id": "episode:doctor-artifact-basis",
            "accepted_evidence_ref": f"work-product:evidence:{accepted_hash}",
            "accepted_evidence_hash": accepted_hash,
            "evidence_packet": ProductEvidencePacket(
                packet_id="evidence:doctor-artifact-basis",
                source_scope=snapshot.source_scope,
            ).model_dump(mode="json"),
            "audience_role": "doctor",
        },
        episode_id="episode:doctor-artifact-basis",
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_product_artifact_basis_rejects_unbound_evidence_hash() -> None:
    snapshot = _snapshot()
    result = _execute(
        "artifact.render",
        {
            "episode_id": "episode:unbound-artifact-basis",
            "accepted_evidence_ref": f"work-product:evidence:{'c' * 64}",
            "accepted_evidence_hash": "d" * 64,
            "evidence_packet": ProductEvidencePacket(
                packet_id="evidence:unbound-artifact-basis",
                source_scope=snapshot.source_scope,
            ).model_dump(mode="json"),
            "audience_role": "family",
        },
        episode_id="episode:unbound-artifact-basis",
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_agent_cannot_submit_typed_artifact_fact_payload() -> None:
    ledger = EvidenceLedger(
        ledger_id="ledger:agent-forged",
        task_id="task:agent-forged",
        canonical_evidence_refs=["evidence:handler"],
        derived_metrics={"risk_level": "info"},
        review_status=ReviewStatus.REVIEWED,
    )
    packet = ContextPacket(
        task_context=TaskContext(
            task_id="task:agent-forged",
            trace_id="trace:agent-forged",
            role="family",
            purpose="report",
        ),
        evidence_packet=EvidencePacket(evidence_ledger=ledger),
    )
    result = ProductToolExecutor().execute(
        "artifact.render",
        {
            "context": packet.model_dump(mode="json"),
            "evidence_ledger": ledger.model_dump(mode="json"),
            "audience_role": "family",
        },
        context=ProductToolExecutionContext(
            caller=AgentId.SLEEP_CARE,
            fact_snapshot=_snapshot(),
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_product_artifact_handler_rejects_cross_subject_summary() -> None:
    ledger = EvidenceLedger(
        ledger_id="ledger:cross-subject",
        task_id="task:cross-subject",
        canonical_evidence_refs=["evidence:handler"],
        derived_metrics={"risk_level": "info"},
        review_status=ReviewStatus.REVIEWED,
    )
    summary = RadarNightSummary(
        radar_device_id="radar-cross-subject",
        subject_id="another-subject",
        night_of=date(2026, 8, 7),
        source_report_ref="evidence:handler",
    )
    packet = ContextPacket(
        task_context=TaskContext(
            task_id="task:cross-subject",
            trace_id="trace:cross-subject",
            role="family",
            purpose="report",
        ),
        evidence_packet=EvidencePacket(
            night_summaries=[summary],
            evidence_ledger=ledger,
        ),
    )
    result = _execute(
        "artifact.render",
        {
            "context": packet.model_dump(mode="json"),
            "evidence_ledger": ledger.model_dump(mode="json"),
            "audience_role": "family",
        },
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def _execute(
    tool_name: str,
    arguments: dict[str, object],
    *,
    episode_id: str | None = None,
):
    if tool_name == "artifact.render" and episode_id is None:
        packet = arguments.get("context")
        if isinstance(packet, dict):
            task_context = packet.get("task_context")
            if isinstance(task_context, dict):
                episode_id = task_context.get("task_id")
    if episode_id is None:
        episode_id = "episode-phase3a-handler"
    return ProductToolExecutor().execute(
        tool_name,
        arguments,
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=_snapshot(),
            episode_id=episode_id,
        ),
    )


def _snapshot() -> FactSnapshot:
    return FactSnapshot.create(
        fact_snapshot_id="phase3a-handler-snapshot",
        binding=AuthenticatedBinding(
            actor_id="actor-1",
            subject_id="subject-1",
            role="family",
        ),
        source_scope=SourceScope(
            kind=SourceScopeKind.CURRENT_NIGHT,
            as_of=NOW,
            timezone_name="Asia/Shanghai",
            date_start=date(2026, 8, 7),
            date_end=date(2026, 8, 7),
            valid_night_count=1,
        ),
        canonical_data_version="v1",
        source_refs=("evidence:handler", "current_risk:risk-1"),
        created_at=NOW,
    )
