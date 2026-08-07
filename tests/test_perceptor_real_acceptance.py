from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from sleepagent.integrations.perceptor import (
    RealAcceptanceStage,
    RealAcceptanceStageVerdict,
    RealAcceptanceStatus,
    pending_real_perceptor_acceptance,
)
from sleepagent.sleep_domain import VerificationReviewerKind


NOW = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)


def test_missing_real_vendor_facts_produce_eight_pending_verdicts() -> None:
    report = pending_real_perceptor_acceptance(
        namespace_id="live:perceptor-acceptance-pending",
        adapter_artifact_sha256="a" * 64,
        configuration_fingerprint="b" * 64,
        reviewed_at=NOW,
        blocker=(
            "Real Perceptor credentials, device binding and a complete "
            "one-night evidence set are unavailable."
        ),
    )

    assert {item.stage for item in report.verdicts} == set(RealAcceptanceStage)
    assert {item.status for item in report.verdicts} == {
        RealAcceptanceStatus.PENDING
    }
    assert len(report.capability_receipts) == 3
    assert all(
        item.status.value == "pending" for item in report.capability_receipts
    )
    platform_stages = {
        RealAcceptanceStage.BINDING,
        RealAcceptanceStage.NIGHT_EPISODE,
        RealAcceptanceStage.DETERMINISTIC_FAST_PATH,
        RealAcceptanceStage.AGENT_SLOW_PATH,
        RealAcceptanceStage.API_REFERENCE_CLIENT,
    }
    assert all(
        verdict.receipt_not_applicable_reason
        == "platform stage is not an AdapterCapability"
        for verdict in report.verdicts
        if verdict.stage in platform_stages
    )
    assert report.duplicate_push_checked is False
    assert report.report_repull_checked is False


def test_fixture_or_automated_review_cannot_be_verified() -> None:
    common = {
        "stage": RealAcceptanceStage.TRANSPORT,
        "status": RealAcceptanceStatus.VERIFIED,
        "namespace_id": "live:perceptor-acceptance",
        "device_scope_sha256": "c" * 64,
        "local_sleep_date": date(2026, 7, 29),
        "evidence_references": ("real-evidence:transport",),
        "evidence_sha256": ("d" * 64,),
        "test_result_references": ("real-test:transport",),
        "test_result_sha256": ("e" * 64,),
        "capability_receipt_ids": ("receipt:push",),
        "reviewed_at": NOW,
    }
    with pytest.raises(ValidationError, match="human reviewer"):
        RealAcceptanceStageVerdict(
            **common,
            reviewer_kind=VerificationReviewerKind.AUTOMATED,
        )
    with pytest.raises(ValidationError, match="fixture"):
        RealAcceptanceStageVerdict(
            **{
                **common,
                "evidence_references": ("fixture:transport",),
            },
            reviewer_kind=VerificationReviewerKind.HUMAN,
            reviewed_by_actor_id="acceptance-reviewer",
        )


def test_pending_and_failed_verdicts_require_explicit_reason() -> None:
    with pytest.raises(ValidationError, match="blocking"):
        RealAcceptanceStageVerdict(
            stage=RealAcceptanceStage.BINDING,
            status=RealAcceptanceStatus.PENDING,
            namespace_id="live:perceptor-acceptance",
            receipt_not_applicable_reason=(
                "platform stage is not an AdapterCapability"
            ),
            reviewer_kind=VerificationReviewerKind.AUTOMATED,
            reviewed_at=NOW,
        )
    failed = RealAcceptanceStageVerdict(
        stage=RealAcceptanceStage.BINDING,
        status=RealAcceptanceStatus.FAILED,
        namespace_id="live:perceptor-acceptance",
        receipt_not_applicable_reason="platform stage is not an AdapterCapability",
        failure_code="binding_authority_mismatch",
        reviewer_kind=VerificationReviewerKind.AUTOMATED,
        reviewed_at=NOW,
    )
    assert failed.failure_code == "binding_authority_mismatch"
