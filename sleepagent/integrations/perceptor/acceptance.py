"""Evidence-bounded verdicts for a real Perceptor one-night acceptance run.

This is intentionally separate from fixture/conformance testing.  Automated
code may create PENDING or FAILED verdicts; VERIFIED requires immutable real
evidence plus an explicit human reviewer.  Adapter capability receipts remain
adapter-scoped and are not stretched to describe platform stages.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from sleepagent.sleep_domain.contracts import (
    AdapterCapability,
    CapabilityVerificationReceipt,
    CapabilityVerificationStatus,
    DataMode,
    Sha256Hex,
    SleepDomainContract,
    VerificationReviewerKind,
)


SINGLE_NIGHT_SCOPE_LIMITATION = (
    "This device-bounded, one-night acceptance does not establish cross-vendor "
    "compatibility, long-term reliability, or clinical validity."
)


class RealAcceptanceStage(str, Enum):
    TRANSPORT = "transport"
    PUSH_AUTHENTICATION = "push_authentication"
    PULL_REPORT_NORMALIZATION = "pull_report_normalization"
    BINDING = "binding"
    NIGHT_EPISODE = "night_episode"
    DETERMINISTIC_FAST_PATH = "deterministic_fast_path"
    AGENT_SLOW_PATH = "agent_slow_path"
    API_REFERENCE_CLIENT = "api_reference_client"


class RealAcceptanceStatus(str, Enum):
    VERIFIED = "verified"
    PENDING = "pending"
    FAILED = "failed"


class RealAcceptanceStageVerdict(SleepDomainContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["real_perceptor_stage_verdict.v1"] = (
        "real_perceptor_stage_verdict.v1"
    )
    stage: RealAcceptanceStage
    status: RealAcceptanceStatus
    namespace_id: str = Field(..., min_length=1)
    data_mode: Literal[DataMode.LIVE] = DataMode.LIVE
    device_scope_sha256: Sha256Hex | None = None
    local_sleep_date: date | None = None
    evidence_references: tuple[str, ...] = ()
    evidence_sha256: tuple[Sha256Hex, ...] = ()
    test_result_references: tuple[str, ...] = ()
    test_result_sha256: tuple[Sha256Hex, ...] = ()
    capability_receipt_ids: tuple[str, ...] = ()
    receipt_not_applicable_reason: str | None = None
    blocking_reasons: tuple[str, ...] = ()
    failure_code: str | None = None
    limitations: tuple[str, ...] = (SINGLE_NIGHT_SCOPE_LIMITATION,)
    reviewer_kind: VerificationReviewerKind
    reviewed_by_actor_id: str | None = None
    reviewed_at: datetime

    @model_validator(mode="after")
    def verdict_is_evidence_bounded(self) -> "RealAcceptanceStageVerdict":
        if not self.namespace_id.startswith("live:"):
            raise ValueError("real acceptance requires an explicit live namespace")
        if len(self.evidence_references) != len(self.evidence_sha256):
            raise ValueError("each evidence reference requires one SHA-256")
        if len(self.test_result_references) != len(self.test_result_sha256):
            raise ValueError("each test result reference requires one SHA-256")
        disallowed = ("fixture", "fake", "replay", "simulated")
        for reference in (
            *self.evidence_references,
            *self.test_result_references,
        ):
            if any(marker in reference.lower() for marker in disallowed):
                raise ValueError(
                    "fixture/fake/replay/simulated material cannot verify a "
                    "real Perceptor stage"
                )
        if SINGLE_NIGHT_SCOPE_LIMITATION not in self.limitations:
            raise ValueError("real acceptance must retain its scope limitation")
        if self.status == RealAcceptanceStatus.VERIFIED:
            if self.reviewer_kind != VerificationReviewerKind.HUMAN:
                raise ValueError("VERIFIED requires a human reviewer")
            if not self.reviewed_by_actor_id:
                raise ValueError("VERIFIED requires reviewed_by_actor_id")
            if (
                self.device_scope_sha256 is None
                or self.local_sleep_date is None
                or not self.evidence_references
                or not self.test_result_references
            ):
                raise ValueError(
                    "VERIFIED requires device/night scope plus immutable "
                    "evidence and test results"
                )
            if self.blocking_reasons or self.failure_code:
                raise ValueError("VERIFIED cannot carry blockers or failure")
        elif self.status == RealAcceptanceStatus.PENDING:
            if not self.blocking_reasons:
                raise ValueError("PENDING requires explicit blocking reasons")
            if self.failure_code:
                raise ValueError("PENDING cannot carry failure_code")
        elif not self.failure_code:
            raise ValueError("FAILED requires a stable failure_code")
        if bool(self.capability_receipt_ids) == bool(
            self.receipt_not_applicable_reason
        ):
            raise ValueError(
                "stage must reference adapter receipts or explain why none apply"
            )
        return self


class RealPerceptorAcceptanceReport(SleepDomainContract):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["real_perceptor_acceptance_report.v1"] = (
        "real_perceptor_acceptance_report.v1"
    )
    report_id: str = Field(..., min_length=1)
    namespace_id: str = Field(..., min_length=1)
    data_mode: Literal[DataMode.LIVE] = DataMode.LIVE
    adapter_id: Literal["perceptor-v1"] = "perceptor-v1"
    adapter_version: str = Field(..., min_length=1)
    evidence_manifest_sha256: Sha256Hex
    automated_audit_sha256: Sha256Hex
    device_scope_sha256: Sha256Hex | None = None
    local_sleep_date: date | None = None
    verdicts: tuple[RealAcceptanceStageVerdict, ...]
    capability_receipts: tuple[CapabilityVerificationReceipt, ...]
    duplicate_push_checked: bool = False
    report_repull_checked: bool = False
    scope_limitation: Literal[
        "This device-bounded, one-night acceptance does not establish "
        "cross-vendor compatibility, long-term reliability, or clinical "
        "validity."
    ] = SINGLE_NIGHT_SCOPE_LIMITATION
    created_at: datetime

    @model_validator(mode="after")
    def report_is_complete_and_consistent(
        self,
    ) -> "RealPerceptorAcceptanceReport":
        expected = set(RealAcceptanceStage)
        stages = [item.stage for item in self.verdicts]
        if len(stages) != len(set(stages)) or set(stages) != expected:
            raise ValueError("report requires exactly one verdict for each stage")
        receipt_ids = {item.receipt_id for item in self.capability_receipts}
        receipts_by_id = {
            item.receipt_id: item for item in self.capability_receipts
        }
        if len(receipt_ids) != len(self.capability_receipts):
            raise ValueError("capability receipt ids must be unique")
        for receipt in self.capability_receipts:
            if (
                receipt.data_mode != DataMode.LIVE
                or receipt.adapter_id != self.adapter_id
                or receipt.adapter_version != self.adapter_version
            ):
                raise ValueError("capability receipt is outside report scope")
        for verdict in self.verdicts:
            if (
                verdict.namespace_id != self.namespace_id
                or verdict.device_scope_sha256 != self.device_scope_sha256
                or verdict.local_sleep_date != self.local_sleep_date
            ):
                raise ValueError("stage verdict is outside report scope")
            if not set(verdict.capability_receipt_ids).issubset(receipt_ids):
                raise ValueError("stage references an unknown capability receipt")
            required_capabilities = {
                RealAcceptanceStage.TRANSPORT: {AdapterCapability.PUSH},
                RealAcceptanceStage.PUSH_AUTHENTICATION: {
                    AdapterCapability.PUSH
                },
                RealAcceptanceStage.PULL_REPORT_NORMALIZATION: {
                    AdapterCapability.PULL,
                    AdapterCapability.SLEEP_REPORT,
                },
            }.get(verdict.stage)
            referenced_capabilities = {
                receipts_by_id[receipt_id].capability
                for receipt_id in verdict.capability_receipt_ids
            }
            if required_capabilities is None:
                if verdict.capability_receipt_ids:
                    raise ValueError(
                        "platform stages cannot claim Adapter capability receipts"
                    )
            elif referenced_capabilities != required_capabilities:
                raise ValueError(
                    "adapter stage does not reference its exact capabilities"
                )
            if (
                verdict.status == RealAcceptanceStatus.VERIFIED
                and any(
                    receipts_by_id[receipt_id].status
                    != CapabilityVerificationStatus.VERIFIED
                    for receipt_id in verdict.capability_receipt_ids
                )
            ):
                raise ValueError(
                    "VERIFIED adapter stage requires VERIFIED capability receipts"
                )
        all_verified = all(
            item.status == RealAcceptanceStatus.VERIFIED
            for item in self.verdicts
        )
        if all_verified and not (
            self.duplicate_push_checked and self.report_repull_checked
        ):
            raise ValueError(
                "fully VERIFIED report requires duplicate-push and report "
                "re-pull verification"
            )
        return self


def pending_real_perceptor_acceptance(
    *,
    namespace_id: str,
    adapter_artifact_sha256: str,
    configuration_fingerprint: str,
    reviewed_at: datetime,
    blocker: str,
) -> RealPerceptorAcceptanceReport:
    """Create an honest PENDING report when real vendor facts are unavailable."""

    if not blocker:
        raise ValueError("blocker is required")
    receipt_by_capability: dict[
        AdapterCapability, CapabilityVerificationReceipt
    ] = {}
    for capability in (
        AdapterCapability.PUSH,
        AdapterCapability.PULL,
        AdapterCapability.SLEEP_REPORT,
    ):
        receipt_by_capability[capability] = CapabilityVerificationReceipt(
            receipt_id=f"capability-receipt:perceptor:{capability.value}:pending",
            data_mode=DataMode.LIVE,
            adapter_id="perceptor-v1",
            adapter_version="1.0.0",
            adapter_artifact_sha256=adapter_artifact_sha256,
            configuration_fingerprint=configuration_fingerprint,
            capability=capability,
            environment="production-acceptance",
            status=CapabilityVerificationStatus.PENDING,
            evidence_references=(),
            evidence_sha256=(),
            test_result_references=(),
            test_result_sha256=(),
            conformance_result="pending_real_vendor_evidence",
            reviewer_kind=VerificationReviewerKind.AUTOMATED,
            reviewed_at=reviewed_at,
        )
    push_receipt = receipt_by_capability[AdapterCapability.PUSH].receipt_id
    pull_receipts = (
        receipt_by_capability[AdapterCapability.PULL].receipt_id,
        receipt_by_capability[AdapterCapability.SLEEP_REPORT].receipt_id,
    )
    adapter_receipts: dict[RealAcceptanceStage, tuple[str, ...]] = {
        RealAcceptanceStage.TRANSPORT: (push_receipt,),
        RealAcceptanceStage.PUSH_AUTHENTICATION: (push_receipt,),
        RealAcceptanceStage.PULL_REPORT_NORMALIZATION: pull_receipts,
    }
    verdicts = tuple(
        RealAcceptanceStageVerdict(
            stage=stage,
            status=RealAcceptanceStatus.PENDING,
            namespace_id=namespace_id,
            capability_receipt_ids=adapter_receipts.get(stage, ()),
            receipt_not_applicable_reason=(
                None
                if stage in adapter_receipts
                else "platform stage is not an AdapterCapability"
            ),
            blocking_reasons=(blocker,),
            reviewer_kind=VerificationReviewerKind.AUTOMATED,
            reviewed_at=reviewed_at,
        )
        for stage in RealAcceptanceStage
    )
    report_material = "|".join(
        (
            namespace_id,
            adapter_artifact_sha256,
            configuration_fingerprint,
            blocker,
        )
    )
    report_id = "real-perceptor-acceptance:" + hashlib.sha256(
        report_material.encode("utf-8")
    ).hexdigest()
    pending_scope_sha256 = hashlib.sha256(
        ("pending-manifest|" + report_material).encode("utf-8")
    ).hexdigest()
    pending_audit_sha256 = hashlib.sha256(
        ("pending-audit|" + report_material).encode("utf-8")
    ).hexdigest()
    return RealPerceptorAcceptanceReport(
        report_id=report_id,
        namespace_id=namespace_id,
        adapter_version="1.0.0",
        evidence_manifest_sha256=pending_scope_sha256,
        automated_audit_sha256=pending_audit_sha256,
        verdicts=verdicts,
        capability_receipts=tuple(receipt_by_capability.values()),
        created_at=reviewed_at,
    )


__all__ = [
    "RealAcceptanceStage",
    "RealAcceptanceStageVerdict",
    "RealAcceptanceStatus",
    "RealPerceptorAcceptanceReport",
    "SINGLE_NIGHT_SCOPE_LIMITATION",
    "pending_real_perceptor_acceptance",
]
