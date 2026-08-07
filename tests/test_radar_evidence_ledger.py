from __future__ import annotations

from datetime import datetime, timezone

from sleepagent.radar_agent.evidence import (
    ClaimReferencePolicy,
    EvidenceLedgerBuilder,
    LedgerFactPurpose,
    build_ledger_fact_packet,
    create_ledger_snapshot,
)
from sleepagent.radar_agent.schemas import EvidenceClaim, ReviewStatus, RiskLevel


NOW = datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc)


def test_ledger_fact_packets_are_the_downstream_fact_source() -> None:
    builder = EvidenceLedgerBuilder(ledger_id="ledger-001", task_id="task-001")
    builder.add_raw_ref("raw:001")
    builder.add_canonical_ref("night-summary:2026-07-10")
    builder.add_metric("total_sleep_minutes", 420)
    builder.add_claim(
        EvidenceClaim(
            claim_id="claim-supported",
            task_id="task-001",
            text="The sleep summary has enough evidence for a family report.",
            evidence_refs=["night-summary:2026-07-10"],
            confidence=0.84,
            risk_level=RiskLevel.INFO,
            generated_by="test",
            review_status=ReviewStatus.REVIEWED,
        )
    )

    ledger = builder.build()

    assert ledger.review_status == ReviewStatus.REVIEWED
    assert ledger.raw_evidence_refs == ["raw:001"]
    assert ledger.canonical_evidence_refs == ["night-summary:2026-07-10"]

    for purpose in LedgerFactPurpose:
        packet = build_ledger_fact_packet(ledger, purpose=purpose)
        assert packet.source == "evidence_ledger"
        assert packet.purpose == purpose
        assert packet.ledger_id == ledger.ledger_id
        assert packet.claims == ledger.claims
        assert packet.derived_metrics["total_sleep_minutes"] == 420
        assert packet.evidence_refs == ["raw:001", "night-summary:2026-07-10"]


def test_claim_without_evidence_refs_is_downgraded_by_default() -> None:
    builder = EvidenceLedgerBuilder(ledger_id="ledger-002", task_id="task-002")
    builder.add_claim(
        EvidenceClaim(
            claim_id="claim-unsupported",
            task_id="task-002",
            text="Unsupported claim should not be emitted as reviewed fact.",
            confidence=0.88,
            risk_level=RiskLevel.ESCALATE,
            generated_by="test",
            review_status=ReviewStatus.DRAFT,
        )
    )

    ledger = builder.build()

    assert ledger.review_status == ReviewStatus.NEEDS_HUMAN_REVIEW
    assert ledger.uncertainty == "missing_evidence_refs"
    assert ledger.claims[0].review_status == ReviewStatus.NEEDS_HUMAN_REVIEW
    assert ledger.claims[0].risk_level == RiskLevel.UNCERTAIN
    assert ledger.claims[0].confidence == 0.2
    assert "missing_evidence_refs" in ledger.claims[0].uncertainty

    packet = build_ledger_fact_packet(ledger, purpose=LedgerFactPurpose.CHAT)
    assert packet.claims[0].review_status == ReviewStatus.NEEDS_HUMAN_REVIEW
    assert packet.uncertainty == "missing_evidence_refs"


def test_unresolved_claim_refs_can_be_rejected() -> None:
    builder = EvidenceLedgerBuilder(
        ledger_id="ledger-003",
        task_id="task-003",
        claim_reference_policy=ClaimReferencePolicy.REJECT,
    )
    builder.add_canonical_ref("night-summary:known")
    builder.add_claim(
        EvidenceClaim(
            claim_id="claim-unresolved",
            task_id="task-003",
            text="This claim references evidence outside the ledger.",
            evidence_refs=["night-summary:missing"],
            confidence=0.75,
            risk_level=RiskLevel.WATCH,
            generated_by="test",
            review_status=ReviewStatus.REVIEWED,
        )
    )

    ledger = builder.build()

    assert ledger.claims == []
    assert ledger.review_status == ReviewStatus.NEEDS_HUMAN_REVIEW
    assert ledger.derived_metrics["rejected_claim_ids"] == ["claim-unresolved"]
    assert ledger.uncertainty == "unresolved_evidence_refs:night-summary:missing"
    assert ledger.caveats == [
        "Rejected claim claim-unresolved because "
        "unresolved_evidence_refs:night-summary:missing."
    ]


def test_data_quality_block_records_explicit_not_interpretable_scopes() -> None:
    builder = EvidenceLedgerBuilder(ledger_id="ledger-004", task_id="task-004")
    builder.mark_uninterpretable_scope(
        scopes=["sleep_health_conclusion", "risk_signal"],
        reason="coverage_below_minimum",
        evidence_refs=["night-summary:low-quality"],
    )

    ledger = builder.build()
    packet = build_ledger_fact_packet(ledger, purpose=LedgerFactPurpose.REPORT)

    assert ledger.review_status == ReviewStatus.NEEDS_HUMAN_REVIEW
    assert ledger.uncertainty == "data_quality_not_interpretable"
    assert ledger.derived_metrics["not_interpretable_scopes"] == [
        "sleep_health_conclusion",
        "risk_signal",
    ]
    assert ledger.derived_metrics["not_interpretable_reason"] == "coverage_below_minimum"
    assert packet.caveats == [
        "Data quality is insufficient; the following scopes are not "
        "interpretable: sleep_health_conclusion, risk_signal."
    ]


def test_ledger_snapshots_version_full_ledger_content() -> None:
    builder = EvidenceLedgerBuilder(ledger_id="ledger-005", task_id="task-005")
    builder.add_canonical_ref("night-summary:stable")
    builder.add_claim(
        EvidenceClaim(
            claim_id="claim-stable",
            task_id="task-005",
            text="Stable evidence-backed claim.",
            evidence_refs=["night-summary:stable"],
            confidence=0.7,
            generated_by="test",
            review_status=ReviewStatus.REVIEWED,
        )
    )
    ledger_v1 = builder.build()
    snapshot_v1 = create_ledger_snapshot(
        ledger_v1,
        version_number=1,
        created_at=NOW,
    )

    builder.add_metric("sleep_score", 82)
    ledger_v2 = builder.build()
    snapshot_v2 = create_ledger_snapshot(
        ledger_v2,
        version_number=2,
        created_at=NOW,
    )

    assert snapshot_v1.version_number == 1
    assert snapshot_v2.version_number == 2
    assert snapshot_v1.content_hash != snapshot_v2.content_hash
    assert snapshot_v1.source_claim_ids == ["claim-stable"]
    assert snapshot_v2.ledger.derived_metrics["sleep_score"] == 82
