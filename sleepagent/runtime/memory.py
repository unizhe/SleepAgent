from __future__ import annotations

# 仅保留模型输入/发布防线与 Episode 结果日志；不再提供记忆检索、归纳 worker 或 sidecar。
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any, Callable, Literal

from pydantic import Field, model_validator

from sleepagent.runtime.contracts import (
    AgentId,
    FrozenContract,
    SourceScopeKind,
    stable_hash,
)


USER_MEMORY_TRUST_LABEL: Literal["USER_MEMORY_UNTRUSTED_DATA"] = (
    "USER_MEMORY_UNTRUSTED_DATA"
)


class MemoryPurpose(str, Enum):
    PERSONAL_EVIDENCE_CONTEXT = "personal_evidence_context"
    EXPLICIT_MEMORY_REVIEW = "explicit_memory_review"
    EXPLICIT_MEMORY_CHANGE = "explicit_memory_change"
    EXPLICIT_MEMORY_FORGET = "explicit_memory_forget"


class MemoryItemStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"
    FORGOTTEN = "forgotten"


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
    """历史 durable DTO；只供已有状态读取，不提供新的 memory execution。"""

    schema_version: Literal["GovernedMemoryItem.v2"] = "GovernedMemoryItem.v2"
    memory_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    memory_type: Literal[
        "preference", "routine", "environment", "communication_preference"
    ]
    concept_id: str = Field(..., min_length=3, max_length=160)
    value_schema_id: Literal[
        "bounded_string.v1", "boolean.v1", "number.v1", "enum.v1"
    ]
    value_schema_version: str = "1"
    typed_value: Any
    value_hash: str | None = Field(default=None, min_length=64, max_length=64)
    trust_label: Literal["USER_MEMORY_UNTRUSTED_DATA"] = USER_MEMORY_TRUST_LABEL
    provenance_type: ProvenanceType
    source_ref: str
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
    confirmation_ref: str
    retention_policy_version: str

    @model_validator(mode="after")
    def validate_value(self) -> "GovernedMemoryItemV2":
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,159}", self.concept_id):
            raise ValueError("concept_id must be normalized")
        if self.value_schema_id == "bounded_string.v1" and not (
            type(self.typed_value) is str and 1 <= len(self.typed_value) <= 500
        ):
            raise ValueError("bounded_string.v1 requires 1..500 characters")
        if self.value_schema_id == "boolean.v1" and type(self.typed_value) is not bool:
            raise ValueError("boolean.v1 requires a strict boolean")
        if self.value_schema_id == "number.v1" and (
            type(self.typed_value) not in {int, float}
            or not math.isfinite(float(self.typed_value))
        ):
            raise ValueError("number.v1 requires a finite number")
        expected = stable_hash(
            {
                "concept_id": self.concept_id,
                "value_schema_id": self.value_schema_id,
                "value_schema_version": self.value_schema_version,
                "typed_value": self.typed_value,
            }
        )
        if self.value_hash is not None and self.value_hash != expected:
            raise ValueError("value_hash does not bind the governed value")
        if self.value_hash is None:
            object.__setattr__(self, "value_hash", expected)
        return self


MemoryItemRecord = LegacyMemoryItemV1 | GovernedMemoryItemV2
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
