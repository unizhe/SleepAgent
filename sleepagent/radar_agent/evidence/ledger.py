from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any, Literal

from pydantic import Field, model_validator

from sleepagent.radar_agent.schemas import (
    EvidenceClaim,
    EvidenceLedger,
    QuestionnaireEntry,
    RadarAgentSchema,
    ReviewStatus,
    RiskLevel,
    SupplementaryDocument,
)


class ClaimReferencePolicy(str, Enum):
    """How the ledger handles unsupported or unresolved claim references."""

    DOWNGRADE = "downgrade"
    REJECT = "reject"


class LedgerFactPurpose(str, Enum):
    """Downstream use cases that are allowed to read evidence-ledger facts."""

    REPORT = "report"
    CHAT = "chat"
    ALERT = "alert"
    DOCTOR_MATERIAL = "doctor_material"


class LedgerFactPacket(RadarAgentSchema):
    """Sanitized fact packet for report/chat/alert/doctor-material generation.

    The packet intentionally carries evidence references and ledger-reviewed facts,
    not raw vendor payloads. Downstream surfaces should consume this packet instead
    of directly reading agent outputs or raw radar events.
    """

    source: Literal["evidence_ledger"] = "evidence_ledger"
    purpose: LedgerFactPurpose
    ledger_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    raw_evidence_refs: list[str] = Field(default_factory=list)
    canonical_evidence_refs: list[str] = Field(default_factory=list)
    derived_metrics: dict[str, Any] = Field(default_factory=dict)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    uncertainty: str | None = None
    caveats: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.DRAFT

    @model_validator(mode="after")
    def claims_must_be_ledger_qualified(self) -> "LedgerFactPacket":
        draft_claims = [
            claim.claim_id
            for claim in self.claims
            if claim.review_status == ReviewStatus.DRAFT
        ]
        if draft_claims:
            raise ValueError(
                "ledger fact packets require non-draft claims: "
                + ", ".join(draft_claims)
            )
        return self


class EvidenceLedgerSnapshot(RadarAgentSchema):
    """Immutable content snapshot for versioning ledger-backed outputs."""

    snapshot_id: str = Field(..., min_length=1)
    ledger_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    version_number: int = Field(..., ge=1)
    content_hash: str = Field(..., min_length=64, max_length=64)
    ledger: EvidenceLedger
    source_claim_ids: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.DRAFT
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def snapshot_claim_ids_match_ledger(self) -> "EvidenceLedgerSnapshot":
        ledger_claim_ids = [claim.claim_id for claim in self.ledger.claims]
        if self.source_claim_ids != ledger_claim_ids:
            raise ValueError("source_claim_ids must match ledger claim order.")
        return self


class EvidenceLedgerBuilder:
    """Builds a reviewed Evidence Ledger as the single source of downstream facts."""

    def __init__(
        self,
        *,
        ledger_id: str,
        task_id: str,
        claim_reference_policy: ClaimReferencePolicy | str = (
            ClaimReferencePolicy.DOWNGRADE
        ),
    ) -> None:
        self._ledger_id = ledger_id
        self._task_id = task_id
        self._claim_reference_policy = ClaimReferencePolicy(claim_reference_policy)
        self._raw_evidence_refs: list[str] = []
        self._canonical_evidence_refs: list[str] = []
        self._derived_metrics: dict[str, Any] = {}
        self._questionnaire_entries: list[QuestionnaireEntry] = []
        self._supplementary_documents: list[SupplementaryDocument] = []
        self._claims: list[EvidenceClaim] = []
        self._caveats: list[str] = []
        self._uncertainties: list[str] = []
        self._rejected_claim_ids: list[str] = []

    def add_raw_ref(self, evidence_ref: str) -> None:
        _append_unique(self._raw_evidence_refs, evidence_ref)

    def add_canonical_ref(self, evidence_ref: str) -> None:
        _append_unique(self._canonical_evidence_refs, evidence_ref)

    def add_metric(self, name: str, value: Any) -> None:
        self._derived_metrics[name] = value

    def add_claim(self, claim: EvidenceClaim) -> None:
        self._claims.append(claim)

    def add_questionnaire_entry(self, entry: QuestionnaireEntry) -> None:
        if entry not in self._questionnaire_entries:
            self._questionnaire_entries.append(entry)
        if entry.evidence_ref:
            self.add_canonical_ref(entry.evidence_ref)

    def add_supplementary_document(self, document: SupplementaryDocument) -> None:
        if document not in self._supplementary_documents:
            self._supplementary_documents.append(document)
        self.add_canonical_ref(f"supplementary-document:{document.document_id}")

    def add_caveat(self, caveat: str) -> None:
        _append_unique(self._caveats, caveat)

    def mark_uninterpretable_scope(
        self,
        *,
        scopes: list[str],
        reason: str,
        evidence_refs: list[str],
    ) -> None:
        """Record exactly which downstream judgments are blocked by data quality."""

        current = list(self._derived_metrics.get("not_interpretable_scopes", []))
        for scope in scopes:
            _append_unique(current, scope)
        self._derived_metrics["not_interpretable_scopes"] = current
        self._derived_metrics["not_interpretable_reason"] = reason
        self._derived_metrics["not_interpretable_evidence_refs"] = list(evidence_refs)
        for evidence_ref in evidence_refs:
            self.add_canonical_ref(evidence_ref)
        _append_unique(self._uncertainties, "data_quality_not_interpretable")
        self.add_caveat(
            "Data quality is insufficient; the following scopes are not "
            f"interpretable: {', '.join(current)}."
        )

    def build(self) -> EvidenceLedger:
        claims = self._claims_with_reference_integrity()
        confidence = min((claim.confidence for claim in claims), default=0)
        uncertainty = "; ".join(self._uncertainties) or None
        return EvidenceLedger(
            ledger_id=self._ledger_id,
            task_id=self._task_id,
            raw_evidence_refs=self._raw_evidence_refs,
            canonical_evidence_refs=self._canonical_evidence_refs,
            derived_metrics=self._derived_metrics,
            questionnaire_entries=self._questionnaire_entries,
            supplementary_documents=self._supplementary_documents,
            claims=claims,
            confidence=confidence,
            uncertainty=uncertainty,
            caveats=self._caveats,
            review_status=(
                ReviewStatus.NEEDS_HUMAN_REVIEW
                if self._uncertainties or self._rejected_claim_ids
                else ReviewStatus.REVIEWED
            ),
        )

    def _claims_with_reference_integrity(self) -> list[EvidenceClaim]:
        known_refs = set(self._raw_evidence_refs) | set(self._canonical_evidence_refs)
        accepted: list[EvidenceClaim] = []
        for claim in self._claims:
            missing_refs = not claim.evidence_refs
            unresolved_refs = [
                ref for ref in claim.evidence_refs if ref not in known_refs
            ]
            if not missing_refs and not unresolved_refs:
                accepted.append(claim)
                continue

            issue = (
                "missing_evidence_refs"
                if missing_refs
                else "unresolved_evidence_refs:" + ",".join(unresolved_refs)
            )
            if self._claim_reference_policy == ClaimReferencePolicy.REJECT:
                _append_unique(self._rejected_claim_ids, claim.claim_id)
                _append_unique(self._uncertainties, issue)
                self.add_caveat(f"Rejected claim {claim.claim_id} because {issue}.")
                continue

            accepted.append(_downgrade_claim(claim, issue=issue))
            _append_unique(self._uncertainties, issue)
            self.add_caveat(f"Downgraded claim {claim.claim_id} because {issue}.")
        if self._rejected_claim_ids:
            self._derived_metrics["rejected_claim_ids"] = list(self._rejected_claim_ids)
        return accepted


def build_ledger_fact_packet(
    ledger: EvidenceLedger,
    *,
    purpose: LedgerFactPurpose | str,
) -> LedgerFactPacket:
    """Return the only sanctioned fact source for downstream generation."""

    return LedgerFactPacket(
        purpose=LedgerFactPurpose(purpose),
        ledger_id=ledger.ledger_id,
        task_id=ledger.task_id,
        raw_evidence_refs=list(ledger.raw_evidence_refs),
        canonical_evidence_refs=list(ledger.canonical_evidence_refs),
        derived_metrics=dict(ledger.derived_metrics),
        claims=list(ledger.claims),
        evidence_refs=_dedupe(
            list(ledger.raw_evidence_refs)
            + list(ledger.canonical_evidence_refs)
            + [ref for claim in ledger.claims for ref in claim.evidence_refs]
        ),
        confidence=ledger.confidence,
        uncertainty=ledger.uncertainty,
        caveats=list(ledger.caveats),
        review_status=ledger.review_status,
    )


def create_ledger_snapshot(
    ledger: EvidenceLedger,
    *,
    version_number: int,
    created_at: datetime | None = None,
) -> EvidenceLedgerSnapshot:
    """Create a deterministic hash snapshot of the full ledger content."""

    payload = ledger.model_dump_json()
    content_hash = sha256(payload.encode("utf-8")).hexdigest()
    return EvidenceLedgerSnapshot(
        snapshot_id=f"{ledger.ledger_id}:v{version_number}:{content_hash[:12]}",
        ledger_id=ledger.ledger_id,
        task_id=ledger.task_id,
        version_number=version_number,
        content_hash=content_hash,
        ledger=ledger,
        source_claim_ids=[claim.claim_id for claim in ledger.claims],
        review_status=ledger.review_status,
        created_at=created_at or datetime.now(timezone.utc),
    )


def _downgrade_claim(claim: EvidenceClaim, *, issue: str) -> EvidenceClaim:
    caveats = list(claim.caveats)
    _append_unique(caveats, f"Claim requires review: {issue}.")
    return claim.model_copy(
        update={
            "confidence": min(claim.confidence, 0.2),
            "risk_level": RiskLevel.UNCERTAIN,
            "uncertainty": issue,
            "caveats": caveats,
            "review_status": ReviewStatus.NEEDS_HUMAN_REVIEW,
        }
    )


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _dedupe(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


__all__ = [
    "ClaimReferencePolicy",
    "EvidenceLedgerBuilder",
    "EvidenceLedgerSnapshot",
    "LedgerFactPacket",
    "LedgerFactPurpose",
    "build_ledger_fact_packet",
    "create_ledger_snapshot",
]
