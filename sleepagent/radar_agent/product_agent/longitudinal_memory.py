from __future__ import annotations

import json
import math
import re
import secrets
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from threading import RLock
from typing import Any, Callable, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    EpisodeStatus,
    EpisodeType,
    FrozenContract,
    InvocationOutcome,
    MemoryChangeCandidate,
    SourceScope,
    SourceScopeKind,
    StrictContract,
    ToolEffect,
    stable_hash,
)


LONGITUDINAL_MEMORY_VERSION = "sleepagent-longitudinal-memory.v1"
INDUCTION_VERSION = "sleepagent-deterministic-induction.v2"
INDUCTION_PROJECTOR_VERSION = "sleepagent-induction-projector.v2"
MEMORY_QUERY_POLICY_VERSION = "sleepagent-memory-query-policy.v1"
TOKENIZER_VERSION = "sleepagent-canonical-tokenizer.v1"
EPISODIC_HINT_TRUST_LABEL = "EPISODIC_HINT_UNTRUSTED"
USER_MEMORY_TRUST_LABEL = "USER_MEMORY_UNTRUSTED_DATA"


class MemoryPurpose(str, Enum):
    PERSONAL_EVIDENCE_CONTEXT = "personal_evidence_context"
    EXPLICIT_MEMORY_REVIEW = "explicit_memory_review"
    EXPLICIT_MEMORY_CHANGE = "explicit_memory_change"
    EXPLICIT_MEMORY_FORGET = "explicit_memory_forget"


class LongitudinalMemoryType(str, Enum):
    GOVERNED_MEMORY = "governed_memory"
    EPISODE_DIGEST = "episode_digest"


class MemorySelectorKind(str, Enum):
    CONCEPT_IDS = "concept_ids"
    ITEM_HANDLES = "item_handles"
    INVENTORY_PAGE = "inventory_page"


class MemoryItemStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"
    FORGOTTEN = "forgotten"


class DigestStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"
    FORGOTTEN = "forgotten"


class InductionJobState(str, Enum):
    PENDING = "pending"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILED = "retryable_failed"
    DEAD_LETTER = "dead_letter"


class InductionReceiptStatus(str, Enum):
    SUCCEEDED = "succeeded"
    EXCLUDED = "excluded"
    DEAD_LETTER = "dead_letter"


class SensitivityClass(str, Enum):
    PERSONAL = "personal"
    SENSITIVE_PERSONAL = "sensitive_personal"


class ProvenanceType(str, Enum):
    ELDER_CONFIRMED = "elder_confirmed"
    AUTHORIZED_OBSERVER = "authorized_observer"
    ACCEPTED_EVIDENCE = "accepted_evidence"


class DataRetentionPolicy(StrictContract):
    policy_version: str = "sleepagent-retention.v1"
    digest_ttl_days: int = Field(default=90, ge=1, le=90)
    pending_candidate_ttl_days: int = Field(default=30, ge=1, le=30)
    manifest_ttl_days: int = Field(default=7, ge=1, le=7)
    handle_ttl_minutes: int = Field(default=15, ge=1, le=60)

    def digest_expiry(self, terminal_recorded_at: datetime) -> datetime:
        return terminal_recorded_at + timedelta(days=self.digest_ttl_days)

    def candidate_expiry(self, created_at: datetime) -> datetime:
        return created_at + timedelta(days=self.pending_candidate_ttl_days)

    def manifest_expiry(self, terminal_recorded_at: datetime) -> datetime:
        return terminal_recorded_at + timedelta(days=self.manifest_ttl_days)


DEFAULT_RETENTION_POLICY = DataRetentionPolicy()


class LegacyMemoryItemV1(FrozenContract):
    """Exact loader for pre-governance Memory rows.

    A missing discriminator is injected by MemoryContextState.  Legacy items are
    never evidence-eligible and can only be surfaced in explicit elder review.
    """

    schema_version: Literal["LegacyMemoryItem.v1"] = "LegacyMemoryItem.v1"
    classification_status: Literal["legacy_unclassified"] = "legacy_unclassified"
    memory_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    value: str = Field(..., min_length=1, max_length=1000)
    source_ref: str = Field(..., min_length=1)
    version: int = Field(..., ge=1)
    active: bool = True
    confirmed: bool = True


class GovernedMemoryItemV2(FrozenContract):
    schema_version: Literal["GovernedMemoryItem.v2"] = "GovernedMemoryItem.v2"
    memory_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    memory_type: Literal[
        "preference",
        "routine",
        "environment",
        "communication_preference",
    ]
    concept_id: str = Field(..., min_length=3, max_length=160)
    value_schema_id: Literal[
        "bounded_string.v1",
        "boolean.v1",
        "number.v1",
        "enum.v1",
    ]
    value_schema_version: str = Field(default="1", min_length=1)
    typed_value: Any
    value_hash: str | None = Field(default=None, min_length=64, max_length=64)
    trust_label: Literal["USER_MEMORY_UNTRUSTED_DATA"] = USER_MEMORY_TRUST_LABEL
    provenance_type: ProvenanceType
    source_ref: str = Field(..., min_length=1)
    source_scope_kind: SourceScopeKind
    version: int = Field(..., ge=1)
    recorded_at: datetime
    valid_from: datetime
    valid_until: datetime | None = None
    sensitivity_class: SensitivityClass
    allowed_roles: tuple[AgentId, ...]
    allowed_purposes: tuple[MemoryPurpose, ...]
    status: MemoryItemStatus = MemoryItemStatus.ACTIVE
    supersedes_ref: str | None = None
    conflict_refs: tuple[str, ...] = ()
    confirmation_ref: str = Field(..., min_length=1)
    retention_policy_version: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_typed_value(self) -> "GovernedMemoryItemV2":
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,159}", self.concept_id):
            raise ValueError("concept_id must be a registered normalized identifier")
        if self.value_schema_id == "bounded_string.v1":
            if type(self.typed_value) is not str or not 1 <= len(self.typed_value) <= 500:
                raise ValueError("bounded_string.v1 requires 1..500 characters")
        elif self.value_schema_id == "boolean.v1":
            if type(self.typed_value) is not bool:
                raise ValueError("boolean.v1 requires a strict boolean")
        elif self.value_schema_id == "number.v1":
            if type(self.typed_value) not in {int, float} or not math.isfinite(
                float(self.typed_value)
            ):
                raise ValueError("number.v1 requires a finite strict number")
        elif self.value_schema_id == "enum.v1":
            if type(self.typed_value) is not str or not re.fullmatch(
                r"[a-z0-9][a-z0-9_.-]{0,79}", self.typed_value
            ):
                raise ValueError("enum.v1 requires a normalized enum code")
        if not self.allowed_roles or not self.allowed_purposes:
            raise ValueError("governed Memory requires non-empty access ceilings")
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must follow valid_from")
        material = {
            "concept_id": self.concept_id,
            "value_schema_id": self.value_schema_id,
            "value_schema_version": self.value_schema_version,
            "typed_value": self.typed_value,
        }
        expected = stable_hash(material)
        if self.value_hash is not None and self.value_hash != expected:
            raise ValueError("value_hash does not bind the governed value")
        if self.value_hash is None:
            object.__setattr__(self, "value_hash", expected)
        return self

    @property
    def active(self) -> bool:
        return self.status == MemoryItemStatus.ACTIVE

    @property
    def confirmed(self) -> bool:
        return bool(self.confirmation_ref)

    @property
    def value(self) -> str:
        if isinstance(self.typed_value, bool):
            return "true" if self.typed_value else "false"
        return str(self.typed_value)


MemoryItemRecord = LegacyMemoryItemV1 | GovernedMemoryItemV2


class MemoryQueryIntent(StrictContract):
    purpose: MemoryPurpose
    memory_types: tuple[LongitudinalMemoryType, ...]
    selector_kind: MemorySelectorKind
    requested_concept_ids: tuple[str, ...] = ()
    item_handles: tuple[str, ...] = ()
    inventory_cursor: str | None = None
    requested_time_scope: SourceScopeKind

    @model_validator(mode="after")
    def validate_selector(self) -> "MemoryQueryIntent":
        if not self.memory_types or len(set(self.memory_types)) != len(
            self.memory_types
        ):
            raise ValueError("memory_types must be a non-empty unique set")
        if self.selector_kind == MemorySelectorKind.CONCEPT_IDS:
            if not self.requested_concept_ids:
                raise ValueError("concept selector requires exact concept IDs")
            if self.item_handles or self.inventory_cursor is not None:
                raise ValueError("concept selector cannot include other selectors")
            for value in self.requested_concept_ids:
                if value in {"*", "all"} or not re.fullmatch(
                    r"[a-z0-9][a-z0-9_.-]{1,159}", value
                ):
                    raise ValueError("invalid concept selector")
        elif self.selector_kind == MemorySelectorKind.ITEM_HANDLES:
            if not self.item_handles:
                raise ValueError("item selector requires exact handles")
            if self.requested_concept_ids or self.inventory_cursor is not None:
                raise ValueError("item selector cannot include other selectors")
            if len(set(self.item_handles)) != len(self.item_handles) or any(
                value.casefold() == "all"
                or "*" in value
                or not re.fullmatch(r"mh_[A-Za-z0-9_-]{16,}", value)
                for value in self.item_handles
            ):
                raise ValueError("item selector requires exact opaque handles")
        else:
            if self.requested_concept_ids or self.item_handles:
                raise ValueError("inventory selector cannot include other selectors")
            if self.inventory_cursor is None:
                raise ValueError("inventory selector requires FIRST or a cursor")
            if (
                self.inventory_cursor != "FIRST"
                and (
                    self.inventory_cursor.casefold() == "all"
                    or "*" in self.inventory_cursor
                    or not re.fullmatch(
                        r"mc_[A-Za-z0-9_-]{16,}",
                        self.inventory_cursor,
                    )
                )
            ):
                raise ValueError("invalid inventory cursor")
        return self


class SubjectEpochs(StrictContract):
    subject_id: str = Field(..., min_length=1)
    privacy_epoch: int = Field(default=0, ge=0)
    authorization_epoch: int = Field(default=0, ge=0)
    retrieval_policy_epoch: int = Field(default=0, ge=0)


class ResolvedMemoryQuery(StrictContract):
    query_id: str = Field(..., min_length=1)
    episode_id: str = Field(..., min_length=1)
    plan_revision: int = Field(..., ge=0)
    plan_step_id: str = Field(..., min_length=1)
    invocation_id: str = Field(..., min_length=1)
    requesting_agent: AgentId
    purpose: MemoryPurpose
    memory_types: tuple[LongitudinalMemoryType, ...]
    selector_kind: MemorySelectorKind
    requested_concept_ids: tuple[str, ...] = ()
    item_handles: tuple[str, ...] = ()
    inventory_cursor: str | None = None
    source_scope: SourceScope
    as_of: datetime
    max_items: int = Field(..., ge=1, le=8)
    token_budget: int = Field(..., ge=1, le=4096)
    actor_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    actor_role: Literal["elder", "family", "doctor", "system"]
    authorization_scope: tuple[str, ...]
    fact_snapshot_id: str = Field(..., min_length=1)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    privacy_epoch: int = Field(..., ge=0)
    authorization_epoch: int = Field(..., ge=0)
    retrieval_policy_epoch: int = Field(..., ge=0)
    user_intent_ref: str | None = None
    user_intent_hash: str | None = Field(default=None, min_length=64, max_length=64)


class MemorySliceItem(StrictContract):
    retrieval_handle: str = Field(..., min_length=16)
    item_kind: LongitudinalMemoryType
    concept_ids: tuple[str, ...]
    event_codes: tuple[str, ...] = ()
    outcome_codes: tuple[str, ...] = ()
    provenance_code: str
    display_value: Any | None = None
    selection_reason_codes: tuple[str, ...]
    source_label: str = Field(..., min_length=1)
    status: str
    source_availability: Literal["available", "unavailable", "unknown"]
    allowed_current_use: Literal[
        "confirmed_memory",
        "episodic_hint_only",
        "explicit_review_only",
    ]
    valid_from: datetime
    expires_at: datetime
    conflict_group: str | None = None
    trust_label: str


class MemoryReadReceipt(StrictContract):
    schema_version: str = "MemoryReadReceipt.v1"
    receipt_id: str = Field(..., min_length=1)
    receipt_hash: str | None = Field(default=None, min_length=64, max_length=64)
    query_id: str = Field(..., min_length=1)
    query_hash: str = Field(..., min_length=64, max_length=64)
    policy_version: str = MEMORY_QUERY_POLICY_VERSION
    subject_id: str = Field(..., min_length=1)
    requesting_agent: AgentId
    purpose: MemoryPurpose
    candidate_count: int = Field(..., ge=0)
    selected_persistent_refs: tuple[str, ...]
    selected_handles: tuple[str, ...]
    filter_reason_codes: tuple[str, ...]
    selection_reason_codes: tuple[str, ...]
    actual_tokens: int = Field(..., ge=0)
    tokenizer_version: str = TOKENIZER_VERSION
    privacy_epoch: int = Field(..., ge=0)
    authorization_epoch: int = Field(..., ge=0)
    retrieval_policy_epoch: int = Field(..., ge=0)
    completed_at: datetime

    @model_validator(mode="after")
    def bind_hash(self) -> "MemoryReadReceipt":
        expected = stable_hash(
            self.model_dump(mode="json", exclude={"receipt_hash"})
        )
        if self.receipt_hash is not None and self.receipt_hash != expected:
            raise ValueError("MemoryReadReceipt hash mismatch")
        if self.receipt_hash is None:
            object.__setattr__(self, "receipt_hash", expected)
        return self


class HandleBinding(StrictContract):
    handle: str = Field(..., min_length=16)
    subject_id: str = Field(..., min_length=1)
    actor_id: str = Field(..., min_length=1)
    requesting_agent: AgentId
    purpose: MemoryPurpose
    invocation_id: str = Field(..., min_length=1)
    persistent_ref: str = Field(..., min_length=1)
    item_kind: LongitudinalMemoryType
    privacy_epoch: int = Field(..., ge=0)
    authorization_epoch: int = Field(..., ge=0)
    retrieval_policy_epoch: int = Field(..., ge=0)
    created_at: datetime
    expires_at: datetime


class CandidateReviewHandle(StrictContract):
    handle: str = Field(..., min_length=16)
    candidate_hash: str = Field(..., min_length=64, max_length=64)
    subject_id: str = Field(..., min_length=1)
    actor_id: str = Field(..., min_length=1)
    privacy_epoch: int = Field(..., ge=0)
    authorization_epoch: int = Field(..., ge=0)
    retrieval_policy_epoch: int = Field(..., ge=0)
    expires_at: datetime


class EpisodeDigest(FrozenContract):
    schema_version: str = "EpisodeDigest.v1"
    digest_id: str = Field(..., min_length=1)
    digest_hash: str | None = Field(default=None, min_length=64, max_length=64)
    subject_id: str = Field(..., min_length=1)
    episode_id: str = Field(..., min_length=1)
    episode_type: EpisodeType
    terminal_result_id: str = Field(..., min_length=64, max_length=64)
    source_receipt_revision: int = Field(..., ge=1)
    source_result_ref: str = Field(..., min_length=1)
    source_result_hash: str = Field(..., min_length=64, max_length=64)
    fact_snapshot_id: str = Field(..., min_length=1)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    source_scope: SourceScope
    terminal_recorded_at: datetime
    concept_ids: tuple[str, ...]
    event_type_codes: tuple[str, ...]
    outcome_codes: tuple[str, ...]
    observation_window_start: datetime
    observation_window_end: datetime
    accepted_evidence_refs: tuple[str, ...] = ()
    accepted_care_refs: tuple[str, ...] = ()
    feedback_event_refs: tuple[str, ...] = ()
    safety_decision_refs: tuple[str, ...] = ()
    confirmation_refs: tuple[str, ...] = ()
    source_lineage_refs: tuple[str, ...] = ()
    eligible_purposes: tuple[MemoryPurpose, ...]
    eligible_roles: tuple[AgentId, ...]
    sensitivity_class: SensitivityClass
    valid_from: datetime
    expires_at: datetime
    retention_policy_version: str
    supersedes_digest_id: str | None = None
    conflict_refs: tuple[str, ...] = ()
    trust_label: Literal["EPISODIC_HINT_UNTRUSTED"] = EPISODIC_HINT_TRUST_LABEL

    @model_validator(mode="after")
    def bind_digest_hash(self) -> "EpisodeDigest":
        if not self.concept_ids:
            raise ValueError("EpisodeDigest requires typed concepts")
        expected = stable_hash(self.model_dump(mode="json", exclude={"digest_hash"}))
        if self.digest_hash is not None and self.digest_hash != expected:
            raise ValueError("EpisodeDigest hash mismatch")
        if self.digest_hash is None:
            object.__setattr__(self, "digest_hash", expected)
        return self


class EpisodeDigestStatusEvent(FrozenContract):
    event_id: str = Field(..., min_length=1)
    event_hash: str | None = Field(default=None, min_length=64, max_length=64)
    digest_id: str = Field(..., min_length=1)
    status_sequence: int = Field(..., ge=1)
    from_status: DigestStatus | None
    to_status: DigestStatus
    reason_code: str = Field(..., min_length=1)
    privacy_epoch: int = Field(..., ge=0)
    causal_ref: str = Field(..., min_length=1)
    created_at: datetime

    @model_validator(mode="after")
    def bind_event_hash(self) -> "EpisodeDigestStatusEvent":
        expected = stable_hash(self.model_dump(mode="json", exclude={"event_hash"}))
        if self.event_hash is not None and self.event_hash != expected:
            raise ValueError("Digest status event hash mismatch")
        if self.event_hash is None:
            object.__setattr__(self, "event_hash", expected)
        return self


class ProfileCandidateProjection(StrictContract):
    candidate_id: str = Field(..., min_length=1)
    candidate_hash: str | None = Field(default=None, min_length=64, max_length=64)
    source_candidate_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    operation: Literal["create", "replace", "expire", "forget"]
    concept_id: str = Field(..., min_length=1)
    memory_type: str = Field(..., min_length=1)
    value_schema_id: str = Field(..., min_length=1)
    value_schema_version: str = Field(..., min_length=1)
    typed_value: Any
    source_ref: str = Field(..., min_length=1)
    provenance_type: str = Field(..., min_length=1)
    sensitivity_class: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def bind_projection_hash(self) -> "ProfileCandidateProjection":
        expected = stable_hash(
            self.model_dump(mode="json", exclude={"candidate_hash"})
        )
        if self.candidate_hash is not None and self.candidate_hash != expected:
            raise ValueError("Profile candidate projection hash mismatch")
        if self.candidate_hash is None:
            object.__setattr__(self, "candidate_hash", expected)
        return self


class AcceptedWorkProductView(StrictContract):
    work_product_ref: str = Field(..., min_length=1)
    agent_id: AgentId
    target_id: str = Field(..., min_length=1)
    target_hash: str = Field(..., min_length=64, max_length=64)
    concept_ids: tuple[str, ...]
    evidence_codes: tuple[str, ...] = ()
    care_codes: tuple[str, ...] = ()
    safety_codes: tuple[str, ...] = ()
    profile_candidates: tuple[ProfileCandidateProjection, ...] = ()


class ToolMetadataView(StrictContract):
    tool_invocation_id: str = Field(..., min_length=1)
    tool_name: str = Field(..., min_length=1)
    tool_version: str = Field(..., min_length=1)
    caller: str = Field(..., min_length=1)
    effect: ToolEffect
    outcome: InvocationOutcome
    observed_at: datetime
    source_classes: tuple[str, ...] = ()
    quality_codes: tuple[str, ...] = ()


class SkillInvocationView(StrictContract):
    invocation_id: str = Field(..., min_length=1)
    agent_id: AgentId
    skill_id: str = Field(..., min_length=1)
    skill_version: str = Field(..., min_length=1)
    package_hash: str = Field(..., min_length=64, max_length=64)
    validation_status: str = Field(..., min_length=1)


class InductionInputManifest(FrozenContract):
    manifest_schema_version: str = "InductionInputManifest.v2"
    projector_version: str = INDUCTION_PROJECTOR_VERSION
    manifest_id: str = Field(..., min_length=1)
    manifest_hash: str | None = Field(default=None, min_length=64, max_length=64)
    subject_id: str = Field(..., min_length=1)
    terminal_result_id: str = Field(..., min_length=64, max_length=64)
    source_result_hash: str = Field(..., min_length=64, max_length=64)
    source_receipt_revision: int = Field(..., ge=1)
    terminal_recorded_at: datetime
    episode_id: str = Field(..., min_length=1)
    episode_type: EpisodeType
    episode_status: EpisodeStatus
    fact_snapshot_id: str = Field(..., min_length=1)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    source_scope: SourceScope
    accepted_typed_views: tuple[AcceptedWorkProductView, ...]
    tool_metadata_views: tuple[ToolMetadataView, ...]
    skill_invocation_views: tuple[SkillInvocationView, ...]
    confirmation_refs: tuple[str, ...]
    declined_candidate_refs: tuple[str, ...] = ()
    feedback_event_refs: tuple[str, ...]
    privacy_epoch: int = Field(..., ge=0)
    authorization_epoch: int = Field(..., ge=0)
    retrieval_policy_epoch: int = Field(default=0, ge=0)
    retention_policy_version: str
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def bind_manifest_hash(self) -> "InductionInputManifest":
        expected = stable_hash(self.model_dump(mode="json", exclude={"manifest_hash"}))
        if self.manifest_hash is not None and self.manifest_hash != expected:
            raise ValueError("InductionInputManifest hash mismatch")
        if self.manifest_hash is None:
            object.__setattr__(self, "manifest_hash", expected)
        return self


class InductionJob(StrictContract):
    job_id: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=64, max_length=64)
    episode_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    terminal_result_id: str = Field(..., min_length=64, max_length=64)
    source_receipt_revision: int = Field(..., ge=1)
    source_result_ref: str = Field(..., min_length=1)
    source_result_hash: str = Field(..., min_length=64, max_length=64)
    terminal_recorded_at: datetime
    input_manifest_ref: str = Field(..., min_length=1)
    input_manifest_hash: str = Field(..., min_length=64, max_length=64)
    manifest_expires_at: datetime
    induction_version: str = INDUCTION_VERSION
    retention_policy_version: str
    state: InductionJobState = InductionJobState.PENDING
    attempt_count: int = Field(default=0, ge=0)
    processing_generation: int = Field(default=1, ge=1)
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    next_attempt_at: datetime
    last_error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class InductionJobEvent(FrozenContract):
    event_id: str = Field(..., min_length=1)
    job_id: str = Field(..., min_length=1)
    from_state: InductionJobState | None
    to_state: InductionJobState
    attempt_count: int = Field(..., ge=0)
    reason_code: str = Field(..., min_length=1)
    created_at: datetime


class InductionAttemptRecord(FrozenContract):
    attempt_id: str = Field(..., min_length=1)
    job_id: str = Field(..., min_length=1)
    processing_generation: int = Field(..., ge=1)
    attempt_number: int = Field(..., ge=1)
    outcome: Literal["succeeded", "retryable_failed", "dead_letter"]
    error_code: str | None = None
    started_at: datetime
    completed_at: datetime


class InductionReceipt(FrozenContract):
    receipt_id: str = Field(..., min_length=1)
    receipt_hash: str | None = Field(default=None, min_length=64, max_length=64)
    job_id: str = Field(..., min_length=1)
    processing_generation: int = Field(..., ge=1)
    episode_id: str = Field(..., min_length=1)
    terminal_result_id: str = Field(..., min_length=64, max_length=64)
    source_receipt_revision: int = Field(..., ge=1)
    source_result_hash: str = Field(..., min_length=64, max_length=64)
    input_manifest_hash: str = Field(..., min_length=64, max_length=64)
    induction_version: str
    status: InductionReceiptStatus
    decision_codes: tuple[str, ...]
    episode_digest_ref: str | None = None
    profile_candidate_refs: tuple[str, ...] = ()
    skill_outcome_refs: tuple[str, ...] = ()
    exclusion_reasons: tuple[str, ...] = ()
    attempt_count: int = Field(..., ge=0)
    parent_receipt_ref: str | None = None
    completed_at: datetime

    @model_validator(mode="after")
    def bind_receipt_hash(self) -> "InductionReceipt":
        expected = stable_hash(self.model_dump(mode="json", exclude={"receipt_hash"}))
        if self.receipt_hash is not None and self.receipt_hash != expected:
            raise ValueError("InductionReceipt hash mismatch")
        if self.receipt_hash is None:
            object.__setattr__(self, "receipt_hash", expected)
        return self


class PendingProfileCandidate(StrictContract):
    candidate_id: str = Field(..., min_length=1)
    candidate_hash: str = Field(..., min_length=64, max_length=64)
    subject_id: str = Field(..., min_length=1)
    source_result_hash: str = Field(..., min_length=64, max_length=64)
    projection: ProfileCandidateProjection
    lineage_refs: tuple[str, ...]
    repeat_count: int = Field(default=1, ge=1)
    created_at: datetime
    expires_at: datetime
    status: Literal["pending", "expired", "withdrawn", "forgotten"] = "pending"

    @model_validator(mode="after")
    def bind_projection(self) -> "PendingProfileCandidate":
        if self.candidate_hash != self.projection.candidate_hash:
            raise ValueError("pending candidate does not bind its projection")
        return self


class SkillOutcomeRecord(FrozenContract):
    outcome_id: str = Field(..., min_length=1)
    episode_id: str = Field(..., min_length=1)
    terminal_result_id: str = Field(..., min_length=64, max_length=64)
    invocation_id: str = Field(..., min_length=1)
    agent_id: AgentId
    skill_id: str = Field(..., min_length=1)
    skill_version: str = Field(..., min_length=1)
    package_hash: str = Field(..., min_length=64, max_length=64)
    status: Literal["succeeded", "failed", "superseded"]
    root_cause: Literal[
        "none",
        "model_output",
        "tool_failure",
        "policy_block",
        "runtime_failure",
    ]
    reason_codes: tuple[str, ...]
    created_at: datetime


class OfflineSkillOutcomeEnvelope(FrozenContract):
    envelope_id: str = Field(..., min_length=1)
    skill_id: str = Field(..., min_length=1)
    skill_version: str = Field(..., min_length=1)
    package_hash: str = Field(..., min_length=64, max_length=64)
    agent_id: AgentId
    status: str = Field(..., min_length=1)
    root_cause: str = Field(..., min_length=1)
    reason_codes: tuple[str, ...]
    episode_type: EpisodeType
    time_bucket: str = Field(..., min_length=1)
    eligibility_epoch: int = Field(..., ge=0)
    created_at: datetime
    withdrawn: bool = False


class DeploymentControlAttestation(StrictContract):
    attestation_id: str = Field(..., min_length=1)
    manifest_encryption_verified: bool
    backup_crypto_expiry_verified: bool
    worker_least_privilege_verified: bool
    publication_journal_verified: bool
    writer_fencing_verified: bool
    orphan_terminal_count: int = Field(..., ge=0)
    benchmark_gate_passed: bool
    attested_at: datetime

    def require_release_ready(self) -> None:
        if not all(
            (
                self.manifest_encryption_verified,
                self.backup_crypto_expiry_verified,
                self.worker_least_privilege_verified,
                self.publication_journal_verified,
                self.writer_fencing_verified,
                self.benchmark_gate_passed,
            )
        ) or self.orphan_terminal_count != 0:
            raise ValueError("deployment controls do not qualify Digest retrieval")


class PublicationJournalEntry(StrictContract):
    intent_id: str = Field(..., min_length=1)
    episode_id: str = Field(..., min_length=1)
    draft_hash: str = Field(..., min_length=64, max_length=64)
    state: Literal["reserved", "delivered", "failed"]
    delivered: bool | None = None
    created_at: datetime
    updated_at: datetime


def canonical_token_count(value: Any) -> int:
    """Pinned, deterministic count for the canonical model-facing JSON slice."""

    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    pieces = re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\s]", text)
    return len(pieces)


def detect_explicit_memory_purpose(text: str) -> MemoryPurpose | None:
    normalized = " ".join(text.strip().lower().split())
    if not normalized:
        return None
    forget = ("忘记", "删掉", "删除记忆", "forget", "remove memory")
    change = (
        "请记住",
        "帮我记住",
        "修改记忆",
        "改成",
        "更新记忆",
        "remember this",
        "remember instead",
        "change memory",
    )
    review = (
        "你记得我什么",
        "查看记忆",
        "看看记忆",
        "what do you remember",
        "show my memories",
    )
    if any(token in normalized for token in forget):
        return MemoryPurpose.EXPLICIT_MEMORY_FORGET
    if any(token in normalized for token in change):
        return MemoryPurpose.EXPLICIT_MEMORY_CHANGE
    if any(token in normalized for token in review):
        return MemoryPurpose.EXPLICIT_MEMORY_REVIEW
    return None


def canonical_result_hash(result: Any) -> str:
    return stable_hash(result.model_dump(mode="json"))


_EPISODE_CONCEPTS: dict[EpisodeType, tuple[str, ...]] = {
    EpisodeType.MORNING_REVIEW: ("sleep.last_night",),
    EpisodeType.TREND_REVIEW: ("sleep.longitudinal_trend",),
    EpisodeType.GROUNDED_DIALOGUE: ("sleep.personal_context",),
    EpisodeType.CARE_PLAN: ("sleep.care_plan",),
    EpisodeType.CARE_FOLLOWUP: ("sleep.care_followup",),
    EpisodeType.DATA_QUALITY_RECOVERY: ("sleep.data_quality",),
    EpisodeType.ROLE_MATERIAL: ("sleep.role_material",),
    EpisodeType.URGENT_BOUNDARY: ("sleep.urgent_boundary",),
}


def _source_class(source_ref: str) -> str:
    prefix = source_ref.split(":", 1)[0].lower()
    allowed = {
        "radar": "canonical_observation",
        "trend": "trend_tool",
        "user_report": "user_report",
        "authorized_observer_report": "authorized_observer_report",
        "profile": "confirmed_memory",
        "memory": "confirmed_memory",
        "evidence": "accepted_ledger",
        "evidence_reasoning": "accepted_ledger",
        "care": "care_state",
        "care_strategy": "care_state",
        "safety": "safety",
        "knowledge": "reviewed_knowledge",
    }
    return allowed.get(prefix, "opaque_source")


def project_induction_manifest(
    result: Any,
    *,
    subject_id: str,
    terminal_recorded_at: datetime,
    epochs: SubjectEpochs,
    retention_policy: DataRetentionPolicy = DEFAULT_RETENTION_POLICY,
) -> InductionInputManifest:
    """Project an allowlisted terminal input without reading prose/tool output."""

    if not result.receipt.terminal:
        raise ValueError("only terminal results can create an Induction Manifest")
    result_hash = canonical_result_hash(result)
    work_views: list[AcceptedWorkProductView] = []
    for product in result.accepted_work_products:
        payload = product.payload
        concepts = set(_EPISODE_CONCEPTS[result.receipt.episode_type])
        evidence_codes: list[str] = []
        care_codes: list[str] = []
        safety_codes: list[str] = []
        candidates: list[ProfileCandidateProjection] = []
        if product.agent_id == AgentId.EVIDENCE_REASONING:
            for claim in payload.get("claims", []):
                evidence_codes.extend(
                    (
                        f"semantic:{claim.get('semantic', 'unknown')}",
                        f"source:{claim.get('source_kind', 'unknown')}",
                    )
                )
        elif product.agent_id == AgentId.CARE_STRATEGY:
            care_codes.extend(
                (
                    f"disposition:{payload.get('disposition', 'unknown')}",
                    f"strategy:{payload.get('strategy_id', 'unknown')}",
                )
            )
        elif product.agent_id == AgentId.SAFETY_REVIEW:
            safety_codes.append(f"verdict:{payload.get('verdict', 'unknown')}")
            safety_codes.extend(
                f"reason:{code}" for code in payload.get("reason_codes", [])
            )
        elif product.agent_id == AgentId.SLEEP_CARE:
            for candidate in payload.get("memory_change_candidates", []):
                concept_id = str(candidate.get("concept_id", ""))
                if not concept_id:
                    continue
                concepts.add(concept_id)
                candidates.append(
                    ProfileCandidateProjection(
                        candidate_id=candidate["candidate_id"],
                        source_candidate_hash=candidate["candidate_hash"],
                        operation=candidate["operation"],
                        concept_id=concept_id,
                        memory_type=candidate["memory_type"],
                        value_schema_id=candidate["value_schema_id"],
                        value_schema_version=candidate["value_schema_version"],
                        typed_value=candidate["typed_value"],
                        source_ref=candidate["source_ref"],
                        provenance_type=candidate["provenance_type"],
                        sensitivity_class=candidate["sensitivity_class"],
                    )
                )
        work_views.append(
            AcceptedWorkProductView(
                work_product_ref=product.work_product_ref,
                agent_id=product.agent_id,
                target_id=product.target_id,
                target_hash=product.target_hash,
                concept_ids=tuple(sorted(concepts)),
                evidence_codes=tuple(sorted(set(evidence_codes))),
                care_codes=tuple(sorted(set(care_codes))),
                safety_codes=tuple(sorted(set(safety_codes))),
                profile_candidates=tuple(candidates),
            )
        )
    tool_views = tuple(
        ToolMetadataView(
            tool_invocation_id=receipt.tool_invocation_id,
            tool_name=receipt.tool_name,
            tool_version=receipt.tool_version,
            caller=receipt.caller,
            effect=receipt.effect,
            outcome=receipt.outcome,
            observed_at=receipt.observed_at,
            source_classes=tuple(
                sorted({_source_class(ref) for ref in receipt.source_refs})
            ),
            quality_codes=(
                (f"error:{receipt.error_code}",) if receipt.error_code else ()
            ),
        )
        for receipt in result.tool_receipts
    )
    skill_views = tuple(
        SkillInvocationView(
            invocation_id=record.invocation_id,
            agent_id=record.agent_id,
            skill_id=record.skill_id,
            skill_version=record.skill_version,
            package_hash=record.skill_package_hash,
            validation_status=record.validation_status,
        )
        for record in result.agent_invocations
    )
    confirmation_refs = {
        *result.committed_memory_candidate_ids,
        *(
            item.confirmation_id
            for item in result.pending_confirmations
            if item.target_kind in {"memory", "habit_profile"}
        ),
    }
    manifest_id = f"induction-manifest:{result_hash}"
    return InductionInputManifest(
        manifest_id=manifest_id,
        subject_id=subject_id,
        terminal_result_id=result_hash,
        source_result_hash=result_hash,
        source_receipt_revision=result.receipt.receipt_revision,
        terminal_recorded_at=terminal_recorded_at,
        episode_id=result.receipt.episode_id,
        episode_type=result.receipt.episode_type,
        episode_status=result.receipt.status,
        fact_snapshot_id=result.receipt.fact_snapshot_id,
        fact_snapshot_hash=result.receipt.fact_snapshot_hash,
        source_scope=result.receipt.source_scope,
        accepted_typed_views=tuple(work_views),
        tool_metadata_views=tool_views,
        skill_invocation_views=skill_views,
        confirmation_refs=tuple(sorted(confirmation_refs)),
        declined_candidate_refs=tuple(
            sorted(set(result.declined_confirmation_ids))
        ),
        feedback_event_refs=(),
        privacy_epoch=epochs.privacy_epoch,
        authorization_epoch=epochs.authorization_epoch,
        retrieval_policy_epoch=epochs.retrieval_policy_epoch,
        retention_policy_version=retention_policy.policy_version,
        created_at=terminal_recorded_at,
        expires_at=retention_policy.manifest_expiry(terminal_recorded_at),
    )


def build_induction_job(
    manifest: InductionInputManifest,
    *,
    now: datetime,
    retention_policy: DataRetentionPolicy = DEFAULT_RETENTION_POLICY,
) -> InductionJob:
    idempotency_key = stable_hash(
        {
            "episode_id": manifest.episode_id,
            "source_result_hash": manifest.source_result_hash,
            "induction_version": INDUCTION_VERSION,
        }
    )
    return InductionJob(
        job_id=f"induction-job:{idempotency_key}",
        idempotency_key=idempotency_key,
        episode_id=manifest.episode_id,
        subject_id=manifest.subject_id,
        terminal_result_id=manifest.terminal_result_id,
        source_receipt_revision=manifest.source_receipt_revision,
        source_result_ref=f"product-result:{manifest.terminal_result_id}",
        source_result_hash=manifest.source_result_hash,
        terminal_recorded_at=manifest.terminal_recorded_at,
        input_manifest_ref=manifest.manifest_id,
        input_manifest_hash=str(manifest.manifest_hash),
        manifest_expires_at=manifest.expires_at,
        retention_policy_version=retention_policy.policy_version,
        next_attempt_at=now,
        created_at=now,
        updated_at=now,
    )


CanonicalSourceResolver = Callable[
    [tuple[str, ...], ResolvedMemoryQuery], dict[str, Any] | None
]


class LongitudinalRepository(Protocol):
    def current_epochs(self, subject_id: str) -> SubjectEpochs: ...

    def list_active_digests(
        self, subject_id: str, *, as_of: datetime
    ) -> list[EpisodeDigest]: ...

    def digest_status(self, digest_id: str) -> DigestStatus: ...

    def save_memory_read_receipt(self, receipt: MemoryReadReceipt) -> None: ...

    def create_handle(self, binding: HandleBinding) -> None: ...

    def resolve_handle(
        self,
        handle: str,
        *,
        now: datetime | None = None,
    ) -> HandleBinding | None: ...

    def purge_handles(
        self,
        *,
        subject_id: str | None = None,
        invocation_id: str | None = None,
    ) -> int: ...

    def purge_exact_handles(self, handles: tuple[str, ...]) -> int: ...

    def digest_read_enabled(self) -> bool: ...

    def enable_digest_read(self, attestation: DeploymentControlAttestation) -> None: ...

    def kill_switch(self, *, reason_code: str, now: datetime) -> int: ...

    def list_pending_candidates(
        self, subject_id: str, *, as_of: datetime
    ) -> list[PendingProfileCandidate]: ...


class TerminalBundle(StrictContract):
    terminal_result_id: str = Field(..., min_length=64, max_length=64)
    terminal_recorded_at: datetime
    manifest: InductionInputManifest
    job: InductionJob


class InventoryCursorBinding(StrictContract):
    cursor: str = Field(..., min_length=16)
    subject_id: str = Field(..., min_length=1)
    actor_id: str = Field(..., min_length=1)
    purpose: MemoryPurpose
    last_sort_key: str = Field(..., min_length=1)
    privacy_epoch: int = Field(..., ge=0)
    authorization_epoch: int = Field(..., ge=0)
    retrieval_policy_epoch: int = Field(..., ge=0)
    expires_at: datetime


class InMemoryLongitudinalResultStore:
    """Atomic in-memory reference implementation for Result + Manifest + Job."""

    def __init__(
        self,
        *,
        retention_policy: DataRetentionPolicy = DEFAULT_RETENTION_POLICY,
    ) -> None:
        self.retention_policy = retention_policy
        self.lock = RLock()
        self._results: dict[str, list[Any]] = {}
        self._result_identity: dict[tuple[str, int], str] = {}
        self._bundles: dict[str, TerminalBundle] = {}
        self._manifests: dict[str, InductionInputManifest] = {}
        self._manifest_purged: set[str] = set()
        self._jobs: dict[str, InductionJob] = {}
        self._job_events: list[InductionJobEvent] = []
        self._attempts: list[InductionAttemptRecord] = []
        self._receipts: dict[str, InductionReceipt] = {}
        self._semantic_receipt_by_job: dict[str, str] = {}
        self._digests: dict[str, EpisodeDigest] = {}
        self._digest_events: dict[str, list[EpisodeDigestStatusEvent]] = {}
        self._pending_candidates: dict[
            tuple[str, str],
            PendingProfileCandidate,
        ] = {}
        self._skill_outcomes: dict[str, SkillOutcomeRecord] = {}
        self._offline_outcomes: dict[str, OfflineSkillOutcomeEnvelope] = {}
        self._offline_subjects: dict[str, str] = {}
        self._offline_lineage: dict[str, str] = {}
        self._memory_read_receipts: dict[str, MemoryReadReceipt] = {}
        self._handles: dict[str, HandleBinding] = {}
        self._cursors: dict[str, InventoryCursorBinding] = {}
        self._candidate_review_handles: dict[str, CandidateReviewHandle] = {}
        self._epochs: dict[str, SubjectEpochs] = {}
        self._retrieval_policy_epoch = 0
        self._digest_enabled = False
        self._deployment_attestations: list[DeploymentControlAttestation] = []
        self._publication_journal: dict[str, PublicationJournalEntry] = {}

    def _copy(self, value: Any) -> Any:
        return value.model_copy(deep=True) if hasattr(value, "model_copy") else value

    def current_epochs(self, subject_id: str) -> SubjectEpochs:
        current = self._epochs.get(
            subject_id,
            SubjectEpochs(
                subject_id=subject_id,
                retrieval_policy_epoch=self._retrieval_policy_epoch,
            ),
        )
        if current.retrieval_policy_epoch != self._retrieval_policy_epoch:
            current = current.model_copy(
                update={"retrieval_policy_epoch": self._retrieval_policy_epoch}
            )
            self._epochs[subject_id] = current
        return current.model_copy(deep=True)

    def append_nonterminal(self, result: Any, *, subject_id: str) -> None:
        if result.receipt.terminal:
            raise ValueError("terminal results require append_terminal_bundle")
        del subject_id
        with self.lock:
            self._append_result(result)

    def append_terminal_bundle(
        self,
        result: Any,
        *,
        subject_id: str,
        now: datetime | None = None,
    ) -> TerminalBundle:
        if not result.receipt.terminal:
            raise ValueError("non-terminal results use append_nonterminal")
        recorded_at = now or datetime.now(timezone.utc)
        result_id = canonical_result_hash(result)
        key = (result.receipt.episode_id, result.receipt.receipt_revision)
        with self.lock:
            existing_id = self._result_identity.get(key)
            if existing_id is not None:
                if existing_id != result_id:
                    raise ValueError(
                        "episode receipt revision already binds different content"
                    )
                return self._bundles[result_id].model_copy(deep=True)
            manifest = project_induction_manifest(
                result,
                subject_id=subject_id,
                terminal_recorded_at=recorded_at,
                epochs=self.current_epochs(subject_id),
                retention_policy=self.retention_policy,
            )
            job = build_induction_job(
                manifest,
                now=recorded_at,
                retention_policy=self.retention_policy,
            )
            bundle = TerminalBundle(
                terminal_result_id=result_id,
                terminal_recorded_at=recorded_at,
                manifest=manifest,
                job=job,
            )
            self._append_result(result)
            self._bundles[result_id] = bundle
            self._manifests[manifest.manifest_id] = manifest
            self._jobs[job.job_id] = job
            self._job_events.append(
                InductionJobEvent(
                    event_id=f"job-event:{stable_hash((job.job_id, 'created'))}",
                    job_id=job.job_id,
                    from_state=None,
                    to_state=InductionJobState.PENDING,
                    attempt_count=0,
                    reason_code="terminal_bundle_committed",
                    created_at=recorded_at,
                )
            )
            return bundle.model_copy(deep=True)

    def _append_result(self, result: Any) -> None:
        result_id = canonical_result_hash(result)
        key = (result.receipt.episode_id, result.receipt.receipt_revision)
        existing_id = self._result_identity.get(key)
        if existing_id is not None:
            if existing_id != result_id:
                raise ValueError(
                    "episode receipt revision already binds different content"
                )
            return
        history = self._results.setdefault(result.receipt.episode_id, [])
        if history and result.receipt.receipt_revision <= max(
            item.receipt.receipt_revision for item in history
        ):
            raise ValueError("Episode receipt revisions must increase")
        history.append(result.model_copy(deep=True))
        self._result_identity[key] = result_id

    def append(self, result: Any) -> None:
        if result.receipt.terminal:
            raise ValueError(
                "legacy append is fenced for terminal results; "
                "use append_terminal_bundle"
            )
        raise ValueError("append_nonterminal requires trusted subject binding")

    def latest(self, episode_id: str) -> Any:
        try:
            return self._results[episode_id][-1].model_copy(deep=True)
        except (KeyError, IndexError) as exc:
            raise KeyError(f"unknown Episode result: {episode_id}") from exc

    def history(self, episode_id: str) -> list[Any]:
        return [
            item.model_copy(deep=True)
            for item in self._results.get(episode_id, [])
        ]

    def expire_pending_candidates(self, *, now: datetime) -> int:
        expired = 0
        with self.lock:
            for key, candidate in list(self._pending_candidates.items()):
                if candidate.status == "pending" and candidate.expires_at <= now:
                    self._pending_candidates[key] = candidate.model_copy(
                        update={"status": "expired"}
                    )
                    expired += 1
        return expired

    def apply_authorization_change(
        self,
        *,
        subject_id: str,
        more_restrictive: bool,
        causal_ref: str,
        now: datetime,
    ) -> SubjectEpochs:
        with self.lock:
            current = self.current_epochs(subject_id)
            updated = current.model_copy(
                update={
                    "authorization_epoch": current.authorization_epoch + 1,
                    "privacy_epoch": (
                        current.privacy_epoch + 1
                        if more_restrictive
                        else current.privacy_epoch
                    ),
                }
            )
            self._epochs[subject_id] = updated
            self._candidate_review_handles = {
                handle: binding
                for handle, binding in self._candidate_review_handles.items()
                if binding.subject_id != subject_id
            }
            self.purge_handles(subject_id=subject_id)
            if more_restrictive:
                for digest in list(self._digests.values()):
                    if (
                        digest.subject_id == subject_id
                        and self.digest_status(digest.digest_id)
                        == DigestStatus.ACTIVE
                    ):
                        self.transition_digest_status(
                            digest.digest_id,
                            to_status=DigestStatus.WITHDRAWN,
                            reason_code="authorization_restricted",
                            causal_ref=causal_ref,
                            now=now,
                        )
            return updated.model_copy(deep=True)

    def terminal_bundle(self, terminal_result_id: str) -> TerminalBundle:
        bundle = self._bundles[terminal_result_id]
        if bundle.manifest.manifest_id in self._manifest_purged:
            raise ValueError("source_manifest_expired")
        return bundle.model_copy(deep=True)

    def reserve_publication(
        self,
        *,
        episode_id: str,
        draft_hash: str,
        now: datetime | None = None,
    ) -> tuple[PublicationJournalEntry, bool]:
        created_at = now or datetime.now(timezone.utc)
        intent_id = f"publication:{stable_hash((episode_id, draft_hash))}"
        with self.lock:
            existing = self._publication_journal.get(intent_id)
            if existing is not None:
                return existing.model_copy(deep=True), False
            entry = PublicationJournalEntry(
                intent_id=intent_id,
                episode_id=episode_id,
                draft_hash=draft_hash,
                state="reserved",
                created_at=created_at,
                updated_at=created_at,
            )
            self._publication_journal[intent_id] = entry
            return entry.model_copy(deep=True), True

    def complete_publication(
        self,
        *,
        intent_id: str,
        delivered: bool,
        now: datetime | None = None,
    ) -> PublicationJournalEntry:
        completed_at = now or datetime.now(timezone.utc)
        with self.lock:
            current = self._publication_journal.get(intent_id)
            if current is None:
                raise KeyError("publication intent was not reserved")
            if current.state != "reserved":
                if current.delivered != delivered:
                    raise ValueError("publication outcome conflicts with journal")
                return current.model_copy(deep=True)
            updated = current.model_copy(
                update={
                    "state": "delivered" if delivered else "failed",
                    "delivered": delivered,
                    "updated_at": completed_at,
                }
            )
            self._publication_journal[intent_id] = updated
            return updated.model_copy(deep=True)

    def publication_entry(self, intent_id: str) -> PublicationJournalEntry:
        return self._publication_journal[intent_id].model_copy(deep=True)

    def lease_next_job(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int = 30,
    ) -> InductionJob | None:
        with self.lock:
            candidates = sorted(
                (
                    job
                    for job in self._jobs.values()
                    if (
                        job.state
                        in {
                            InductionJobState.PENDING,
                            InductionJobState.RETRYABLE_FAILED,
                        }
                        and job.next_attempt_at <= now
                    )
                    or (
                        job.state == InductionJobState.LEASED
                        and job.lease_expires_at is not None
                        and job.lease_expires_at <= now
                    )
                ),
                key=lambda item: (item.created_at, item.job_id),
            )
            if not candidates:
                return None
            current = candidates[0]
            updated = current.model_copy(
                update={
                    "state": InductionJobState.LEASED,
                    "attempt_count": current.attempt_count + 1,
                    "lease_owner": worker_id,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                }
            )
            self._jobs[current.job_id] = updated
            self._job_events.append(
                InductionJobEvent(
                    event_id=f"job-event:{stable_hash((current.job_id, updated.attempt_count, 'lease'))}",
                    job_id=current.job_id,
                    from_state=current.state,
                    to_state=InductionJobState.LEASED,
                    attempt_count=updated.attempt_count,
                    reason_code=(
                        "lease_recovered"
                        if current.state == InductionJobState.LEASED
                        else "lease_acquired"
                    ),
                    created_at=now,
                )
            )
            return updated.model_copy(deep=True)

    def get_manifest(self, manifest_id: str) -> InductionInputManifest | None:
        manifest = self._manifests.get(manifest_id)
        return None if manifest is None else manifest.model_copy(deep=True)

    def latest_terminal_revision(self, episode_id: str) -> int:
        terminal = [
            item.receipt.receipt_revision
            for item in self._results.get(episode_id, [])
            if item.receipt.terminal
        ]
        return max(terminal) if terminal else 0

    def semantic_receipt(self, job_id: str) -> InductionReceipt | None:
        receipt_id = self._semantic_receipt_by_job.get(job_id)
        if receipt_id is None:
            return None
        return self._receipts[receipt_id].model_copy(deep=True)

    def complete_induction(
        self,
        *,
        job: InductionJob,
        receipt: InductionReceipt,
        digest: EpisodeDigest | None,
        candidates: tuple[PendingProfileCandidate, ...],
        skill_outcomes: tuple[SkillOutcomeRecord, ...],
        offline_outcomes: tuple[OfflineSkillOutcomeEnvelope, ...],
        now: datetime,
    ) -> None:
        with self.lock:
            current = self._jobs.get(job.job_id)
            if (
                current is None
                or current.state != InductionJobState.LEASED
                or current.lease_owner != job.lease_owner
                or current.attempt_count != job.attempt_count
            ):
                raise ValueError("Induction lease changed before commit")
            if job.job_id in self._semantic_receipt_by_job:
                return
            superseded_digest_id: str | None = None
            for existing in list(self._digests.values()):
                if (
                    existing.episode_id == job.episode_id
                    and existing.source_receipt_revision
                    < job.source_receipt_revision
                    and self.digest_status(existing.digest_id)
                    == DigestStatus.ACTIVE
                ):
                    self.transition_digest_status(
                        existing.digest_id,
                        to_status=DigestStatus.SUPERSEDED,
                        reason_code="higher_terminal_revision",
                        causal_ref=receipt.receipt_id,
                        now=now,
                    )
                    superseded_digest_id = existing.digest_id
            if digest is not None:
                if superseded_digest_id is not None:
                    digest = EpisodeDigest.model_validate(
                        {
                            **digest.model_dump(
                                mode="json",
                                exclude={"digest_hash"},
                            ),
                            "supersedes_digest_id": superseded_digest_id,
                        }
                    )
                self._digests[digest.digest_id] = digest
                self._digest_events[digest.digest_id] = [
                    EpisodeDigestStatusEvent(
                        event_id=f"digest-event:{stable_hash((digest.digest_id, 1, 'active'))}",
                        digest_id=digest.digest_id,
                        status_sequence=1,
                        from_status=None,
                        to_status=DigestStatus.ACTIVE,
                        reason_code="induction_succeeded",
                        privacy_epoch=self.current_epochs(digest.subject_id).privacy_epoch,
                        causal_ref=receipt.receipt_id,
                        created_at=now,
                    )
                ]
            older_result_hashes = {
                existing.source_result_hash
                for existing in self._jobs.values()
                if existing.episode_id == job.episode_id
                and existing.source_receipt_revision
                < job.source_receipt_revision
            }
            for key, candidate in list(self._pending_candidates.items()):
                if (
                    candidate.source_result_hash in older_result_hashes
                    and candidate.status == "pending"
                ):
                    self._pending_candidates[key] = candidate.model_copy(
                        update={"status": "withdrawn"}
                    )
            for outcome_id, outcome in list(self._skill_outcomes.items()):
                if (
                    outcome.episode_id == job.episode_id
                    and outcome.terminal_result_id != job.terminal_result_id
                    and outcome.status != "superseded"
                ):
                    self._skill_outcomes[outcome_id] = outcome.model_copy(
                        update={
                            "status": "superseded",
                            "reason_codes": (
                                *outcome.reason_codes,
                                "higher_terminal_revision",
                            ),
                        }
                    )
            for envelope_id, envelope in list(self._offline_outcomes.items()):
                if (
                    self._offline_lineage.get(envelope_id)
                    in older_result_hashes
                    and not envelope.withdrawn
                ):
                    self._offline_outcomes[envelope_id] = envelope.model_copy(
                        update={"withdrawn": True}
                    )
            for candidate in candidates:
                candidate_key = (
                    candidate.subject_id,
                    candidate.candidate_hash,
                )
                existing = self._pending_candidates.get(candidate_key)
                if existing is None:
                    self._pending_candidates[candidate_key] = candidate
                else:
                    self._pending_candidates[candidate_key] = (
                        existing.model_copy(
                            update={"repeat_count": existing.repeat_count + 1}
                        )
                    )
            for outcome in skill_outcomes:
                self._skill_outcomes[outcome.outcome_id] = outcome
            for envelope in offline_outcomes:
                self._offline_outcomes[envelope.envelope_id] = envelope
                self._offline_subjects[envelope.envelope_id] = (
                    self._manifests[job.input_manifest_ref].subject_id
                )
                self._offline_lineage[envelope.envelope_id] = (
                    job.terminal_result_id
                )
            self._receipts[receipt.receipt_id] = receipt
            self._semantic_receipt_by_job[job.job_id] = receipt.receipt_id
            completed = current.model_copy(
                update={
                    "state": InductionJobState.SUCCEEDED,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "updated_at": now,
                }
            )
            self._jobs[job.job_id] = completed
            self._job_events.append(
                InductionJobEvent(
                    event_id=f"job-event:{stable_hash((job.job_id, current.attempt_count, 'succeeded'))}",
                    job_id=job.job_id,
                    from_state=InductionJobState.LEASED,
                    to_state=InductionJobState.SUCCEEDED,
                    attempt_count=current.attempt_count,
                    reason_code=receipt.status.value,
                    created_at=now,
                )
            )
            self._attempts.append(
                InductionAttemptRecord(
                    attempt_id=f"attempt:{job.job_id}:{job.processing_generation}:{current.attempt_count}",
                    job_id=job.job_id,
                    processing_generation=job.processing_generation,
                    attempt_number=current.attempt_count,
                    outcome="succeeded",
                    started_at=now,
                    completed_at=now,
                )
            )

    def fail_induction(
        self,
        *,
        job: InductionJob,
        error_code: str,
        now: datetime,
        max_attempts: int,
    ) -> InductionReceipt | None:
        with self.lock:
            current = self._jobs.get(job.job_id)
            if current is None or current.state != InductionJobState.LEASED:
                raise ValueError("Induction lease changed before failure")
            dead = current.attempt_count >= max_attempts or now >= current.manifest_expires_at
            next_state = (
                InductionJobState.DEAD_LETTER
                if dead
                else InductionJobState.RETRYABLE_FAILED
            )
            updated = current.model_copy(
                update={
                    "state": next_state,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "last_error_code": error_code,
                    "next_attempt_at": now
                    + timedelta(seconds=min(60, 2 ** current.attempt_count)),
                    "updated_at": now,
                }
            )
            self._jobs[job.job_id] = updated
            self._job_events.append(
                InductionJobEvent(
                    event_id=f"job-event:{stable_hash((job.job_id, current.attempt_count, next_state.value))}",
                    job_id=job.job_id,
                    from_state=InductionJobState.LEASED,
                    to_state=next_state,
                    attempt_count=current.attempt_count,
                    reason_code=error_code,
                    created_at=now,
                )
            )
            self._attempts.append(
                InductionAttemptRecord(
                    attempt_id=f"attempt:{job.job_id}:{job.processing_generation}:{current.attempt_count}",
                    job_id=job.job_id,
                    processing_generation=job.processing_generation,
                    attempt_number=current.attempt_count,
                    outcome="dead_letter" if dead else "retryable_failed",
                    error_code=error_code,
                    started_at=now,
                    completed_at=now,
                )
            )
            if not dead:
                return None
            receipt = InductionReceipt(
                receipt_id=f"induction-receipt:{stable_hash((job.job_id, job.processing_generation, 'dead'))}",
                job_id=job.job_id,
                processing_generation=job.processing_generation,
                episode_id=job.episode_id,
                terminal_result_id=job.terminal_result_id,
                source_receipt_revision=job.source_receipt_revision,
                source_result_hash=job.source_result_hash,
                input_manifest_hash=job.input_manifest_hash,
                induction_version=job.induction_version,
                status=InductionReceiptStatus.DEAD_LETTER,
                decision_codes=("dead_letter",),
                exclusion_reasons=(error_code,),
                attempt_count=current.attempt_count,
                completed_at=now,
            )
            self._receipts[receipt.receipt_id] = receipt
            return receipt.model_copy(deep=True)

    def replay_dead_letter(
        self,
        *,
        job_id: str,
        parent_receipt_ref: str,
        now: datetime,
    ) -> InductionJob:
        with self.lock:
            current = self._jobs[job_id]
            if current.state != InductionJobState.DEAD_LETTER:
                raise ValueError("only dead-letter Jobs can be replayed")
            if current.input_manifest_ref in self._manifest_purged or (
                now >= current.manifest_expires_at
            ):
                raise ValueError("source_manifest_expired")
            parent = self._receipts.get(parent_receipt_ref)
            if parent is None or parent.job_id != job_id:
                raise ValueError("replay requires the exact parent Receipt")
            updated = current.model_copy(
                update={
                    "state": InductionJobState.PENDING,
                    "processing_generation": current.processing_generation + 1,
                    "attempt_count": 0,
                    "last_error_code": None,
                    "next_attempt_at": now,
                    "updated_at": now,
                }
            )
            self._jobs[job_id] = updated
            self._job_events.append(
                InductionJobEvent(
                    event_id=(
                        "job-event:"
                        + stable_hash(
                            (
                                job_id,
                                updated.processing_generation,
                                "manual_replay",
                            )
                        )
                    ),
                    job_id=job_id,
                    from_state=InductionJobState.DEAD_LETTER,
                    to_state=InductionJobState.PENDING,
                    attempt_count=0,
                    reason_code=f"manual_replay:{parent_receipt_ref}",
                    created_at=now,
                )
            )
            return updated.model_copy(deep=True)

    def purge_expired_manifests(self, *, now: datetime) -> int:
        with self.lock:
            purge = [
                manifest_id
                for manifest_id, manifest in self._manifests.items()
                if manifest.expires_at <= now
            ]
            for manifest_id in purge:
                del self._manifests[manifest_id]
                self._manifest_purged.add(manifest_id)
                for result_id, bundle in list(self._bundles.items()):
                    if bundle.manifest.manifest_id == manifest_id:
                        del self._bundles[result_id]
            return len(purge)

    def digest_status(self, digest_id: str) -> DigestStatus:
        events = self._digest_events.get(digest_id)
        if not events:
            raise KeyError(f"unknown Digest: {digest_id}")
        prior: DigestStatus | None = None
        terminal = {
            DigestStatus.EXPIRED,
            DigestStatus.SUPERSEDED,
            DigestStatus.WITHDRAWN,
            DigestStatus.FORGOTTEN,
        }
        for sequence, event in enumerate(events, start=1):
            if (
                event.digest_id != digest_id
                or event.status_sequence != sequence
                or event.from_status != prior
                or (prior in terminal and event.to_status == DigestStatus.ACTIVE)
            ):
                raise ValueError("Digest event projection mismatch")
            prior = event.to_status
        return events[-1].to_status

    def transition_digest_status(
        self,
        digest_id: str,
        *,
        to_status: DigestStatus,
        reason_code: str,
        causal_ref: str,
        now: datetime,
    ) -> EpisodeDigestStatusEvent:
        with self.lock:
            digest = self._digests[digest_id]
            events = self._digest_events[digest_id]
            current = events[-1].to_status
            if current == to_status:
                return events[-1].model_copy(deep=True)
            if current in {DigestStatus.FORGOTTEN, DigestStatus.WITHDRAWN}:
                raise ValueError("terminal privacy status cannot be changed")
            if current in {DigestStatus.EXPIRED, DigestStatus.SUPERSEDED} and (
                to_status == DigestStatus.ACTIVE
            ):
                raise ValueError("Digest cannot be reactivated")
            sequence = events[-1].status_sequence + 1
            event = EpisodeDigestStatusEvent(
                event_id=f"digest-event:{stable_hash((digest_id, sequence, to_status.value))}",
                digest_id=digest_id,
                status_sequence=sequence,
                from_status=current,
                to_status=to_status,
                reason_code=reason_code,
                privacy_epoch=self.current_epochs(digest.subject_id).privacy_epoch,
                causal_ref=causal_ref,
                created_at=now,
            )
            events.append(event)
            return event.model_copy(deep=True)

    def list_active_digests(
        self, subject_id: str, *, as_of: datetime
    ) -> list[EpisodeDigest]:
        return [
            digest.model_copy(deep=True)
            for digest in sorted(
                self._digests.values(),
                key=lambda item: (
                    item.terminal_recorded_at,
                    item.digest_id,
                ),
            )
            if digest.subject_id == subject_id
            and digest.valid_from <= as_of < digest.expires_at
            and self.digest_status(digest.digest_id) == DigestStatus.ACTIVE
        ]

    def expire_digests(self, *, now: datetime) -> int:
        count = 0
        for digest in list(self._digests.values()):
            if (
                digest.expires_at <= now
                and self.digest_status(digest.digest_id) == DigestStatus.ACTIVE
            ):
                self.transition_digest_status(
                    digest.digest_id,
                    to_status=DigestStatus.EXPIRED,
                    reason_code="retention_expired",
                    causal_ref=f"retention:{now.isoformat()}",
                    now=now,
                )
                count += 1
        return count

    def save_memory_read_receipt(self, receipt: MemoryReadReceipt) -> None:
        self._memory_read_receipts[receipt.receipt_id] = receipt.model_copy(deep=True)

    def memory_read_receipt(self, receipt_id: str) -> MemoryReadReceipt:
        return self._memory_read_receipts[receipt_id].model_copy(deep=True)

    def create_handle(self, binding: HandleBinding) -> None:
        self._handles[binding.handle] = binding.model_copy(deep=True)

    def resolve_handle(
        self,
        handle: str,
        *,
        now: datetime | None = None,
    ) -> HandleBinding | None:
        binding = self._handles.get(handle)
        if binding is None:
            return None
        if binding.expires_at <= (now or datetime.now(timezone.utc)):
            del self._handles[handle]
            return None
        return binding.model_copy(deep=True)

    def purge_handles(
        self,
        *,
        subject_id: str | None = None,
        invocation_id: str | None = None,
    ) -> int:
        selected = [
            handle
            for handle, binding in self._handles.items()
            if (subject_id is None or binding.subject_id == subject_id)
            and (invocation_id is None or binding.invocation_id == invocation_id)
        ]
        for handle in selected:
            del self._handles[handle]
        return len(selected)

    def purge_exact_handles(self, handles: tuple[str, ...]) -> int:
        removed = 0
        for handle in handles:
            if self._handles.pop(handle, None) is not None:
                removed += 1
        return removed

    def issue_cursor(self, binding: InventoryCursorBinding) -> None:
        self._cursors[binding.cursor] = binding.model_copy(deep=True)

    def resolve_cursor(
        self,
        cursor: str,
        *,
        now: datetime | None = None,
    ) -> InventoryCursorBinding | None:
        binding = self._cursors.get(cursor)
        if binding is None or binding.expires_at <= (
            now or datetime.now(timezone.utc)
        ):
            self._cursors.pop(cursor, None)
            return None
        return binding.model_copy(deep=True)

    def digest_read_enabled(self) -> bool:
        return self._digest_enabled

    def orphan_terminal_count(self) -> int:
        terminal_ids = {
            result_id
            for result_id, bundle in self._bundles.items()
            if bundle.job.job_id in self._jobs
            and bundle.manifest.manifest_id in self._manifests
        }
        expected = {
            result_id
            for result_id in self._result_identity.values()
            if result_id in self._bundles
        }
        return len(expected - terminal_ids)

    def enable_digest_read(self, attestation: DeploymentControlAttestation) -> None:
        attestation.require_release_ready()
        if self.orphan_terminal_count() != 0:
            raise ValueError("terminal orphan scan is not zero")
        self._deployment_attestations.append(
            attestation.model_copy(deep=True)
        )
        self._digest_enabled = True

    def kill_switch(self, *, reason_code: str, now: datetime) -> int:
        del reason_code, now
        with self.lock:
            self._retrieval_policy_epoch += 1
            self._digest_enabled = False
            self._handles.clear()
            self._cursors.clear()
            self._candidate_review_handles.clear()
            for subject_id, epochs in list(self._epochs.items()):
                self._epochs[subject_id] = epochs.model_copy(
                    update={
                        "retrieval_policy_epoch": self._retrieval_policy_epoch
                    }
                )
            return self._retrieval_policy_epoch

    def apply_privacy_action(
        self,
        *,
        subject_id: str,
        action: Literal["forget", "withdraw", "delete", "correction"],
        causal_ref: str,
        now: datetime,
    ) -> SubjectEpochs:
        with self.lock:
            epochs = self.current_epochs(subject_id)
            epochs = epochs.model_copy(
                update={"privacy_epoch": epochs.privacy_epoch + 1}
            )
            self._epochs[subject_id] = epochs
            target_status = {
                "forget": DigestStatus.FORGOTTEN,
                "withdraw": DigestStatus.WITHDRAWN,
                "delete": DigestStatus.FORGOTTEN,
                "correction": DigestStatus.SUPERSEDED,
            }[action]
            for digest in list(self._digests.values()):
                if (
                    digest.subject_id == subject_id
                    and self.digest_status(digest.digest_id)
                    == DigestStatus.ACTIVE
                ):
                    self.transition_digest_status(
                        digest.digest_id,
                        to_status=target_status,
                        reason_code=action,
                        causal_ref=causal_ref,
                        now=now,
                    )
            for key, candidate in list(self._pending_candidates.items()):
                if candidate.subject_id == subject_id and candidate.status == "pending":
                    self._pending_candidates[key] = candidate.model_copy(
                        update={
                            "status": (
                                "forgotten"
                                if action in {"forget", "delete"}
                                else "withdrawn"
                            )
                        }
                    )
            for key, envelope in list(self._offline_outcomes.items()):
                if (
                    self._offline_subjects.get(key) == subject_id
                    and not envelope.withdrawn
                ):
                    self._offline_outcomes[key] = envelope.model_copy(
                        update={"withdrawn": True}
                    )
            self._candidate_review_handles = {
                handle: binding
                for handle, binding in self._candidate_review_handles.items()
                if binding.subject_id != subject_id
            }
            self.purge_handles(subject_id=subject_id)
            return epochs.model_copy(deep=True)

    def list_pending_candidates(
        self, subject_id: str, *, as_of: datetime
    ) -> list[PendingProfileCandidate]:
        return [
            item.model_copy(deep=True)
            for item in sorted(
                self._pending_candidates.values(),
                key=lambda value: (value.created_at, value.candidate_id),
            )
            if item.subject_id == subject_id
            and item.status == "pending"
            and item.expires_at > as_of
        ]

    def create_candidate_review_handle(
        self,
        binding: CandidateReviewHandle,
    ) -> None:
        self._candidate_review_handles[binding.handle] = binding.model_copy(
            deep=True
        )

    def resolve_candidate_review_handle(
        self,
        handle: str,
        *,
        now: datetime,
    ) -> CandidateReviewHandle | None:
        binding = self._candidate_review_handles.get(handle)
        if binding is None or binding.expires_at <= now:
            self._candidate_review_handles.pop(handle, None)
            return None
        return binding.model_copy(deep=True)

    def pending_candidate(
        self,
        subject_id: str,
        candidate_hash: str,
        *,
        as_of: datetime,
    ) -> PendingProfileCandidate | None:
        candidate = self._pending_candidates.get(
            (subject_id, candidate_hash)
        )
        if (
            candidate is None
            or candidate.status != "pending"
            or candidate.expires_at <= as_of
        ):
            return None
        return candidate.model_copy(deep=True)

    def skill_outcomes(self) -> list[SkillOutcomeRecord]:
        return [item.model_copy(deep=True) for item in self._skill_outcomes.values()]

    def offline_outcomes(self) -> list[OfflineSkillOutcomeEnvelope]:
        return [item.model_copy(deep=True) for item in self._offline_outcomes.values()]

    def job(self, job_id: str) -> InductionJob:
        return self._jobs[job_id].model_copy(deep=True)

    def job_events(self, job_id: str) -> list[InductionJobEvent]:
        return [
            item.model_copy(deep=True)
            for item in self._job_events
            if item.job_id == job_id
        ]

    def attempts(self, job_id: str) -> list[InductionAttemptRecord]:
        return [
            item.model_copy(deep=True)
            for item in self._attempts
            if item.job_id == job_id
        ]

    def receipts(self, job_id: str) -> list[InductionReceipt]:
        return [
            item.model_copy(deep=True)
            for item in self._receipts.values()
            if item.job_id == job_id
        ]

    def induction_worker_view(self) -> "_InductionWorkerRepositoryView":
        return _InductionWorkerRepositoryView(self)


class _InductionWorkerRepositoryView:
    """Capability-limited view: deliberately has no result/history reader."""

    def __init__(self, repository: InMemoryLongitudinalResultStore) -> None:
        self.__repository = repository

    def lease_next_job(self, **kwargs: Any) -> InductionJob | None:
        return self.__repository.lease_next_job(**kwargs)

    def semantic_receipt(self, job_id: str) -> InductionReceipt | None:
        return self.__repository.semantic_receipt(job_id)

    def get_manifest(self, manifest_id: str) -> InductionInputManifest | None:
        return self.__repository.get_manifest(manifest_id)

    def current_epochs(self, subject_id: str) -> SubjectEpochs:
        return self.__repository.current_epochs(subject_id)

    def latest_terminal_revision(self, episode_id: str) -> int:
        return self.__repository.latest_terminal_revision(episode_id)

    def receipts(self, job_id: str) -> list[InductionReceipt]:
        return self.__repository.receipts(job_id)

    def complete_induction(self, **kwargs: Any) -> None:
        self.__repository.complete_induction(**kwargs)

    def fail_induction(self, **kwargs: Any) -> InductionReceipt | None:
        return self.__repository.fail_induction(**kwargs)


class DeterministicInductionWorker:
    """No-model post-Episode projector over the sanitized Manifest Store."""

    def __init__(
        self,
        repository: Any,
        *,
        worker_id: str = "induction-worker",
        max_attempts: int = 3,
        retention_policy: DataRetentionPolicy = DEFAULT_RETENTION_POLICY,
    ) -> None:
        self.repository = (
            repository.induction_worker_view()
            if hasattr(repository, "induction_worker_view")
            else repository
        )
        self.worker_id = worker_id
        self.max_attempts = max_attempts
        self.retention_policy = retention_policy

    def process_next(self, *, now: datetime | None = None) -> InductionReceipt | None:
        current_time = now or datetime.now(timezone.utc)
        job = self.repository.lease_next_job(
            worker_id=self.worker_id,
            now=current_time,
        )
        if job is None:
            return None
        try:
            existing = self.repository.semantic_receipt(job.job_id)
            if existing is not None:
                return existing
            manifest = self.repository.get_manifest(job.input_manifest_ref)
            if manifest is None or current_time >= job.manifest_expires_at:
                raise ValueError("source_manifest_expired")
            if manifest.manifest_hash != job.input_manifest_hash:
                raise ValueError("manifest_hash_mismatch")
            if manifest.source_result_hash != job.source_result_hash:
                raise ValueError("manifest_result_binding_mismatch")
            digest, candidates, skill_outcomes, offline, decision_codes, exclusions = (
                self._derive(job, manifest, now=current_time)
            )
            status = (
                InductionReceiptStatus.SUCCEEDED
                if digest is not None or candidates or skill_outcomes
                else InductionReceiptStatus.EXCLUDED
            )
            previous = self.repository.receipts(job.job_id)
            parent_ref = (
                previous[-1].receipt_id
                if job.processing_generation > 1 and previous
                else None
            )
            receipt = InductionReceipt(
                receipt_id=(
                    "induction-receipt:"
                    + stable_hash(
                        (
                            job.job_id,
                            job.processing_generation,
                            status.value,
                            tuple(decision_codes),
                        )
                    )
                ),
                job_id=job.job_id,
                processing_generation=job.processing_generation,
                episode_id=job.episode_id,
                terminal_result_id=job.terminal_result_id,
                source_receipt_revision=job.source_receipt_revision,
                source_result_hash=job.source_result_hash,
                input_manifest_hash=job.input_manifest_hash,
                induction_version=job.induction_version,
                status=status,
                decision_codes=tuple(decision_codes),
                episode_digest_ref=digest.digest_id if digest else None,
                profile_candidate_refs=tuple(
                    item.candidate_id for item in candidates
                ),
                skill_outcome_refs=tuple(
                    item.outcome_id for item in skill_outcomes
                ),
                exclusion_reasons=tuple(exclusions),
                attempt_count=job.attempt_count,
                parent_receipt_ref=parent_ref,
                completed_at=current_time,
            )
            self.repository.complete_induction(
                job=job,
                receipt=receipt,
                digest=digest,
                candidates=tuple(candidates),
                skill_outcomes=tuple(skill_outcomes),
                offline_outcomes=tuple(offline),
                now=current_time,
            )
            return receipt
        except Exception as exc:
            dead = self.repository.fail_induction(
                job=job,
                error_code=type(exc).__name__
                if str(exc) == ""
                else str(exc)[:160],
                now=current_time,
                max_attempts=self.max_attempts,
            )
            return dead

    def process_all(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[InductionReceipt]:
        receipts: list[InductionReceipt] = []
        for _ in range(limit):
            receipt = self.process_next(now=now)
            if receipt is None:
                break
            receipts.append(receipt)
        return receipts

    def _derive(
        self,
        job: InductionJob,
        manifest: InductionInputManifest,
        *,
        now: datetime,
    ) -> tuple[
        EpisodeDigest | None,
        list[PendingProfileCandidate],
        list[SkillOutcomeRecord],
        list[OfflineSkillOutcomeEnvelope],
        list[str],
        list[str],
    ]:
        epochs = self.repository.current_epochs(manifest.subject_id)
        decision_codes: list[str] = []
        exclusions: list[str] = []
        if (
            epochs.privacy_epoch != manifest.privacy_epoch
            or epochs.authorization_epoch != manifest.authorization_epoch
        ):
            return (
                None,
                [],
                [],
                [],
                ["privacy_epoch_changed"],
                ["authorization_or_privacy_changed"],
            )
        if (
            self.repository.latest_terminal_revision(manifest.episode_id)
            > manifest.source_receipt_revision
        ):
            return (
                None,
                [],
                [],
                [],
                ["superseded_terminal_revision"],
                ["newer_terminal_result_exists"],
            )
        if manifest.episode_type == EpisodeType.URGENT_BOUNDARY:
            return (
                None,
                [],
                [],
                [],
                ["urgent_audit_only"],
                ["urgent_boundary_excluded"],
            )
        skill_outcomes, offline = self._skill_outcomes(manifest, now=now)
        if manifest.episode_status in {EpisodeStatus.PARTIAL, EpisodeStatus.BLOCKED}:
            return (
                None,
                [],
                skill_outcomes,
                offline,
                [f"operational_{manifest.episode_status.value}"],
                ["no_retrievable_personal_memory"],
            )
        if manifest.episode_status != EpisodeStatus.COMPLETE:
            return (
                None,
                [],
                skill_outcomes,
                offline,
                ["unsupported_terminal_status"],
                ["status_not_digest_eligible"],
            )
        if not manifest.accepted_typed_views:
            return (
                None,
                [],
                skill_outcomes,
                offline,
                ["no_accepted_typed_input"],
                ["insufficient_induction_input"],
            )
        digest = self._digest(manifest, now=now)
        candidates = self._profile_candidates(manifest, now=now)
        decision_codes.extend(("digest_created", "source_revalidation_required"))
        if candidates:
            decision_codes.append("pending_candidates_created")
        if skill_outcomes:
            decision_codes.append("skill_outcomes_recorded")
        return (
            digest,
            candidates,
            skill_outcomes,
            offline,
            decision_codes,
            exclusions,
        )

    def _digest(
        self,
        manifest: InductionInputManifest,
        *,
        now: datetime,
    ) -> EpisodeDigest:
        concepts = sorted(
            {
                concept
                for view in manifest.accepted_typed_views
                for concept in view.concept_ids
            }
            or set(_EPISODE_CONCEPTS[manifest.episode_type])
        )
        evidence_refs = tuple(
            sorted(
                view.work_product_ref
                for view in manifest.accepted_typed_views
                if view.agent_id == AgentId.EVIDENCE_REASONING
            )
        )
        care_refs = tuple(
            sorted(
                view.work_product_ref
                for view in manifest.accepted_typed_views
                if view.agent_id == AgentId.CARE_STRATEGY
            )
        )
        safety_refs = tuple(
            sorted(
                view.work_product_ref
                for view in manifest.accepted_typed_views
                if view.agent_id == AgentId.SAFETY_REVIEW
            )
        )
        outcome_codes = sorted(
            {
                f"episode_status:{manifest.episode_status.value}",
                *(
                    code
                    for view in manifest.accepted_typed_views
                    for code in (
                        *view.evidence_codes,
                        *view.care_codes,
                        *view.safety_codes,
                    )
                ),
            }
        )
        zone = ZoneInfo(manifest.source_scope.timezone_name)
        start_date = (
            manifest.source_scope.date_start
            or manifest.terminal_recorded_at.astimezone(zone).date()
        )
        end_date = (
            manifest.source_scope.date_end
            or manifest.terminal_recorded_at.astimezone(zone).date()
        )
        observation_start = datetime.combine(start_date, time.min, tzinfo=zone)
        observation_end = datetime.combine(end_date, time.max, tzinfo=zone)
        digest_id = f"episode-digest:{manifest.source_result_hash}"
        return EpisodeDigest(
            digest_id=digest_id,
            subject_id=manifest.subject_id,
            episode_id=manifest.episode_id,
            episode_type=manifest.episode_type,
            terminal_result_id=manifest.terminal_result_id,
            source_receipt_revision=manifest.source_receipt_revision,
            source_result_ref=f"product-result:{manifest.terminal_result_id}",
            source_result_hash=manifest.source_result_hash,
            fact_snapshot_id=manifest.fact_snapshot_id,
            fact_snapshot_hash=manifest.fact_snapshot_hash,
            source_scope=manifest.source_scope,
            terminal_recorded_at=manifest.terminal_recorded_at,
            concept_ids=tuple(concepts),
            event_type_codes=(f"episode:{manifest.episode_type.value}",),
            outcome_codes=tuple(outcome_codes),
            observation_window_start=observation_start,
            observation_window_end=observation_end,
            accepted_evidence_refs=evidence_refs,
            accepted_care_refs=care_refs,
            safety_decision_refs=safety_refs,
            confirmation_refs=manifest.confirmation_refs,
            source_lineage_refs=(
                str(manifest.manifest_hash),
                manifest.source_result_hash,
            ),
            eligible_purposes=(MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT,),
            eligible_roles=(AgentId.EVIDENCE_REASONING,),
            sensitivity_class=SensitivityClass.SENSITIVE_PERSONAL,
            valid_from=manifest.terminal_recorded_at,
            expires_at=self.retention_policy.digest_expiry(
                manifest.terminal_recorded_at
            ),
            retention_policy_version=self.retention_policy.policy_version,
        )

    def _profile_candidates(
        self,
        manifest: InductionInputManifest,
        *,
        now: datetime,
    ) -> list[PendingProfileCandidate]:
        committed = set(manifest.confirmation_refs)
        declined = set(manifest.declined_candidate_refs)
        candidates: list[PendingProfileCandidate] = []
        for view in manifest.accepted_typed_views:
            for projection in view.profile_candidates:
                if (
                    projection.candidate_id in committed
                    or projection.candidate_id in declined
                ):
                    continue
                candidates.append(
                    PendingProfileCandidate(
                        candidate_id=projection.candidate_id,
                        candidate_hash=projection.candidate_hash,
                        subject_id=manifest.subject_id,
                        source_result_hash=manifest.source_result_hash,
                        projection=projection,
                        lineage_refs=(
                            str(manifest.manifest_hash),
                            view.work_product_ref,
                        ),
                        created_at=now,
                        expires_at=self.retention_policy.candidate_expiry(now),
                    )
                )
        return candidates

    def _skill_outcomes(
        self,
        manifest: InductionInputManifest,
        *,
        now: datetime,
    ) -> tuple[list[SkillOutcomeRecord], list[OfflineSkillOutcomeEnvelope]]:
        outcomes: list[SkillOutcomeRecord] = []
        offline: list[OfflineSkillOutcomeEnvelope] = []
        for view in manifest.skill_invocation_views:
            succeeded = view.validation_status.lower() in {
                "valid",
                "accepted",
                "completed",
            }
            status = "succeeded" if succeeded else "failed"
            root = "none" if succeeded else "model_output"
            outcome = SkillOutcomeRecord(
                outcome_id=(
                    "skill-outcome:"
                    + stable_hash(
                        (
                            manifest.source_result_hash,
                            view.invocation_id,
                            view.skill_id,
                        )
                    )
                ),
                episode_id=manifest.episode_id,
                terminal_result_id=manifest.terminal_result_id,
                invocation_id=view.invocation_id,
                agent_id=view.agent_id,
                skill_id=view.skill_id,
                skill_version=view.skill_version,
                package_hash=view.package_hash,
                status=status,
                root_cause=root,
                reason_codes=(f"validation:{view.validation_status}",),
                created_at=now,
            )
            outcomes.append(outcome)
            offline.append(
                OfflineSkillOutcomeEnvelope(
                    envelope_id=(
                        "offline-skill-outcome:"
                        + stable_hash(
                            (
                                outcome.outcome_id,
                                manifest.privacy_epoch,
                                "offline.v1",
                            )
                        )
                    ),
                    skill_id=view.skill_id,
                    skill_version=view.skill_version,
                    package_hash=view.package_hash,
                    agent_id=view.agent_id,
                    status=status,
                    root_cause=root,
                    reason_codes=outcome.reason_codes,
                    episode_type=manifest.episode_type,
                    time_bucket=now.strftime("%Y-%m"),
                    eligibility_epoch=manifest.privacy_epoch,
                    created_at=now,
                )
            )
        return outcomes, offline


class _RetrievalCandidate(StrictContract):
    persistent_ref: str
    item_kind: LongitudinalMemoryType
    concept_ids: tuple[str, ...]
    event_codes: tuple[str, ...] = ()
    outcome_codes: tuple[str, ...] = ()
    provenance_code: str
    display_value: Any | None = None
    source_refs: tuple[str, ...]
    source_label: str
    status: str
    source_availability: Literal["available", "unavailable", "unknown"]
    allowed_current_use: Literal[
        "confirmed_memory",
        "episodic_hint_only",
        "explicit_review_only",
    ]
    valid_from: datetime
    expires_at: datetime
    conflict_refs: tuple[str, ...] = ()
    trust_label: str
    sort_time: datetime


class LongitudinalMemoryService:
    """Purpose-bound deterministic retrieval over Memory V2 and Digest hints."""

    def __init__(
        self,
        *,
        memory_store: Any,
        repository: Any,
        source_resolvers: dict[str, CanonicalSourceResolver] | None = None,
        retention_policy: DataRetentionPolicy = DEFAULT_RETENTION_POLICY,
    ) -> None:
        self.memory_store = memory_store
        self.repository = repository
        configured_resolvers = source_resolvers or {}
        allowed_resolver_classes = {
            "canonical_observation",
            "trend_tool",
            "user_report",
            "authorized_observer_report",
            "confirmed_memory",
            "accepted_ledger",
            "care_state",
            "reviewed_knowledge",
            "opaque_source",
        }
        if not set(configured_resolvers).issubset(allowed_resolver_classes):
            raise ValueError("audit/raw source readers cannot be Memory resolvers")
        self.source_resolvers = dict(configured_resolvers)
        self.retention_policy = retention_policy
        self._budget_lock = RLock()
        self._token_usage: dict[tuple[str, str, str, str], int] = {}

    def read(
        self,
        arguments: dict[str, Any],
        context: Any,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current_time = now or datetime.now(timezone.utc)
        intent = MemoryQueryIntent.model_validate(arguments)
        query = self._resolve_query(intent, context, now=current_time)
        candidates, filter_reasons = self._candidates(query, now=current_time)
        selected, next_cursor, selection_reasons = self._select(
            candidates,
            query,
            now=current_time,
        )
        selected_conflict_labels = {
            item.persistent_ref: label
            for label, group, incomplete in self._build_conflict_groups(
                selected
            )
            if not incomplete
            and (
                len(group) > 1
                or any(candidate.conflict_refs for candidate in group)
            )
            for item in group
        }
        slice_items: list[MemorySliceItem] = []
        selected_refs: list[str] = []
        for candidate in selected:
            expires_at = min(
                candidate.expires_at,
                current_time
                + timedelta(minutes=self.retention_policy.handle_ttl_minutes),
            )
            handle = "mh_" + secrets.token_urlsafe(24)
            binding = HandleBinding(
                handle=handle,
                subject_id=query.subject_id,
                actor_id=query.actor_id,
                requesting_agent=query.requesting_agent,
                purpose=query.purpose,
                invocation_id=query.invocation_id,
                persistent_ref=candidate.persistent_ref,
                item_kind=candidate.item_kind,
                privacy_epoch=query.privacy_epoch,
                authorization_epoch=query.authorization_epoch,
                retrieval_policy_epoch=query.retrieval_policy_epoch,
                created_at=current_time,
                expires_at=expires_at,
            )
            self.repository.create_handle(binding)
            slice_items.append(
                MemorySliceItem(
                    retrieval_handle=handle,
                    item_kind=candidate.item_kind,
                    concept_ids=candidate.concept_ids,
                    event_codes=candidate.event_codes,
                    outcome_codes=candidate.outcome_codes,
                    provenance_code=candidate.provenance_code,
                    display_value=candidate.display_value,
                    selection_reason_codes=(
                        "exact_selector_match",
                        "hard_filters_passed",
                    ),
                    source_label=candidate.source_label,
                    status=candidate.status,
                    source_availability=candidate.source_availability,
                    allowed_current_use=candidate.allowed_current_use,
                    valid_from=candidate.valid_from,
                    expires_at=expires_at,
                    conflict_group=selected_conflict_labels.get(
                        candidate.persistent_ref
                    ),
                    trust_label=candidate.trust_label,
                )
            )
            selected_refs.append(candidate.persistent_ref)
        serialized = [item.model_dump(mode="json") for item in slice_items]
        actual_tokens = canonical_token_count(serialized)
        if actual_tokens > query.token_budget:
            self.repository.purge_exact_handles(
                tuple(item.retrieval_handle for item in slice_items)
            )
            raise ValueError("final serialized Memory slice exceeds token budget")
        budget_key = self._budget_key(context)
        with self._budget_lock:
            used = self._token_usage.get(budget_key, 0)
            if used + actual_tokens > context.memory_token_budget:
                self.repository.purge_exact_handles(
                    tuple(item.retrieval_handle for item in slice_items)
                )
                raise ValueError("Agent/Episode Memory budget exhausted")
            self._token_usage[budget_key] = used + actual_tokens
        receipt_id = "memory-read:" + stable_hash(
            (
                query.query_id,
                tuple(selected_refs),
                current_time.isoformat(),
            )
        )
        receipt = MemoryReadReceipt(
            receipt_id=receipt_id,
            query_id=query.query_id,
            query_hash=stable_hash(query),
            subject_id=query.subject_id,
            requesting_agent=query.requesting_agent,
            purpose=query.purpose,
            candidate_count=len(candidates),
            selected_persistent_refs=tuple(selected_refs),
            selected_handles=tuple(
                item.retrieval_handle for item in slice_items
            ),
            filter_reason_codes=tuple(sorted(set(filter_reasons))),
            selection_reason_codes=tuple(sorted(set(selection_reasons))),
            actual_tokens=actual_tokens,
            privacy_epoch=query.privacy_epoch,
            authorization_epoch=query.authorization_epoch,
            retrieval_policy_epoch=query.retrieval_policy_epoch,
            completed_at=current_time,
        )
        self.repository.save_memory_read_receipt(receipt)
        return {
            "schema_version": "MemoryReadSlice.v1",
            "items": serialized,
            "reason_codes": sorted(
                set([*filter_reasons, *selection_reasons])
            ),
            "next_inventory_cursor": next_cursor,
            "read_receipt_id": receipt.receipt_id,
            "token_count": actual_tokens,
            "tokenizer_version": TOKENIZER_VERSION,
            "source_refs": [
                receipt.receipt_id,
                *(item.retrieval_handle for item in slice_items),
            ],
        }

    def resolve_source(
        self,
        arguments: dict[str, Any],
        context: Any,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current_time = now or datetime.now(timezone.utc)
        if set(arguments) != {"retrieval_handle"}:
            raise ValueError("source resolver accepts one exact retrieval_handle")
        handle = str(arguments["retrieval_handle"])
        binding = self.repository.resolve_handle(handle, now=current_time)
        if binding is None:
            raise ValueError("unknown_or_expired_retrieval_handle")
        if not isinstance(context.caller, AgentId) or (
            context.caller != AgentId.EVIDENCE_REASONING
        ):
            raise ValueError("only Evidence may revalidate episodic sources")
        self._validate_handle_binding(binding, context, now=current_time)
        if binding.item_kind == LongitudinalMemoryType.GOVERNED_MEMORY:
            item = self._governed_item(
                binding.subject_id,
                binding.persistent_ref.split(":", 1)[1],
            )
            if item is None or not self._v2_current(item, current_time):
                raise ValueError("canonical_memory_source_unavailable")
            canonical_handle = "cs_" + secrets.token_urlsafe(24)
            return {
                "schema_version": "CanonicalMemorySource.v1",
                "canonical_source_handle": canonical_handle,
                "source_class": "confirmed_memory",
                "concept_id": item.concept_id,
                "typed_value": item.typed_value,
                "observed_at": item.recorded_at.isoformat(),
                "trust_label": USER_MEMORY_TRUST_LABEL,
                "source_refs": [canonical_handle],
            }
        digest_id = binding.persistent_ref.split(":", 1)[1]
        digest = next(
            (
                item
                for item in self.repository.list_active_digests(
                    binding.subject_id,
                    as_of=current_time,
                )
                if item.digest_id == digest_id
            ),
            None,
        )
        if digest is None:
            raise ValueError("episodic_source_unavailable")
        result: dict[str, Any] | None = None
        for source_ref in (
            *digest.accepted_evidence_refs,
            *digest.accepted_care_refs,
        ):
            resolver = self.source_resolvers.get(_source_class(source_ref))
            if resolver is None:
                resolver = self.source_resolvers.get("opaque_source")
            if resolver is not None:
                result = resolver((source_ref,), self._query_for_handle(binding, context))
                if result is not None:
                    break
        if result is None:
            raise ValueError("canonical_source_resolver_unavailable")
        canonical_handle = "cs_" + secrets.token_urlsafe(24)
        return {
            "schema_version": "CanonicalEpisodeSource.v1",
            "canonical_source_handle": canonical_handle,
            "source_class": str(result.get("source_class", "canonical_source")),
            "typed_result": result.get("typed_result"),
            "observed_at": str(result.get("observed_at", current_time.isoformat())),
            "source_refs": [canonical_handle],
        }

    def validate_model_input(
        self,
        receipt_output: dict[str, Any],
        context: Any,
        *,
        now: datetime | None = None,
    ) -> None:
        """Recheck every model-visible handle immediately before invocation."""

        current_time = now or datetime.now(timezone.utc)
        for item in receipt_output.get("items", []):
            handle = item.get("retrieval_handle") if isinstance(item, dict) else None
            if not handle:
                raise ValueError("Memory slice item lacks a retrieval handle")
            binding = self.repository.resolve_handle(
                str(handle),
                now=current_time,
            )
            if binding is None:
                raise ValueError("Memory slice handle expired or was revoked")
            self._validate_handle_binding(binding, context, now=current_time)
            self._validate_persistent_binding_current(binding, now=current_time)

    def validate_prepublication(
        self,
        receipt_outputs: tuple[dict[str, Any], ...],
        *,
        subject_id: str,
        actor_id: str,
        now: datetime | None = None,
    ) -> None:
        """Revocation wins over an already assembled response."""

        current_time = now or datetime.now(timezone.utc)
        epochs = self.repository.current_epochs(subject_id)
        for output in receipt_outputs:
            for item in output.get("items", []):
                handle = (
                    item.get("retrieval_handle")
                    if isinstance(item, dict)
                    else None
                )
                binding = (
                    self.repository.resolve_handle(
                        str(handle),
                        now=current_time,
                    )
                    if handle
                    else None
                )
                if (
                    binding is None
                    or binding.subject_id != subject_id
                    or binding.actor_id != actor_id
                    or binding.privacy_epoch != epochs.privacy_epoch
                    or binding.authorization_epoch != epochs.authorization_epoch
                    or binding.retrieval_policy_epoch
                    != epochs.retrieval_policy_epoch
                    or binding.expires_at <= current_time
                ):
                    raise ValueError(
                        "Memory authorization changed before publication"
                    )
                self._validate_persistent_binding_current(
                    binding,
                    now=current_time,
                )

    def review_pending_candidates(
        self,
        arguments: dict[str, Any],
        context: Any,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current_time = now or datetime.now(timezone.utc)
        if arguments:
            raise ValueError("candidate review does not accept model-selected scope")
        if (
            context.fact_snapshot.binding.role != "elder"
            or context.user_intent_purpose
            != MemoryPurpose.EXPLICIT_MEMORY_REVIEW
        ):
            raise ValueError("pending candidate review requires current elder intent")
        candidates = self.repository.list_pending_candidates(
            context.fact_snapshot.binding.subject_id,
            as_of=current_time,
        )
        serialized = []
        epochs = self.repository.current_epochs(
            context.fact_snapshot.binding.subject_id
        )
        for item in candidates[:8]:
            handle = "pc_" + secrets.token_urlsafe(18)
            self.repository.create_candidate_review_handle(
                CandidateReviewHandle(
                    handle=handle,
                    candidate_hash=item.candidate_hash,
                    subject_id=item.subject_id,
                    actor_id=context.fact_snapshot.binding.actor_id,
                    privacy_epoch=epochs.privacy_epoch,
                    authorization_epoch=epochs.authorization_epoch,
                    retrieval_policy_epoch=epochs.retrieval_policy_epoch,
                    expires_at=min(
                        item.expires_at,
                        current_time
                        + timedelta(
                            minutes=self.retention_policy.handle_ttl_minutes
                        ),
                    ),
                )
            )
            serialized.append(
                {
                    "candidate_handle": handle,
                    "concept_id": item.projection.concept_id,
                    "typed_value": item.projection.typed_value,
                    "status": "pending_uncommitted",
                    "expires_at": item.expires_at.isoformat(),
                }
            )
        return {
            "schema_version": "PendingCandidateReview.v1",
            "notice": "尚未保存",
            "candidates": serialized,
            "source_refs": [],
        }

    def prepare_pending_candidate(
        self,
        arguments: dict[str, Any],
        context: Any,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current_time = now or datetime.now(timezone.utc)
        if set(arguments) != {"candidate_handle"}:
            raise ValueError("candidate preparation requires one exact handle")
        if (
            context.caller != AgentId.SLEEP_CARE
            or context.fact_snapshot.binding.role != "elder"
            or context.user_intent_purpose
            != MemoryPurpose.EXPLICIT_MEMORY_CHANGE
        ):
            raise ValueError("candidate preparation requires current elder change intent")
        binding = self.repository.resolve_candidate_review_handle(
            str(arguments["candidate_handle"]),
            now=current_time,
        )
        if binding is None:
            raise ValueError("candidate review handle expired")
        epochs = self.repository.current_epochs(binding.subject_id)
        actual = context.fact_snapshot.binding
        if (
            binding.subject_id != actual.subject_id
            or binding.actor_id != actual.actor_id
            or binding.privacy_epoch != epochs.privacy_epoch
            or binding.authorization_epoch != epochs.authorization_epoch
            or binding.retrieval_policy_epoch
            != epochs.retrieval_policy_epoch
        ):
            raise ValueError("candidate review binding changed")
        pending = self.repository.pending_candidate(
            binding.subject_id,
            binding.candidate_hash,
            as_of=current_time,
        )
        if pending is None:
            raise ValueError("pending candidate is no longer eligible")
        projection = pending.projection
        candidate = MemoryChangeCandidate(
            candidate_id=projection.candidate_id,
            operation=projection.operation,
            subject_id=pending.subject_id,
            memory_type=projection.memory_type,
            concept_id=projection.concept_id,
            value_schema_id=projection.value_schema_id,
            value_schema_version=projection.value_schema_version,
            typed_value=projection.typed_value,
            provenance_type=projection.provenance_type,
            source_ref=f"induction-candidate:{pending.candidate_hash}",
            sensitivity_class=projection.sensitivity_class,
            allowed_roles=(
                AgentId.SLEEP_CARE,
                AgentId.EVIDENCE_REASONING,
            ),
            allowed_purposes=(
                MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT.value,
                MemoryPurpose.EXPLICIT_MEMORY_REVIEW.value,
                MemoryPurpose.EXPLICIT_MEMORY_CHANGE.value,
                MemoryPurpose.EXPLICIT_MEMORY_FORGET.value,
            ),
            confirmation_required=True,
        )
        return {
            "schema_version": "PendingCandidatePreparation.v1",
            "status": "pending_exact_confirmation",
            "memory_change_candidate": candidate.model_dump(mode="json"),
            "notice": "尚未保存；需要老人对这一项单独确认。",
            "source_refs": [],
        }

    def _resolve_query(
        self,
        intent: MemoryQueryIntent,
        context: Any,
        *,
        now: datetime,
    ) -> ResolvedMemoryQuery:
        if not isinstance(context.caller, AgentId):
            raise ValueError("memory.read requires an Agent caller")
        binding = context.fact_snapshot.binding
        if not set(context.authorization_scope).issubset(
            set(binding.authorization_scope)
        ):
            raise ValueError("runtime authorization exceeds authenticated binding")
        if not set(context.authorization_scope).intersection(
            {
                "personal_memory:read",
                "personal_memory:manage",
                "memory:read",
                "memory:manage",
            }
        ):
            raise ValueError("Memory authorization scope is absent")
        epochs = self.repository.current_epochs(binding.subject_id)
        budget_key = self._budget_key(context)
        with self._budget_lock:
            remaining_budget = (
                context.memory_token_budget
                - self._token_usage.get(budget_key, 0)
            )
        if remaining_budget < 1:
            raise ValueError("Agent/Episode Memory budget exhausted")
        if context.caller == AgentId.EVIDENCE_REASONING:
            if intent.purpose != MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT:
                raise ValueError("Evidence may only request personal evidence context")
            if intent.selector_kind != MemorySelectorKind.CONCEPT_IDS:
                raise ValueError("Evidence requires exact concept selectors")
        elif context.caller == AgentId.SLEEP_CARE:
            if intent.purpose == MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT:
                raise ValueError("SleepCare cannot request Evidence memory")
            if binding.role != "elder":
                raise ValueError("explicit Memory management requires the elder")
            if context.user_intent_purpose != intent.purpose:
                raise ValueError("Memory purpose lacks current top-level user intent")
            if LongitudinalMemoryType.EPISODE_DIGEST in intent.memory_types:
                raise ValueError("SleepCare cannot retrieve EpisodeDigest")
        else:
            raise ValueError("Agent has no direct longitudinal read")
        if not context.invocation_id or not context.plan_step_id:
            raise ValueError("Memory query lacks runtime plan-step causality")
        if intent.requested_time_scope != context.fact_snapshot.source_scope.kind:
            raise ValueError("requested time scope is not the Episode scope")
        as_of = min(now, context.fact_snapshot.source_scope.as_of)
        return ResolvedMemoryQuery(
            query_id="memory-query:" + secrets.token_urlsafe(18),
            episode_id=context.episode_id or "",
            plan_revision=context.plan_revision or 0,
            plan_step_id=context.plan_step_id,
            invocation_id=context.invocation_id,
            requesting_agent=context.caller,
            purpose=intent.purpose,
            memory_types=intent.memory_types,
            selector_kind=intent.selector_kind,
            requested_concept_ids=intent.requested_concept_ids,
            item_handles=intent.item_handles,
            inventory_cursor=intent.inventory_cursor,
            source_scope=context.fact_snapshot.source_scope,
            as_of=as_of,
            max_items=min(context.max_memory_items, 8),
            token_budget=remaining_budget,
            actor_id=binding.actor_id,
            subject_id=binding.subject_id,
            actor_role=binding.role,
            authorization_scope=context.authorization_scope,
            fact_snapshot_id=context.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=context.fact_snapshot.fact_snapshot_hash,
            privacy_epoch=epochs.privacy_epoch,
            authorization_epoch=epochs.authorization_epoch,
            retrieval_policy_epoch=epochs.retrieval_policy_epoch,
            user_intent_ref=context.user_intent_ref,
            user_intent_hash=context.user_intent_hash,
        )

    @staticmethod
    def _budget_key(context: Any) -> tuple[str, str, str, str]:
        caller = (
            context.caller.value
            if isinstance(context.caller, AgentId)
            else str(context.caller)
        )
        return (
            context.fact_snapshot.binding.subject_id,
            context.episode_id or "",
            context.invocation_id,
            caller,
        )

    def _candidates(
        self,
        query: ResolvedMemoryQuery,
        *,
        now: datetime,
    ) -> tuple[list[_RetrievalCandidate], list[str]]:
        candidates: list[_RetrievalCandidate] = []
        reasons: list[str] = []
        state = self.memory_store.get(query.subject_id)
        if LongitudinalMemoryType.GOVERNED_MEMORY in query.memory_types:
            latest_items: dict[str, MemoryItemRecord] = {}
            for candidate in state.items:
                current = latest_items.get(candidate.memory_id)
                if current is None or candidate.version > current.version:
                    latest_items[candidate.memory_id] = candidate
            for item in latest_items.values():
                if isinstance(item, LegacyMemoryItemV1):
                    if (
                        query.requesting_agent == AgentId.SLEEP_CARE
                        and query.purpose
                        == MemoryPurpose.EXPLICIT_MEMORY_REVIEW
                        and query.selector_kind
                        == MemorySelectorKind.INVENTORY_PAGE
                        and item.active
                    ):
                        candidates.append(
                            _RetrievalCandidate(
                                persistent_ref=(
                                    f"memory:{item.memory_id}:v{item.version}"
                                ),
                                item_kind=LongitudinalMemoryType.GOVERNED_MEMORY,
                                concept_ids=("legacy.unclassified",),
                                provenance_code="legacy_unclassified",
                                display_value=item.value[:160],
                                source_refs=(item.source_ref,),
                                source_label="legacy_memory",
                                status="legacy_unclassified",
                                source_availability="unknown",
                                allowed_current_use="explicit_review_only",
                                valid_from=now,
                                expires_at=now
                                + timedelta(
                                    minutes=self.retention_policy.handle_ttl_minutes
                                ),
                                trust_label=USER_MEMORY_TRUST_LABEL,
                                sort_time=now,
                            )
                        )
                    else:
                        reasons.append("legacy_memory_filtered")
                    continue
                if item.subject_id != query.subject_id:
                    reasons.append("subject_filtered")
                    continue
                if not self._v2_current(item, now):
                    reasons.append("inactive_or_expired_filtered")
                    continue
                if query.requesting_agent not in item.allowed_roles:
                    reasons.append("role_filtered")
                    continue
                if query.purpose not in item.allowed_purposes:
                    reasons.append("purpose_filtered")
                    continue
                if (
                    query.source_scope.kind
                    != SourceScopeKind.HISTORICAL_RANGE
                    and item.source_scope_kind != query.source_scope.kind
                ):
                    reasons.append("source_scope_filtered")
                    continue
                candidates.append(
                    _RetrievalCandidate(
                        persistent_ref=f"memory:{item.memory_id}:v{item.version}",
                        item_kind=LongitudinalMemoryType.GOVERNED_MEMORY,
                        concept_ids=(item.concept_id,),
                        provenance_code=item.provenance_type.value,
                        display_value=item.typed_value,
                        source_refs=(item.source_ref,),
                        source_label=_source_class(item.source_ref),
                        status=item.status.value,
                        source_availability="available",
                        allowed_current_use="confirmed_memory",
                        valid_from=item.valid_from,
                        expires_at=item.valid_until
                        or (
                            now
                            + timedelta(
                                minutes=self.retention_policy.handle_ttl_minutes
                            )
                        ),
                        conflict_refs=item.conflict_refs,
                        trust_label=USER_MEMORY_TRUST_LABEL,
                        sort_time=item.valid_from,
                    )
                )
        if LongitudinalMemoryType.EPISODE_DIGEST in query.memory_types:
            if (
                query.requesting_agent != AgentId.EVIDENCE_REASONING
                or query.purpose != MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT
                or not self.repository.digest_read_enabled()
            ):
                reasons.append("digest_read_disabled_or_unauthorized")
            else:
                for digest in self.repository.list_active_digests(
                    query.subject_id,
                    as_of=query.as_of,
                ):
                    if query.requesting_agent not in digest.eligible_roles:
                        reasons.append("digest_role_filtered")
                        continue
                    if query.purpose not in digest.eligible_purposes:
                        reasons.append("digest_purpose_filtered")
                        continue
                    if (
                        query.source_scope.kind
                        != SourceScopeKind.HISTORICAL_RANGE
                        and digest.source_scope.kind != query.source_scope.kind
                    ):
                        reasons.append("digest_source_scope_filtered")
                        continue
                    candidates.append(
                        _RetrievalCandidate(
                            persistent_ref=f"digest:{digest.digest_id}",
                            item_kind=LongitudinalMemoryType.EPISODE_DIGEST,
                            concept_ids=digest.concept_ids,
                            event_codes=digest.event_type_codes,
                            outcome_codes=digest.outcome_codes,
                            provenance_code="accepted_episode_process",
                            source_refs=(
                                *digest.accepted_evidence_refs,
                                *digest.accepted_care_refs,
                            ),
                            source_label="episodic_source",
                            status=DigestStatus.ACTIVE.value,
                            source_availability=(
                                "available"
                                if (
                                    digest.accepted_evidence_refs
                                    or digest.accepted_care_refs
                                )
                                else "unavailable"
                            ),
                            allowed_current_use="episodic_hint_only",
                            valid_from=digest.valid_from,
                            expires_at=digest.expires_at,
                            conflict_refs=digest.conflict_refs,
                            trust_label=EPISODIC_HINT_TRUST_LABEL,
                            sort_time=digest.terminal_recorded_at,
                        )
                    )
        if query.selector_kind == MemorySelectorKind.CONCEPT_IDS:
            selected_concepts = set(query.requested_concept_ids)
            candidates = [
                item
                for item in candidates
                if selected_concepts.intersection(item.concept_ids)
            ]
        elif query.selector_kind == MemorySelectorKind.ITEM_HANDLES:
            allowed_refs: set[str] = set()
            for handle in query.item_handles:
                binding = self.repository.resolve_handle(handle, now=now)
                if binding is None:
                    continue
                self._validate_binding_against_query(binding, query)
                allowed_refs.add(binding.persistent_ref)
            candidates = [
                item for item in candidates if item.persistent_ref in allowed_refs
            ]
        return candidates, reasons

    def _select(
        self,
        candidates: list[_RetrievalCandidate],
        query: ResolvedMemoryQuery,
        *,
        now: datetime,
    ) -> tuple[list[_RetrievalCandidate], str | None, list[str]]:
        sorted_candidates = sorted(
            candidates,
            key=lambda item: (
                0
                if set(query.requested_concept_ids).intersection(item.concept_ids)
                else 1,
                0 if item.source_availability == "available" else 1,
                0 if item.status == "active" else 1,
                -item.sort_time.timestamp(),
                item.persistent_ref,
            ),
        )
        next_cursor: str | None = None
        if query.selector_kind == MemorySelectorKind.INVENTORY_PAGE:
            start = 0
            if query.inventory_cursor != "FIRST":
                cursor = self.repository.resolve_cursor(
                    str(query.inventory_cursor),
                    now=now,
                )
                if cursor is None:
                    raise ValueError("invalid_or_expired_inventory_cursor")
                self._validate_cursor(cursor, query)
                start = next(
                    (
                        index + 1
                        for index, item in enumerate(sorted_candidates)
                        if item.persistent_ref == cursor.last_sort_key
                    ),
                    len(sorted_candidates),
                )
            sorted_candidates = sorted_candidates[start:]
        selected: list[_RetrievalCandidate] = []
        reasons: list[str] = []
        for _label, group, incomplete in self._build_conflict_groups(
            sorted_candidates
        ):
            if incomplete:
                reasons.append("conflict_group_incomplete")
                continue
            if len(selected) + len(group) > query.max_items:
                reasons.append(
                    "conflict_group_omitted_budget"
                    if any(item.conflict_refs for item in group)
                    else "item_limit_reached"
                )
                continue
            tentative = [
                {
                    "item_kind": item.item_kind.value,
                    "concept_ids": item.concept_ids,
                    "event_codes": item.event_codes,
                    "outcome_codes": item.outcome_codes,
                    "display_value": item.display_value,
                    "status": item.status,
                    "trust_label": item.trust_label,
                }
                for item in [*selected, *group]
            ]
            if canonical_token_count(tentative) > query.token_budget:
                reasons.append(
                    "conflict_group_omitted_budget"
                    if any(item.conflict_refs for item in group)
                    else "item_oversize_omitted"
                )
                continue
            selected.extend(group)
        if (
            query.selector_kind == MemorySelectorKind.INVENTORY_PAGE
            and len(sorted_candidates) > len(selected)
            and selected
        ):
            cursor = "mc_" + secrets.token_urlsafe(24)
            self.repository.issue_cursor(
                InventoryCursorBinding(
                    cursor=cursor,
                    subject_id=query.subject_id,
                    actor_id=query.actor_id,
                    purpose=query.purpose,
                    last_sort_key=selected[-1].persistent_ref,
                    privacy_epoch=query.privacy_epoch,
                    authorization_epoch=query.authorization_epoch,
                    retrieval_policy_epoch=query.retrieval_policy_epoch,
                    expires_at=now
                    + timedelta(
                        minutes=self.retention_policy.handle_ttl_minutes
                    ),
                )
            )
            next_cursor = cursor
        if not selected:
            reasons.append("empty_result_no_scope_widening")
        return selected, next_cursor, reasons

    @classmethod
    def _build_conflict_groups(
        cls,
        candidates: list[_RetrievalCandidate],
    ) -> list[tuple[str, list[_RetrievalCandidate], bool]]:
        """Return connected, fail-closed conflict components."""

        parents = list(range(len(candidates)))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[max(left_root, right_root)] = min(
                    left_root,
                    right_root,
                )

        alias_owner: dict[str, int | None] = {}
        for index, candidate in enumerate(candidates):
            for alias in cls._candidate_ref_aliases(candidate.persistent_ref):
                prior = alias_owner.get(alias)
                if prior is None and alias in alias_owner:
                    continue
                if prior is not None and prior != index:
                    alias_owner[alias] = None
                else:
                    alias_owner[alias] = index

        missing_indexes: set[int] = set()
        for index, candidate in enumerate(candidates):
            for conflict_ref in candidate.conflict_refs:
                other = alias_owner.get(conflict_ref)
                if other is None:
                    missing_indexes.add(index)
                    continue
                union(index, other)

        grouped: dict[int, list[_RetrievalCandidate]] = {}
        incomplete_roots = {find(index) for index in missing_indexes}
        for index, candidate in enumerate(candidates):
            grouped.setdefault(find(index), []).append(candidate)
        return [
            (
                min(item.persistent_ref for item in group),
                group,
                root in incomplete_roots,
            )
            for root, group in grouped.items()
        ]

    @staticmethod
    def _candidate_ref_aliases(persistent_ref: str) -> set[str]:
        aliases = {persistent_ref}
        if ":" not in persistent_ref:
            return aliases
        kind, inner = persistent_ref.split(":", 1)
        aliases.add(inner)
        if kind == "memory" and ":v" in inner:
            memory_id, raw_version = inner.rsplit(":v", 1)
            if raw_version.isdigit():
                aliases.add(memory_id)
                aliases.add(f"memory:{memory_id}")
        return aliases

    @staticmethod
    def _v2_current(item: GovernedMemoryItemV2, now: datetime) -> bool:
        return (
            item.status == MemoryItemStatus.ACTIVE
            and item.valid_from <= now
            and (item.valid_until is None or now < item.valid_until)
        )

    def _governed_item(
        self, subject_id: str, versioned_ref: str
    ) -> GovernedMemoryItemV2 | None:
        try:
            memory_id, raw_version = versioned_ref.rsplit(":v", 1)
            version = int(raw_version)
        except (ValueError, TypeError):
            return None
        state = self.memory_store.get(subject_id)
        return next(
            (
                item
                for item in state.items
                if isinstance(item, GovernedMemoryItemV2)
                and item.memory_id == memory_id
                and item.version == version
            ),
            None,
        )

    def _validate_handle_binding(
        self,
        binding: HandleBinding,
        context: Any,
        *,
        now: datetime,
    ) -> None:
        epochs = self.repository.current_epochs(binding.subject_id)
        actual = context.fact_snapshot.binding
        if (
            not set(context.authorization_scope).issubset(
                set(actual.authorization_scope)
            )
            or not set(context.authorization_scope).intersection(
                {
                    "personal_memory:read",
                    "personal_memory:manage",
                    "memory:read",
                    "memory:manage",
                }
            )
        ):
            raise ValueError("Memory authorization changed")
        if (
            binding.subject_id != actual.subject_id
            or binding.actor_id != actual.actor_id
            or binding.requesting_agent != context.caller
            or binding.invocation_id != context.invocation_id
            or binding.expires_at <= now
            or binding.privacy_epoch != epochs.privacy_epoch
            or binding.authorization_epoch != epochs.authorization_epoch
            or binding.retrieval_policy_epoch
            != epochs.retrieval_policy_epoch
        ):
            raise ValueError("retrieval_handle_binding_changed")

    def _validate_persistent_binding_current(
        self,
        binding: HandleBinding,
        *,
        now: datetime,
    ) -> None:
        if binding.item_kind == LongitudinalMemoryType.EPISODE_DIGEST:
            digest_id = binding.persistent_ref.split(":", 1)[1]
            if (
                self.repository.digest_status(digest_id)
                != DigestStatus.ACTIVE
            ):
                raise ValueError("Digest handle no longer current")
            digest = next(
                (
                    item
                    for item in self.repository.list_active_digests(
                        binding.subject_id,
                        as_of=now,
                    )
                    if item.digest_id == digest_id
                ),
                None,
            )
            if digest is None:
                raise ValueError("Digest handle no longer retrievable")
            return
        versioned_ref = binding.persistent_ref.split(":", 1)[1]
        try:
            memory_id, raw_version = versioned_ref.rsplit(":v", 1)
            version = int(raw_version)
        except (ValueError, TypeError) as exc:
            raise ValueError("Memory handle binding is malformed") from exc
        state = self.memory_store.get(binding.subject_id)
        matches = [
            candidate
            for candidate in state.items
            if candidate.memory_id == memory_id
        ]
        latest = max(
            matches,
            key=lambda candidate: candidate.version,
        ) if matches else None
        item = next(
            (
                candidate
                for candidate in matches
                if candidate.version == version
            ),
            None,
        )
        if (
            item is None
            or latest is None
            or latest.version != version
            or not item.active
        ):
            raise ValueError("Memory handle no longer current")
        if isinstance(item, GovernedMemoryItemV2) and not self._v2_current(item, now):
            raise ValueError("Memory handle no longer current")

    @staticmethod
    def _validate_binding_against_query(
        binding: HandleBinding,
        query: ResolvedMemoryQuery,
    ) -> None:
        if (
            binding.subject_id != query.subject_id
            or binding.actor_id != query.actor_id
            or binding.requesting_agent != query.requesting_agent
            or binding.purpose != query.purpose
            or binding.invocation_id != query.invocation_id
            or binding.privacy_epoch != query.privacy_epoch
            or binding.authorization_epoch != query.authorization_epoch
            or binding.retrieval_policy_epoch
            != query.retrieval_policy_epoch
        ):
            raise ValueError("item_handle_scope_mismatch")

    @staticmethod
    def _validate_cursor(
        cursor: InventoryCursorBinding,
        query: ResolvedMemoryQuery,
    ) -> None:
        if (
            cursor.subject_id != query.subject_id
            or cursor.actor_id != query.actor_id
            or cursor.purpose != query.purpose
            or cursor.privacy_epoch != query.privacy_epoch
            or cursor.authorization_epoch != query.authorization_epoch
            or cursor.retrieval_policy_epoch != query.retrieval_policy_epoch
        ):
            raise ValueError("inventory_cursor_scope_mismatch")

    def _query_for_handle(
        self,
        binding: HandleBinding,
        context: Any,
    ) -> ResolvedMemoryQuery:
        scope = context.fact_snapshot.source_scope
        return ResolvedMemoryQuery(
            query_id=f"source-resolution:{binding.handle}",
            episode_id=context.episode_id or "",
            plan_revision=context.plan_revision or 0,
            plan_step_id=context.plan_step_id,
            invocation_id=context.invocation_id,
            requesting_agent=binding.requesting_agent,
            purpose=binding.purpose,
            memory_types=(binding.item_kind,),
            selector_kind=MemorySelectorKind.ITEM_HANDLES,
            item_handles=(binding.handle,),
            source_scope=scope,
            as_of=scope.as_of,
            max_items=1,
            token_budget=context.memory_token_budget,
            actor_id=binding.actor_id,
            subject_id=binding.subject_id,
            actor_role=context.fact_snapshot.binding.role,
            authorization_scope=context.authorization_scope,
            fact_snapshot_id=context.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=context.fact_snapshot.fact_snapshot_hash,
            privacy_epoch=binding.privacy_epoch,
            authorization_epoch=binding.authorization_epoch,
            retrieval_policy_epoch=binding.retrieval_policy_epoch,
        )
