from __future__ import annotations

import hashlib
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from sleepagent.domain.care_outcomes import (
    CareOutcome,
    OutcomeCategory,
    PersonalizationCandidateType,
    PersonalizationEffectReceipt,
)
from sleepagent.runtime.contracts import (
    FrozenContract,
    MemoryChangeCandidate,
)


CARE_OUTCOME_MEMORY_CONCEPT_ID = (
    "care_outcome.consistent_wake_time_episode"
)
CARE_OUTCOME_GOVERNANCE_POLICY_VERSION = (
    "care-outcome-memory-governance.v1"
)
CARE_OUTCOME_MEMORY_PURPOSE = "personal_evidence_context"


class PersonalizationGovernanceStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class OutcomePersonalizationGovernance(FrozenContract):
    schema_version: Literal["outcome_personalization_governance.v1"] = (
        "outcome_personalization_governance.v1"
    )
    governance_id: str = Field(
        pattern=r"^personalization-governance:[0-9a-f]{32}$"
    )
    receipt_id: str = Field(min_length=1)
    care_outcome_id: str = Field(min_length=1)
    care_outcome_semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    care_plan_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    action_type: str = Field(min_length=1)
    outcome_category: OutcomeCategory
    outcome_policy_version: str = Field(min_length=1)
    outcome_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_revision: int = Field(ge=1)
    baseline_revision_ids: tuple[str, ...]
    followup_revision_ids: tuple[str, ...]
    candidate_semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_target_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    memory_candidate: MemoryChangeCandidate
    memory_purpose: Literal["personal_evidence_context"] = (
        "personal_evidence_context"
    )
    governance_policy_version: Literal[
        "care-outcome-memory-governance.v1"
    ] = CARE_OUTCOME_GOVERNANCE_POLICY_VERSION
    causal_claim: Literal[False] = False
    confirmation_required: Literal[True] = True
    supersedes_receipt_id: str | None = None

    @model_validator(mode="after")
    def validate_exact_candidate(self) -> "OutcomePersonalizationGovernance":
        candidate = self.memory_candidate
        if (
            candidate.subject_id != self.subject_id
            or candidate.concept_id != CARE_OUTCOME_MEMORY_CONCEPT_ID
            or candidate.memory_type != "routine"
            or candidate.operation != "create"
            or candidate.value_schema_id != "enum.v1"
            or candidate.typed_value != self.outcome_category.value
            or candidate.provenance_type != "accepted_evidence"
            or candidate.source_ref != f"evidence:{self.care_outcome_id}"
            or not candidate.confirmation_required
            or candidate.explicit_user_authorization
            or CARE_OUTCOME_MEMORY_PURPOSE not in candidate.allowed_purposes
        ):
            raise ValueError("unsupported outcome Memory candidate contract")
        if candidate.candidate_hash != self.candidate_semantic_hash:
            raise ValueError("receipt candidate hash does not bind Memory candidate")
        if self.candidate_target_hash != personalization_candidate_target_hash(
            receipt_id=self.receipt_id,
            care_outcome_id=self.care_outcome_id,
            care_outcome_semantic_hash=self.care_outcome_semantic_hash,
            candidate_semantic_hash=self.candidate_semantic_hash,
            subject_id=self.subject_id,
            care_plan_id=self.care_plan_id,
            action_type=self.action_type,
            outcome_policy_version=self.outcome_policy_version,
            outcome_policy_hash=self.outcome_policy_hash,
            evaluation_revision=self.evaluation_revision,
        ):
            raise ValueError("outcome personalization target hash mismatch")
        return self


def personalization_candidate_target_hash(
    *,
    receipt_id: str,
    care_outcome_id: str,
    care_outcome_semantic_hash: str,
    candidate_semantic_hash: str,
    subject_id: str,
    care_plan_id: str,
    action_type: str,
    outcome_policy_version: str,
    outcome_policy_hash: str,
    evaluation_revision: int,
) -> str:
    material = "\x1f".join(
        (
            receipt_id,
            care_outcome_id,
            care_outcome_semantic_hash,
            candidate_semantic_hash,
            subject_id,
            care_plan_id,
            action_type,
            outcome_policy_version,
            outcome_policy_hash,
            str(evaluation_revision),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def personalization_governance_id(
    receipt_id: str,
    candidate_semantic_hash: str,
) -> str:
    digest = hashlib.sha256(
        f"{receipt_id}\x1f{candidate_semantic_hash}".encode("utf-8")
    ).hexdigest()
    return f"personalization-governance:{digest[:32]}"


def build_outcome_personalization_governance(
    outcome: CareOutcome,
    receipt: PersonalizationEffectReceipt,
) -> OutcomePersonalizationGovernance | None:
    candidate = receipt.candidate
    if candidate is None:
        return None
    if (
        candidate.candidate_type
        is not PersonalizationCandidateType.GOVERNED_MEMORY
        or candidate.governance_path
        != "existing_longitudinal_memory_elder_confirmation"
        or candidate.source_evidence_refs != (outcome.care_outcome_id,)
        or receipt.care_outcome_id != outcome.care_outcome_id
        or receipt.care_outcome_semantic_hash != outcome.semantic_hash
        or receipt.subject_id != outcome.subject_id
        or receipt.care_plan_id != outcome.care_plan_id
        or receipt.action_type != outcome.action_type
        or outcome.causal_claim
    ):
        raise ValueError("outcome receipt lineage is not governable")
    memory_candidate = MemoryChangeCandidate.model_validate(
        candidate.semantic_content
    )
    target_hash = personalization_candidate_target_hash(
        receipt_id=receipt.receipt_id,
        care_outcome_id=outcome.care_outcome_id,
        care_outcome_semantic_hash=outcome.semantic_hash,
        candidate_semantic_hash=candidate.candidate_semantic_hash,
        subject_id=outcome.subject_id,
        care_plan_id=outcome.care_plan_id,
        action_type=outcome.action_type.value,
        outcome_policy_version=outcome.policy_version,
        outcome_policy_hash=outcome.policy_hash,
        evaluation_revision=outcome.evaluation_revision,
    )
    return OutcomePersonalizationGovernance(
        governance_id=personalization_governance_id(
            receipt.receipt_id,
            candidate.candidate_semantic_hash,
        ),
        receipt_id=receipt.receipt_id,
        care_outcome_id=outcome.care_outcome_id,
        care_outcome_semantic_hash=outcome.semantic_hash,
        care_plan_id=outcome.care_plan_id,
        subject_id=outcome.subject_id,
        action_type=outcome.action_type.value,
        outcome_category=outcome.outcome_category,
        outcome_policy_version=outcome.policy_version,
        outcome_policy_hash=outcome.policy_hash,
        evaluation_revision=outcome.evaluation_revision,
        baseline_revision_ids=outcome.baseline_revision_ids,
        followup_revision_ids=outcome.followup_revision_ids,
        candidate_semantic_hash=candidate.candidate_semantic_hash,
        candidate_target_hash=target_hash,
        memory_candidate=memory_candidate,
        supersedes_receipt_id=receipt.supersedes_receipt_id,
    )


__all__ = [
    "CARE_OUTCOME_GOVERNANCE_POLICY_VERSION",
    "CARE_OUTCOME_MEMORY_CONCEPT_ID",
    "CARE_OUTCOME_MEMORY_PURPOSE",
    "OutcomePersonalizationGovernance",
    "PersonalizationGovernanceStatus",
    "build_outcome_personalization_governance",
    "personalization_candidate_target_hash",
    "personalization_governance_id",
]
