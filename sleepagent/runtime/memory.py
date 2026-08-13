from __future__ import annotations

# 仅保留模型输入/发布防线与 Episode 结果日志；不再提供记忆检索、归纳 worker 或 sidecar。
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock
from typing import Any, Callable, Iterable, Literal

from pydantic import Field, model_validator

from sleepagent.runtime.contracts import (
    AgentId,
    FrozenContract,
    MemoryChangeCandidate,
    SourceScopeKind,
    TrustLabel,
    stable_hash,
)


MEMORY_QUERY_POLICY_VERSION = "sleepagent-memory-query-policy.v2"
MEMORY_HANDLE_TTL_MINUTES = 15

MemoryType = Literal[
    "preference", "routine", "environment", "communication_preference"
]
MemoryValueSchema = Literal[
    "bounded_string.v1", "boolean.v1", "number.v1", "enum.v1"
]
MemoryActorRole = Literal["elder", "family", "doctor", "system"]


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _contract_hash(value: FrozenContract, excluded_field: str) -> str:
    return stable_hash(value.model_dump(mode="json", exclude={excluded_field}))


def _value_hash(
    concept_id: str,
    value_schema_id: MemoryValueSchema,
    value_schema_version: str,
    typed_value: Any,
) -> str:
    return stable_hash(
        {
            "concept_id": concept_id,
            "value_schema_id": value_schema_id,
            "value_schema_version": value_schema_version,
            "typed_value": typed_value,
        }
    )


def _validate_typed_value(
    value_schema_id: MemoryValueSchema,
    typed_value: Any,
) -> None:
    if value_schema_id == "bounded_string.v1" and not (
        type(typed_value) is str and 1 <= len(typed_value) <= 500
    ):
        raise ValueError("bounded_string.v1 requires 1..500 characters")
    if value_schema_id == "boolean.v1" and type(typed_value) is not bool:
        raise ValueError("boolean.v1 requires a strict boolean")
    if value_schema_id == "number.v1" and (
        type(typed_value) not in {int, float}
        or not math.isfinite(float(typed_value))
    ):
        raise ValueError("number.v1 requires a finite number")
    if value_schema_id == "enum.v1" and not (
        type(typed_value) is str
        and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", typed_value)
    ):
        raise ValueError("enum.v1 requires a normalized enum code")


class MemoryPurpose(str, Enum):
    PERSONAL_EVIDENCE_CONTEXT = "personal_evidence_context"
    EXPLICIT_MEMORY_REVIEW = "explicit_memory_review"
    EXPLICIT_MEMORY_CHANGE = "explicit_memory_change"
    EXPLICIT_MEMORY_FORGET = "explicit_memory_forget"


class MemoryItemStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"


class MemoryOperation(str, Enum):
    REMEMBER = "remember"
    CORRECT = "correct"
    EXPIRE = "expire"
    FORGET = "forget"


class SensitivityClass(str, Enum):
    PERSONAL = "personal"
    SENSITIVE_PERSONAL = "sensitive_personal"


class ProvenanceType(str, Enum):
    ELDER_CONFIRMED = "elder_confirmed"
    AUTHORIZED_OBSERVER = "authorized_observer"
    ACCEPTED_EVIDENCE = "accepted_evidence"


class LegacyMemoryItemV1(FrozenContract):
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
    """受治理的不可变 revision；它始终是不可信个体上下文而非 Evidence。"""

    schema_version: Literal["GovernedMemoryItem.v2"] = "GovernedMemoryItem.v2"
    memory_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    memory_type: MemoryType
    concept_id: str = Field(..., min_length=3, max_length=160)
    value_schema_id: MemoryValueSchema
    value_schema_version: str = "1"
    typed_value: Any
    value_hash: str | None = Field(default=None, min_length=64, max_length=64)
    trust_label: Literal[TrustLabel.USER_MEMORY_UNTRUSTED_DATA] = (
        TrustLabel.USER_MEMORY_UNTRUSTED_DATA
    )
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
    status: Literal[
        MemoryItemStatus.ACTIVE,
        MemoryItemStatus.EXPIRED,
        MemoryItemStatus.FORGOTTEN,
    ] = MemoryItemStatus.ACTIVE
    supersedes_ref: str | None = None
    conflict_refs: tuple[str, ...] = ()
    confirmation_ref: str = Field(..., min_length=1)
    retention_policy_version: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_value(self) -> "GovernedMemoryItemV2":
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,159}", self.concept_id):
            raise ValueError("concept_id must be normalized")
        terminal = self.status in {
            MemoryItemStatus.EXPIRED,
            MemoryItemStatus.FORGOTTEN,
        }
        if terminal and self.typed_value is not None:
            raise ValueError("expired/forgotten Memory revision must be a tombstone")
        if not terminal:
            _validate_typed_value(self.value_schema_id, self.typed_value)
        if not self.allowed_roles or not self.allowed_purposes:
            raise ValueError("governed Memory requires access ceilings")
        if len(set(self.allowed_roles)) != len(self.allowed_roles) or len(
            set(self.allowed_purposes)
        ) != len(self.allowed_purposes):
            raise ValueError("Memory access ceilings must be unique")
        if not set(self.allowed_roles).issubset(
            {AgentId.SLEEP_CARE, AgentId.EVIDENCE_REASONING}
        ):
            raise ValueError("Care and Safety cannot receive generic Memory")
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must follow valid_from")
        expected = _value_hash(
            self.concept_id,
            self.value_schema_id,
            self.value_schema_version,
            self.typed_value,
        )
        if self.value_hash is not None and self.value_hash != expected:
            raise ValueError("value_hash does not bind the governed value")
        if self.value_hash is None:
            object.__setattr__(self, "value_hash", expected)
        return self

    @property
    def revision_ref(self) -> str:
        return f"{self.memory_id}:v{self.version}"


MemoryItemRecord = LegacyMemoryItemV1 | GovernedMemoryItemV2


class MemoryQueryIntent(FrozenContract):
    """调用方只能声明精确概念和用途，不能请求全量 inventory。"""

    purpose: MemoryPurpose
    concept_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    source_scope_kind: SourceScopeKind
    max_items: int = Field(default=8, ge=1, le=8)
    token_budget: int = Field(default=1200, ge=64, le=4096)

    @model_validator(mode="after")
    def validate_concepts(self) -> "MemoryQueryIntent":
        if len(set(self.concept_ids)) != len(self.concept_ids):
            raise ValueError("Memory concept selectors must be unique")
        if any(
            value in {"*", "all"}
            or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,159}", value)
            for value in self.concept_ids
        ):
            raise ValueError("Memory query requires exact normalized concepts")
        if self.source_scope_kind == SourceScopeKind.GENERAL_KNOWLEDGE:
            raise ValueError("personal Memory cannot use general-knowledge scope")
        return self


class ResolvedMemoryQuery(MemoryQueryIntent):
    query_id: str = Field(..., pattern=r"^memory-query:[0-9a-f]{32}$")
    query_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    invocation_id: str = Field(..., min_length=1)
    actor_id: str = Field(..., min_length=1)
    actor_role: MemoryActorRole
    subject_id: str = Field(..., min_length=1)
    requesting_agent: AgentId
    authorization_scope: tuple[str, ...]
    as_of: datetime
    privacy_epoch: int = Field(..., ge=0)
    authorization_epoch: int = Field(..., ge=0)

    @model_validator(mode="after")
    def bind_query_hash(self) -> "ResolvedMemoryQuery":
        expected = _contract_hash(self, "query_hash")
        if self.query_hash is not None and self.query_hash != expected:
            raise ValueError("resolved Memory query hash mismatch")
        if self.query_hash is None:
            object.__setattr__(self, "query_hash", expected)
        return self


class MemorySliceItem(FrozenContract):
    revision_ref: str = Field(..., min_length=1)
    concept_id: str = Field(..., min_length=3, max_length=160)
    value_schema_id: MemoryValueSchema
    value_schema_version: str = Field(..., min_length=1)
    typed_value: Any
    value_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    provenance_type: ProvenanceType
    source_ref: str = Field(..., min_length=1)
    source_scope_kind: SourceScopeKind
    status: Literal[MemoryItemStatus.ACTIVE] = MemoryItemStatus.ACTIVE
    valid_from: datetime
    valid_until: datetime | None = None
    trust_label: Literal[TrustLabel.USER_MEMORY_UNTRUSTED_DATA] = (
        TrustLabel.USER_MEMORY_UNTRUSTED_DATA
    )
    verified_evidence: Literal[False] = False
    verified_medical_fact: Literal[False] = False


class MemoryHandle(FrozenContract):
    handle_id: str = Field(..., pattern=r"^mh_[0-9a-f]{48}$")
    subject_id: str = Field(..., min_length=1)
    actor_id: str = Field(..., min_length=1)
    requesting_agent: AgentId
    purpose: MemoryPurpose
    invocation_id: str = Field(..., min_length=1)
    query_id: str = Field(..., min_length=1)
    query_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    result_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    revision_ref: str = Field(..., min_length=1)
    privacy_epoch: int = Field(..., ge=0)
    authorization_epoch: int = Field(..., ge=0)
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_lifetime(self) -> "MemoryHandle":
        if self.expires_at <= self.created_at:
            raise ValueError("Memory handle must expire after creation")
        return self


class MemoryReadReceipt(FrozenContract):
    schema_version: Literal["MemoryReadReceipt.v2"] = "MemoryReadReceipt.v2"
    receipt_id: str = Field(..., pattern=r"^memory-read:[0-9a-f]{32}$")
    receipt_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    query_id: str = Field(..., min_length=1)
    query_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    invocation_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    requesting_agent: AgentId
    purpose: MemoryPurpose
    result_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    items: tuple[MemorySliceItem, ...]
    handles: tuple[MemoryHandle, ...]
    candidate_count: int = Field(..., ge=0)
    filter_reason_codes: tuple[str, ...]
    actual_tokens: int = Field(..., ge=0)
    privacy_epoch: int = Field(..., ge=0)
    authorization_epoch: int = Field(..., ge=0)
    completed_at: datetime

    @model_validator(mode="after")
    def bind_result_and_receipt(self) -> "MemoryReadReceipt":
        expected_result = stable_hash(
            {
                "query_hash": self.query_hash,
                "items": [item.model_dump(mode="json") for item in self.items],
            }
        )
        if self.result_hash != expected_result:
            raise ValueError("Memory result hash mismatch")
        if len(self.items) != len(self.handles) or any(
            handle.revision_ref != item.revision_ref
            or handle.result_hash != self.result_hash
            for item, handle in zip(self.items, self.handles)
        ):
            raise ValueError("Memory handles do not bind the result items")
        expected = _contract_hash(self, "receipt_hash")
        if self.receipt_hash is not None and self.receipt_hash != expected:
            raise ValueError("Memory read receipt hash mismatch")
        if self.receipt_hash is None:
            object.__setattr__(self, "receipt_hash", expected)
        return self


class MemoryChange(FrozenContract):
    """remember/correct/expire/forget 共用的候选；它不是授权事实。"""

    schema_version: Literal["MemoryChange.v1"] = "MemoryChange.v1"
    change_id: str = Field(..., pattern=r"^memory-change:[a-zA-Z0-9_.:-]+$")
    change_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    operation: MemoryOperation
    memory_id: str = Field(..., pattern=r"^memory:[a-zA-Z0-9_.:-]+$")
    subject_id: str = Field(..., min_length=1)
    expected_state_version: int = Field(..., ge=0)
    proposed_value: MemoryChangeCandidate | None = None
    causal_ref: str = Field(..., min_length=1)
    source_actor_id: str = Field(..., min_length=1)
    source_actor_role: MemoryActorRole
    source_scope_kind: SourceScopeKind
    target_revision_ref: str | None = None
    target_revision_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    confirmation_actor_id: str = Field(..., min_length=1)
    persistence_owner: Literal["longitudinal_memory"] = "longitudinal_memory"
    created_at: datetime
    confirmation_expires_at: datetime

    @model_validator(mode="after")
    def validate_change(self) -> "MemoryChange":
        if self.confirmation_expires_at <= self.created_at:
            raise ValueError("Memory confirmation window is empty")
        candidate = self.proposed_value
        if self.change_id.startswith("memory-change:habit") or self.causal_ref.startswith(
            ("habit-answer:", "habit-fact:", "habit-candidate:")
        ) or (candidate and candidate.source_ref.startswith(
            ("habit-answer:", "habit-fact:", "habit-candidate:")
        )):
            raise ValueError("Habit and Memory cannot share a persistence candidate")
        target_required = self.operation != MemoryOperation.REMEMBER
        if target_required != bool(self.target_revision_ref and self.target_revision_hash):
            raise ValueError("Memory operation has an invalid exact target binding")
        carries_value = self.operation in {
            MemoryOperation.REMEMBER,
            MemoryOperation.CORRECT,
        }
        if carries_value != (candidate is not None):
            raise ValueError("only remember/correct may carry a typed candidate")
        if candidate and (
            candidate.candidate_id != self.memory_id
            or candidate.subject_id != self.subject_id
            or candidate.operation
            != ("create" if self.operation == MemoryOperation.REMEMBER else "replace")
        ):
            raise ValueError("typed candidate does not bind the Memory change")
        provenance = candidate.provenance_type if candidate else None
        if provenance == ProvenanceType.ELDER_CONFIRMED.value and (
            self.source_actor_role != "elder"
            or self.source_actor_id != self.confirmation_actor_id
        ):
            raise ValueError("observer evidence cannot masquerade as elder-confirmed")
        if provenance == ProvenanceType.AUTHORIZED_OBSERVER.value and (
            self.source_actor_role not in {"family", "doctor"}
        ):
            raise ValueError("observer provenance requires an observer role")
        if provenance == ProvenanceType.ACCEPTED_EVIDENCE.value and (
            candidate is not None and not candidate.source_ref.startswith("evidence:")
        ):
            raise ValueError("accepted-evidence provenance requires an Evidence ref")
        expected = _contract_hash(self, "change_hash")
        if self.change_hash is not None and self.change_hash != expected:
            raise ValueError("Memory change hash mismatch")
        if self.change_hash is None:
            object.__setattr__(self, "change_hash", expected)
        return self


class MemoryConfirmation(FrozenContract):
    """独立 HITL 授权事实，只批准一个精确 Change hash。"""

    confirmation_id: str = Field(..., min_length=1)
    confirmation_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    actor_id: str = Field(..., min_length=1)
    actor_role: Literal["elder"] = "elder"
    subject_id: str = Field(..., min_length=1)
    target_change_id: str = Field(..., min_length=1)
    target_change_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    approved_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def bind_confirmation_hash(self) -> "MemoryConfirmation":
        if self.expires_at <= self.approved_at:
            raise ValueError("Memory confirmation must have a positive lifetime")
        expected = _contract_hash(self, "confirmation_hash")
        if self.confirmation_hash is not None and self.confirmation_hash != expected:
            raise ValueError("Memory confirmation hash mismatch")
        if self.confirmation_hash is None:
            object.__setattr__(self, "confirmation_hash", expected)
        return self


class GovernedMemoryState(FrozenContract):
    """内部 append-only ledger；模型只能看到下方安全投影函数的结果。"""

    subject_id: str = Field(..., min_length=1)
    version: int = Field(default=0, ge=0)
    revisions: tuple[MemoryItemRecord, ...] = ()

    @model_validator(mode="after")
    def validate_append_only_chain(self) -> "GovernedMemoryState":
        by_id: dict[str, list[GovernedMemoryItemV2]] = {}
        for item in self.revisions:
            if item.subject_id != self.subject_id:
                raise ValueError("Memory revision subject mismatch")
            if isinstance(item, GovernedMemoryItemV2):
                by_id.setdefault(item.memory_id, []).append(item)
        for revisions in by_id.values():
            ordered = sorted(revisions, key=lambda item: item.version)
            if [item.version for item in ordered] != list(
                range(1, len(ordered) + 1)
            ):
                raise ValueError("Memory revisions must be contiguous")
            for prior, current in zip(ordered, ordered[1:]):
                if current.supersedes_ref != prior.revision_ref:
                    raise ValueError("Memory replacement relation is broken")
                if current.concept_id != prior.concept_id:
                    raise ValueError("Memory lineage concept identity changed")
                if prior.status == MemoryItemStatus.FORGOTTEN:
                    raise ValueError("forgotten Memory cannot be resurrected")
        return self

    def audit_projection(self) -> tuple[dict[str, Any], ...]:
        """投影 effective lineage 状态；logical forget 后不暴露任何值派生字段。"""

        latest = _latest_governed(self)
        forgotten = {
            memory_id
            for memory_id, item in latest.items()
            if item.status == MemoryItemStatus.FORGOTTEN
        }
        records: list[dict[str, Any]] = []
        for item in self.revisions:
            if not isinstance(item, GovernedMemoryItemV2):
                continue
            current = latest[item.memory_id]
            effective_status = (
                item.status
                if item.revision_ref == current.revision_ref
                else MemoryItemStatus.SUPERSEDED
            )
            record: dict[str, Any] = {
                "revision_ref": item.revision_ref,
                "memory_id": item.memory_id,
                "concept_id": item.concept_id,
                "operation": (
                    MemoryOperation.FORGET.value
                    if item.status == MemoryItemStatus.FORGOTTEN
                    else MemoryOperation.EXPIRE.value
                    if item.status == MemoryItemStatus.EXPIRED
                    else MemoryOperation.REMEMBER.value
                    if item.version == 1
                    else MemoryOperation.CORRECT.value
                ),
                "raw_status": item.status.value,
                "effective_status": effective_status.value,
                "recorded_at": item.recorded_at,
                "supersedes_ref": item.supersedes_ref,
                "confirmation_ref": item.confirmation_ref,
            }
            if item.memory_id not in forgotten:
                record.update(
                    revision_hash=stable_hash(item),
                    value_hash=item.value_hash,
                    typed_value=item.typed_value,
                )
            records.append(record)
        return tuple(records)


class MemoryConflictError(ValueError):
    def __init__(self, concept_id: str, revision_refs: tuple[str, ...]) -> None:
        self.concept_id = concept_id
        self.revision_refs = revision_refs
        super().__init__(f"unresolved Memory conflict: {concept_id}")


def _latest_governed(
    state: GovernedMemoryState,
) -> dict[str, GovernedMemoryItemV2]:
    latest: dict[str, GovernedMemoryItemV2] = {}
    for item in state.revisions:
        if not isinstance(item, GovernedMemoryItemV2):
            continue
        prior = latest.get(item.memory_id)
        if prior is None or item.version > prior.version:
            latest[item.memory_id] = item
    return latest


CanonicalSourceResolver = Callable[[tuple[str, ...], Any], dict[str, Any] | None]


def canonical_token_count(value: Any) -> int:
    """提供稳定的预算近似；模型 provider 仍负责最终 token 限制。"""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return max(1, (len(encoded.encode("utf-8")) + 3) // 4)


def detect_explicit_memory_purpose(text: str) -> MemoryPurpose | None:
    normalized = text.lower()
    if any(term in normalized for term in ("forget", "忘记", "删除记忆")):
        return MemoryPurpose.EXPLICIT_MEMORY_FORGET
    if any(term in normalized for term in ("change memory", "修改记忆")):
        return MemoryPurpose.EXPLICIT_MEMORY_CHANGE
    if any(term in normalized for term in ("review memory", "查看记忆")):
        return MemoryPurpose.EXPLICIT_MEMORY_REVIEW
    return None


def canonical_result_hash(result: Any) -> str:
    payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    return stable_hash(payload)


def resolve_memory_query(
    intent: MemoryQueryIntent,
    *,
    invocation_id: str,
    actor_id: str,
    actor_role: MemoryActorRole,
    subject_id: str,
    requesting_agent: AgentId,
    authorization_scope: tuple[str, ...],
    as_of: datetime,
    privacy_epoch: int,
    authorization_epoch: int,
) -> ResolvedMemoryQuery:
    """把不可信 intent 绑定到已认证身份、invocation 和当前权限 epoch。"""

    _require_aware(as_of, "Memory query as_of")
    if not set(authorization_scope).intersection(
        {"personal_memory:read", "personal_memory:manage", "memory:read", "memory:manage"}
    ):
        raise PermissionError("Memory authorization scope is absent")
    if intent.purpose == MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT:
        if requesting_agent != AgentId.EVIDENCE_REASONING:
            raise PermissionError("only Evidence may request personal Memory context")
    elif requesting_agent != AgentId.SLEEP_CARE or actor_role != "elder":
        raise PermissionError("explicit Memory management requires the elder")
    values = {
        **intent.model_dump(),
        "invocation_id": invocation_id,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "subject_id": subject_id,
        "requesting_agent": requesting_agent,
        "authorization_scope": authorization_scope,
        "as_of": as_of,
        "privacy_epoch": privacy_epoch,
        "authorization_epoch": authorization_epoch,
    }
    return ResolvedMemoryQuery(
        query_id=f"memory-query:{stable_hash(values)[:32]}",
        **values,
    )


def project_current_memory(
    state: GovernedMemoryState,
    *,
    now: datetime,
) -> tuple[GovernedMemoryItemV2, ...]:
    """只投影每条 lineage 的当前 active V2；Legacy 与 tombstone 永不返回。"""

    _require_aware(now, "Memory projection time")
    return tuple(
        sorted(
            (
                item
                for item in _latest_governed(state).values()
                if item.status == MemoryItemStatus.ACTIVE
                and item.valid_from <= now
                and (item.valid_until is None or now < item.valid_until)
            ),
            key=lambda item: (item.concept_id, item.memory_id, -item.version),
        )
    )


def select_memory_slice(
    query: ResolvedMemoryQuery,
    state: GovernedMemoryState,
    *,
    now: datetime,
) -> MemoryReadReceipt:
    """执行 exact subject/role/purpose/scope 过滤，再确定性应用预算。"""

    _require_aware(now, "Memory read time")
    if query.subject_id != state.subject_id:
        raise PermissionError("Memory query subject mismatch")
    if query.query_hash != _contract_hash(query, "query_hash"):
        raise ValueError("resolved Memory query is forged")
    reasons: set[str] = set()
    if any(isinstance(item, LegacyMemoryItemV1) for item in state.revisions):
        reasons.add("legacy_v1_excluded")
    current = project_current_memory(state, now=now)
    governed_count = sum(isinstance(item, GovernedMemoryItemV2) for item in state.revisions)
    if len(current) < governed_count:
        reasons.add("inactive_or_expired_excluded")
    visible = list(current)
    filters = (
        ("role_excluded", lambda item: query.requesting_agent in item.allowed_roles),
        ("purpose_excluded", lambda item: query.purpose in item.allowed_purposes),
        ("source_scope_excluded", lambda item: query.source_scope_kind == item.source_scope_kind),
        ("concept_excluded", lambda item: item.concept_id in query.concept_ids),
    )
    for reason, allowed in filters:
        if any(not allowed(item) for item in visible):
            reasons.add(reason)
        visible = [item for item in visible if allowed(item)]
    for concept_id in sorted({item.concept_id for item in visible}):
        group = [item for item in visible if item.concept_id == concept_id]
        if len({item.value_hash for item in group}) > 1 or any(
            item.conflict_refs for item in group
        ):
            raise MemoryConflictError(
                concept_id, tuple(sorted(item.revision_ref for item in group))
            )
    selected: list[MemorySliceItem] = []
    seen_values: set[tuple[str, str | None]] = set()
    for item in visible:
        identity = (item.concept_id, item.value_hash)
        if identity in seen_values:
            reasons.add("duplicate_value_excluded")
            continue
        projected = MemorySliceItem(
            revision_ref=item.revision_ref,
            concept_id=item.concept_id,
            value_schema_id=item.value_schema_id,
            value_schema_version=item.value_schema_version,
            typed_value=item.typed_value,
            value_hash=str(item.value_hash),
            provenance_type=item.provenance_type,
            source_ref=item.source_ref,
            source_scope_kind=item.source_scope_kind,
            valid_from=item.valid_from,
            valid_until=item.valid_until,
        )
        tentative = [*selected, projected]
        if len(tentative) > query.max_items or canonical_token_count(
            [value.model_dump(mode="json") for value in tentative]
        ) > query.token_budget:
            reasons.add("deterministic_budget_exhausted")
            continue
        selected.append(projected)
        seen_values.add(identity)
    result_hash = stable_hash(
        {"query_hash": query.query_hash, "items": [item.model_dump(mode="json") for item in selected]}
    )
    handles = tuple(
        MemoryHandle(
            handle_id="mh_" + stable_hash((result_hash, item.revision_ref))[:48],
            subject_id=query.subject_id,
            actor_id=query.actor_id,
            requesting_agent=query.requesting_agent,
            purpose=query.purpose,
            invocation_id=query.invocation_id,
            query_id=query.query_id,
            query_hash=str(query.query_hash),
            result_hash=result_hash,
            revision_ref=item.revision_ref,
            privacy_epoch=query.privacy_epoch,
            authorization_epoch=query.authorization_epoch,
            created_at=now,
            expires_at=min(
                now + timedelta(minutes=MEMORY_HANDLE_TTL_MINUTES),
                item.valid_until or now + timedelta(minutes=MEMORY_HANDLE_TTL_MINUTES),
            ),
        )
        for item in selected
    )
    return MemoryReadReceipt(
        receipt_id="memory-read:" + stable_hash((query.query_id, result_hash, now))[:32],
        query_id=query.query_id,
        query_hash=str(query.query_hash),
        invocation_id=query.invocation_id,
        subject_id=query.subject_id,
        requesting_agent=query.requesting_agent,
        purpose=query.purpose,
        result_hash=result_hash,
        items=tuple(selected),
        handles=handles,
        candidate_count=len(visible),
        filter_reason_codes=tuple(sorted(reasons)),
        actual_tokens=canonical_token_count(
            [item.model_dump(mode="json") for item in selected]
        ),
        privacy_epoch=query.privacy_epoch,
        authorization_epoch=query.authorization_epoch,
        completed_at=now,
    )


def validate_memory_handle(
    handle: MemoryHandle,
    *,
    receipt: MemoryReadReceipt,
    query: ResolvedMemoryQuery,
    state: GovernedMemoryState,
    privacy_epoch: int,
    authorization_epoch: int,
    now: datetime,
) -> MemorySliceItem:
    """epoch、invocation、query、result 或 current revision 任一改变即失效。"""

    _require_aware(now, "Memory handle validation time")
    result_hash = stable_hash(
        {"query_hash": receipt.query_hash, "items": [item.model_dump(mode="json") for item in receipt.items]}
    )
    if receipt.receipt_hash != _contract_hash(receipt, "receipt_hash") or (
        receipt.result_hash != result_hash
    ):
        raise ValueError("Memory read receipt is forged")
    if handle not in receipt.handles or any(
        (
            handle.query_id != query.query_id,
            handle.query_hash != query.query_hash,
            handle.result_hash != receipt.result_hash,
            handle.invocation_id != query.invocation_id,
            handle.subject_id != query.subject_id,
            handle.actor_id != query.actor_id,
            handle.requesting_agent != query.requesting_agent,
            handle.purpose != query.purpose,
            handle.privacy_epoch != privacy_epoch,
            handle.authorization_epoch != authorization_epoch,
            handle.expires_at <= now,
        )
    ):
        raise PermissionError("Memory handle binding changed")
    current = {item.revision_ref: item for item in project_current_memory(state, now=now)}
    bound = current.get(handle.revision_ref)
    if bound is None:
        raise ValueError("Memory handle no longer references a current revision")
    return next(
        item
        for item in receipt.items
        if item.revision_ref == bound.revision_ref and item.value_hash == bound.value_hash
    )


def apply_memory_change(
    state: GovernedMemoryState,
    change: MemoryChange,
    confirmation: MemoryConfirmation,
    *,
    now: datetime,
) -> GovernedMemoryState:
    """确认后追加 revision；forget 仅为 logical suppression，不代表物理擦除。"""

    _require_aware(now, "Memory change time")
    if change.change_hash != _contract_hash(change, "change_hash") or (
        confirmation.confirmation_hash
        != _contract_hash(confirmation, "confirmation_hash")
    ):
        raise ValueError("Memory change or confirmation is forged")
    if (state.subject_id, state.version) != (
        change.subject_id, change.expected_state_version
    ):
        raise ValueError("Memory subject/version is stale")
    if (
        confirmation.subject_id != change.subject_id
        or confirmation.actor_id != change.confirmation_actor_id
        or confirmation.target_change_id != change.change_id
        or confirmation.target_change_hash != change.change_hash
        or confirmation.expires_at != change.confirmation_expires_at
        or not change.created_at <= confirmation.approved_at <= now
        or now >= confirmation.expires_at
    ):
        raise PermissionError("Memory confirmation is not exactly bound")
    prior = _latest_governed(state).get(change.memory_id)
    if change.operation == MemoryOperation.REMEMBER:
        if prior is not None:
            raise ValueError("Memory lineage already exists")
    elif prior is None or prior.status != MemoryItemStatus.ACTIVE:
        raise ValueError("Memory target is not current")
    elif change.target_revision_ref != prior.revision_ref or (
        change.target_revision_hash != stable_hash(prior)
    ):
        raise ValueError("Memory exact target binding changed")
    candidate = change.proposed_value
    active = change.operation in {MemoryOperation.REMEMBER, MemoryOperation.CORRECT}
    source = candidate if active else prior
    if source is None:
        raise ValueError("Memory change lacks a source value")
    revision = GovernedMemoryItemV2(
        memory_id=change.memory_id,
        subject_id=change.subject_id,
        memory_type=source.memory_type,
        concept_id=source.concept_id,
        value_schema_id=source.value_schema_id,
        value_schema_version=source.value_schema_version,
        typed_value=source.typed_value if active else None,
        provenance_type=ProvenanceType(source.provenance_type),
        source_ref=source.source_ref,
        source_scope_kind=(change.source_scope_kind if active else source.source_scope_kind),
        version=1 if prior is None else prior.version + 1,
        recorded_at=now,
        valid_from=now,
        valid_until=source.valid_until if active else None,
        sensitivity_class=SensitivityClass(source.sensitivity_class),
        allowed_roles=source.allowed_roles,
        allowed_purposes=tuple(MemoryPurpose(value) for value in source.allowed_purposes),
        status={
            MemoryOperation.REMEMBER: MemoryItemStatus.ACTIVE,
            MemoryOperation.CORRECT: MemoryItemStatus.ACTIVE,
            MemoryOperation.EXPIRE: MemoryItemStatus.EXPIRED,
            MemoryOperation.FORGET: MemoryItemStatus.FORGOTTEN,
        }[change.operation],
        supersedes_ref=prior.revision_ref if prior else None,
        confirmation_ref=confirmation.confirmation_id,
        retention_policy_version=source.retention_policy_version,
    )
    return GovernedMemoryState(
        subject_id=state.subject_id,
        version=state.version + 1,
        revisions=(*state.revisions, revision),
    )


@dataclass(frozen=True, slots=True)
class PublicationJournalEntry:
    intent_id: str
    episode_id: str
    command_hash: str
    draft_hash: str
    state: Literal["reserved", "delivered", "failed"] = "reserved"
    delivered: bool | None = None


class InMemoryLongitudinalResultStore:
    """测试/replay 的最小结果日志；durable worker authority 始终是 PostgreSQL。"""

    def __init__(self) -> None:
        self.lock = RLock()
        self._results: dict[str, list[Any]] = {}
        self._publications: dict[str, PublicationJournalEntry] = {}

    def append_nonterminal(self, result: Any, *, subject_id: str) -> None:
        del subject_id
        with self.lock:
            self._results.setdefault(result.receipt.episode_id, []).append(result)

    def append_terminal_bundle(
        self, result: Any, *, subject_id: str, now: datetime | None = None
    ) -> Any:
        del subject_id, now
        with self.lock:
            self._results.setdefault(result.receipt.episode_id, []).append(result)
        return result

    def latest(self, episode_id: str) -> Any:
        with self.lock:
            return self._results[episode_id][-1]

    def history(self, episode_id: str) -> list[Any]:
        with self.lock:
            return list(self._results.get(episode_id, ()))

    def reserve_publication(
        self,
        *,
        command_hash: str,
        episode_id: str,
        draft_hash: str,
        now: datetime | None = None,
    ) -> tuple[PublicationJournalEntry, bool]:
        del now
        intent_id = f"publication:{command_hash}"
        with self.lock:
            existing = self._publications.get(command_hash)
            if existing is not None:
                return existing, False
            entry = PublicationJournalEntry(
                intent_id=intent_id,
                episode_id=episode_id,
                command_hash=command_hash,
                draft_hash=draft_hash,
            )
            self._publications[command_hash] = entry
            return entry, True

    def complete_publication(
        self,
        *,
        intent_id: str,
        delivered: bool,
        now: datetime | None = None,
    ) -> PublicationJournalEntry:
        del now
        with self.lock:
            command_hash = next(
                key
                for key, value in self._publications.items()
                if value.intent_id == intent_id
            )
            updated = replace(
                self._publications[command_hash],
                state="delivered" if delivered else "failed",
                delivered=delivered,
            )
            self._publications[command_hash] = updated
            return updated


class LongitudinalMemoryService:
    """兼容模型输入与发布防线；memory tools 已从 registry 移除。"""

    def __init__(
        self,
        *,
        memory_store: Any,
        repository: Any,
        source_resolvers: dict[str, CanonicalSourceResolver] | None = None,
    ) -> None:
        self.memory_store = memory_store
        self.repository = repository
        self.source_resolvers = dict(source_resolvers or {})

    def validate_model_input(self, receipt_output: dict[str, Any], context: Any) -> None:
        del context
        if receipt_output.get("items"):
            raise ValueError("longitudinal memory input is no longer executable")

    def validate_prepublication(
        self,
        receipt_outputs: tuple[dict[str, Any], ...],
        *,
        subject_id: str,
        actor_id: str,
    ) -> None:
        del subject_id, actor_id
        if any(output.get("items") for output in receipt_outputs):
            raise ValueError("longitudinal memory publication is no longer executable")


__all__ = [
    "CanonicalSourceResolver",
    "GovernedMemoryItemV2",
    "InMemoryLongitudinalResultStore",
    "LegacyMemoryItemV1",
    "LongitudinalMemoryService",
    "MemoryItemRecord",
    "MemoryItemStatus",
    "MemoryPurpose",
    "ProvenanceType",
    "SensitivityClass",
    "canonical_result_hash",
    "canonical_token_count",
    "detect_explicit_memory_purpose",
]
