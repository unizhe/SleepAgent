"""Governed care-action proposal contracts; no delivery authority lives here."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
    )


def stable_hash(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


CARE_ACTION_POLICY_VERSION = "care-action-governance.v1"
CARE_ACTION_POLICY_HASH = stable_hash(
    {
        "version": CARE_ACTION_POLICY_VERSION,
        "urgent": "hard_block",
        "source": "current_hard_finalized_shared_analysis_only",
        "recipient": "semantic_role_only",
        "approval": "one_authorized_human",
        "external_effects": False,
    }
)


class CareActionGovernanceError(ValueError):
    """Raised when candidate, proposal, or grant authority fails closed."""


class CareActionType(str, Enum):
    RECOMMEND_CONSISTENT_WAKE_TIME = "recommend_consistent_wake_time"
    RECOMMEND_MORNING_LIGHT = "recommend_morning_light"
    REQUEST_MANUAL_FOLLOW_UP = "request_manual_follow_up"
    REQUEST_MORNING_REVIEW_FEEDBACK = "request_morning_review_feedback"


class CareActionIntent(str, Enum):
    ROUTINE_ADJUSTMENT = "routine_adjustment"
    ENVIRONMENT_ADJUSTMENT = "environment_adjustment"
    MANUAL_SUPPORT = "manual_support"
    REVIEW_FEEDBACK = "review_feedback"


class CareActionUrgency(str, Enum):
    NORMAL = "normal"
    WATCH = "watch"
    URGENT_SAFETY = "urgent_safety"


class CareAudience(str, Enum):
    ELDER = "elder"
    FAMILY = "family"
    DOCTOR = "doctor"


class CareProposalState(str, Enum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVOKED = "revoked"


class CareGrantState(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class CareDecisionChoice(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    REVOKE = "revoke"
    EXPIRE = "expire"
    SUPERSEDE = "supersede"
    CONFLICT = "conflict"


class _TaxonomyRule(FrozenContract):
    catalog_action_id: str
    catalog_version: int = 1
    action_type: CareActionType
    intent: CareActionIntent
    audience: CareAudience
    required_approver_role: CareAudience
    ttl_seconds: int = Field(ge=60, le=7 * 24 * 60 * 60)


_TAXONOMY = {
    (rule.catalog_action_id, rule.catalog_version): rule
    for rule in (
        _TaxonomyRule(
            catalog_action_id="consistent-wake-time",
            action_type=CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME,
            intent=CareActionIntent.ROUTINE_ADJUSTMENT,
            audience=CareAudience.ELDER,
            required_approver_role=CareAudience.ELDER,
            ttl_seconds=36 * 60 * 60,
        ),
        _TaxonomyRule(
            catalog_action_id="morning-light",
            action_type=CareActionType.RECOMMEND_MORNING_LIGHT,
            intent=CareActionIntent.ENVIRONMENT_ADJUSTMENT,
            audience=CareAudience.ELDER,
            required_approver_role=CareAudience.ELDER,
            ttl_seconds=36 * 60 * 60,
        ),
        _TaxonomyRule(
            catalog_action_id="nighttime-gentle-support",
            action_type=CareActionType.REQUEST_MANUAL_FOLLOW_UP,
            intent=CareActionIntent.MANUAL_SUPPORT,
            audience=CareAudience.FAMILY,
            required_approver_role=CareAudience.FAMILY,
            ttl_seconds=12 * 60 * 60,
        ),
        _TaxonomyRule(
            catalog_action_id="morning-review-feedback",
            action_type=CareActionType.REQUEST_MORNING_REVIEW_FEEDBACK,
            intent=CareActionIntent.REVIEW_FEEDBACK,
            audience=CareAudience.FAMILY,
            required_approver_role=CareAudience.FAMILY,
            ttl_seconds=48 * 60 * 60,
        ),
    )
}


def care_action_taxonomy() -> tuple[Mapping[str, Any], ...]:
    """Return the closed, semantic taxonomy without delivery-channel fields."""

    return tuple(
        _TAXONOMY[key].model_dump(mode="json") for key in sorted(_TAXONOMY)
    )


class CareActionCandidateV2(FrozenContract):
    """System-bound candidate derived from accepted CareStrategy structure."""

    schema_version: str = "care_action_candidate.v2"
    candidate_id: str = Field(min_length=1)
    candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject_id: str = Field(min_length=1)
    source_analysis_revision_id: str = Field(min_length=1)
    source_shared_analysis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_night_finalization_revision_id: str = Field(min_length=1)
    source_care_strategy_invocation_id: str = Field(min_length=1)
    source_care_strategy_version: str = Field(min_length=1)
    source_care_work_product_ref: str = Field(min_length=1)
    action_type: CareActionType
    catalog_action_id: str = Field(min_length=1)
    catalog_action_version: int = Field(ge=1)
    intent: CareActionIntent
    rationale_evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=20)
    urgency: CareActionUrgency
    audience: CareAudience
    parameters: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    policy_context_version: str = CARE_ACTION_POLICY_VERSION
    display_explanation: str | None = Field(default=None, max_length=500)

    @classmethod
    def create(cls, **values: Any) -> "CareActionCandidateV2":
        material = dict(values)
        material.pop("candidate_hash", None)
        material.pop("display_explanation", None)
        material.setdefault("schema_version", "care_action_candidate.v2")
        material.setdefault("policy_context_version", CARE_ACTION_POLICY_VERSION)
        return cls(candidate_hash=stable_hash(material), **values)

    @model_validator(mode="after")
    def candidate_is_structured_and_allowlisted(self) -> "CareActionCandidateV2":
        _aware_utc(self.created_at)
        rule = _TAXONOMY.get(
            (self.catalog_action_id, self.catalog_action_version)
        )
        if rule is None:
            raise ValueError("Care action catalog identity is unsupported")
        if (
            self.action_type is not rule.action_type
            or self.intent is not rule.intent
            or self.audience is not rule.audience
        ):
            raise ValueError("Care action semantics do not match the taxonomy")
        forbidden = {
            "email",
            "email_address",
            "phone",
            "phone_number",
            "destination",
            "recipient",
            "recipient_address",
            "channel",
            "smtp",
            "sms",
        }
        if forbidden.intersection(_nested_parameter_keys(self.parameters)):
            raise ValueError("Care candidate contains a delivery destination")
        material = self.model_dump(
            mode="python", exclude={"candidate_hash", "display_explanation"}
        )
        if self.candidate_hash != stable_hash(material):
            raise ValueError("Care candidate hash does not bind its semantics")
        return self


class CarePolicyDecision(FrozenContract):
    policy_version: str = CARE_ACTION_POLICY_VERSION
    policy_hash: str = CARE_ACTION_POLICY_HASH
    eligible: bool
    reason_code: str = Field(min_length=1, max_length=100)
    required_approver_role: CareAudience | None = None
    authorization_scope: str | None = Field(default=None, min_length=1)
    expires_at: datetime | None = None


class DeterministicCareActionPolicy:
    version = CARE_ACTION_POLICY_VERSION
    policy_hash = CARE_ACTION_POLICY_HASH

    def evaluate(
        self,
        candidate: CareActionCandidateV2,
        *,
        source_is_current: bool,
        source_is_hard_finalized: bool,
        evidence_is_sufficient: bool,
    ) -> CarePolicyDecision:
        rule = _TAXONOMY.get(
            (candidate.catalog_action_id, candidate.catalog_action_version)
        )
        if rule is None:
            return self._deny("unsupported_action")
        if candidate.urgency is CareActionUrgency.URGENT_SAFETY:
            return self._deny("urgent_zero_model_boundary")
        if not source_is_current:
            return self._deny("source_analysis_stale")
        if not source_is_hard_finalized:
            return self._deny("source_not_hard_finalized")
        if not evidence_is_sufficient:
            return self._deny("evidence_insufficient")
        return CarePolicyDecision(
            eligible=True,
            reason_code="eligible",
            required_approver_role=rule.required_approver_role,
            authorization_scope=f"care_action:{candidate.action_type.value}:approve",
            expires_at=candidate.created_at + timedelta(seconds=rule.ttl_seconds),
        )

    def _deny(self, reason: str) -> CarePolicyDecision:
        return CarePolicyDecision(eligible=False, reason_code=reason)


class CareActionProposal(FrozenContract):
    schema_version: str = "care_action_proposal.v1"
    proposal_id: str = Field(min_length=1)
    proposal_semantic_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate: CareActionCandidateV2
    policy: CarePolicyDecision
    required_approver_role: CareAudience
    authorization_scope: str = Field(min_length=1)
    created_at: datetime
    expires_at: datetime
    state: CareProposalState
    version: int = Field(ge=1)

    @classmethod
    def create(
        cls,
        candidate: CareActionCandidateV2,
        policy: CarePolicyDecision,
    ) -> "CareActionProposal":
        if (
            not policy.eligible
            or policy.required_approver_role is None
            or policy.authorization_scope is None
            or policy.expires_at is None
        ):
            raise CareActionGovernanceError(
                f"candidate is not proposal eligible: {policy.reason_code}"
            )
        semantic_key = stable_hash(
            {
                "source_analysis_revision_id": candidate.source_analysis_revision_id,
                "action_type": candidate.action_type.value,
                "subject_id": candidate.subject_id,
                "policy_version": policy.policy_version,
            }
        )
        semantic = {
            "candidate_hash": candidate.candidate_hash,
            "subject_id": candidate.subject_id,
            "action_type": candidate.action_type.value,
            "audience": candidate.audience.value,
            "authorization_scope": policy.authorization_scope,
            "source_analysis_revision_id": candidate.source_analysis_revision_id,
            "source_shared_analysis_sha256": (
                candidate.source_shared_analysis_sha256
            ),
            "source_night_finalization_revision_id": (
                candidate.source_night_finalization_revision_id
            ),
            "policy_version": policy.policy_version,
            "policy_hash": policy.policy_hash,
            "expires_at": policy.expires_at,
        }
        proposal_hash = stable_hash(semantic)
        proposed = cls(
            proposal_id=f"care-proposal:{semantic_key[:32]}",
            proposal_semantic_key=semantic_key,
            proposal_semantic_hash=proposal_hash,
            candidate=candidate,
            policy=policy,
            required_approver_role=policy.required_approver_role,
            authorization_scope=policy.authorization_scope,
            created_at=candidate.created_at,
            expires_at=policy.expires_at,
            state=CareProposalState.PROPOSED,
            version=1,
        )
        return proposed.transition(CareProposalState.AWAITING_APPROVAL)

    @model_validator(mode="after")
    def validate_proposal(self) -> "CareActionProposal":
        created = _aware_utc(self.created_at)
        expires = _aware_utc(self.expires_at)
        if expires <= created:
            raise ValueError("Care proposal expiry must follow creation")
        if not self.policy.eligible or self.policy.expires_at != self.expires_at:
            raise ValueError("Care proposal is not bound to an eligible policy")
        return self

    def transition(self, target: CareProposalState) -> "CareActionProposal":
        allowed = {
            CareProposalState.PROPOSED: {CareProposalState.AWAITING_APPROVAL},
            CareProposalState.AWAITING_APPROVAL: {
                CareProposalState.APPROVED,
                CareProposalState.REJECTED,
                CareProposalState.EXPIRED,
            },
            CareProposalState.APPROVED: {
                CareProposalState.REVOKED,
                CareProposalState.EXPIRED,
            },
        }
        if target not in allowed.get(self.state, set()):
            raise CareActionGovernanceError(
                f"invalid Care proposal transition: {self.state.value} -> {target.value}"
            )
        return self.model_copy(update={"state": target, "version": self.version + 1})


class CareApprovalGrant(FrozenContract):
    schema_version: str = "care_approval_grant.v1"
    grant_id: str = Field(min_length=1)
    grant_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_id: str = Field(min_length=1)
    proposal_semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject_id: str = Field(min_length=1)
    action_type: CareActionType
    audience: CareAudience
    authorization_scope: str = Field(min_length=1)
    approver_actor_id: str = Field(min_length=1)
    approver_role: CareAudience
    approver_binding_id: str = Field(min_length=1)
    authorization_epoch: int = Field(ge=1)
    issued_at: datetime
    expires_at: datetime
    policy_version: str = CARE_ACTION_POLICY_VERSION
    policy_hash: str = CARE_ACTION_POLICY_HASH
    idempotency_key: str = Field(min_length=1, max_length=200)
    state: CareGrantState = CareGrantState.ACTIVE
    version: int = Field(default=1, ge=1)

    @classmethod
    def issue(
        cls,
        proposal: CareActionProposal,
        *,
        approver_actor_id: str,
        approver_role: CareAudience,
        approver_binding_id: str,
        authorization_epoch: int,
        idempotency_key: str,
        issued_at: datetime,
    ) -> "CareApprovalGrant":
        if proposal.state is not CareProposalState.APPROVED:
            raise CareActionGovernanceError("grant requires an approved proposal")
        if approver_role is not proposal.required_approver_role:
            raise CareActionGovernanceError("approver role does not satisfy proposal")
        values = {
            "proposal_id": proposal.proposal_id,
            "proposal_semantic_hash": proposal.proposal_semantic_hash,
            "candidate_hash": proposal.candidate.candidate_hash,
            "subject_id": proposal.candidate.subject_id,
            "action_type": proposal.candidate.action_type,
            "audience": proposal.candidate.audience,
            "authorization_scope": proposal.authorization_scope,
            "approver_actor_id": approver_actor_id,
            "approver_role": approver_role,
            "approver_binding_id": approver_binding_id,
            "authorization_epoch": authorization_epoch,
            "issued_at": _aware_utc(issued_at),
            "expires_at": proposal.expires_at,
            "policy_version": proposal.policy.policy_version,
            "policy_hash": proposal.policy.policy_hash,
            "idempotency_key": idempotency_key,
            "state": CareGrantState.ACTIVE,
            "version": 1,
        }
        grant_hash = stable_hash(
            {
                key: value.value if isinstance(value, Enum) else value
                for key, value in values.items()
            }
        )
        return cls(
            grant_id=f"care-grant:{grant_hash[:32]}",
            grant_hash=grant_hash,
            **values,
        )

    def is_usable(
        self,
        now: datetime,
        *,
        subject_id: str | None = None,
        action_type: CareActionType | None = None,
        authorization_scope: str | None = None,
        proposal_semantic_hash: str | None = None,
    ) -> bool:
        observed = _aware_utc(now)
        return (
            self.state is CareGrantState.ACTIVE
            and self.issued_at <= observed < self.expires_at
            and (subject_id is None or subject_id == self.subject_id)
            and (action_type is None or action_type is self.action_type)
            and (
                authorization_scope is None
                or authorization_scope == self.authorization_scope
            )
            and (
                proposal_semantic_hash is None
                or proposal_semantic_hash == self.proposal_semantic_hash
            )
        )

    def revoke(self) -> "CareApprovalGrant":
        if self.state is not CareGrantState.ACTIVE:
            raise CareActionGovernanceError("only an active grant may be revoked")
        return self.model_copy(
            update={"state": CareGrantState.REVOKED, "version": self.version + 1}
        )


def taxonomy_rule(
    catalog_action_id: str,
    catalog_action_version: int,
) -> Mapping[str, Any] | None:
    rule = _TAXONOMY.get((catalog_action_id, catalog_action_version))
    return None if rule is None else rule.model_dump(mode="python")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CareActionGovernanceError("Care governance clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _nested_parameter_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            keys.add(str(key).casefold())
            keys.update(_nested_parameter_keys(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            keys.update(_nested_parameter_keys(child))
    return keys


__all__ = [
    "CARE_ACTION_POLICY_HASH",
    "CARE_ACTION_POLICY_VERSION",
    "CareActionCandidateV2",
    "CareActionGovernanceError",
    "CareActionIntent",
    "CareActionProposal",
    "CareActionType",
    "CareActionUrgency",
    "CareApprovalGrant",
    "CareAudience",
    "CareDecisionChoice",
    "CareGrantState",
    "CarePolicyDecision",
    "CareProposalState",
    "DeterministicCareActionPolicy",
    "care_action_taxonomy",
    "taxonomy_rule",
]
