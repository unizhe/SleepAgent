# 本模块集中定义当前 Product Agent 运行链共享的严格数据契约。
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime
from enum import Enum, unique
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator


PRODUCT_AGENT_CONTRACT_VERSION = "sleepagent-product-agent.v15"


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def require_aware_datetimes(self) -> "StrictContract":
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, datetime) and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError(f"{name} must be timezone-aware")
        return self


class FrozenContract(StrictContract):
    model_config = ConfigDict(extra="forbid", frozen=True)


@unique
class AgentId(str, Enum):
    """The complete production model-agent roster.

    Skills, tools, services and runtime stages deliberately do not appear here.
    """

    SLEEP_CARE = "sleep_care"
    EVIDENCE_REASONING = "evidence_reasoning"
    CARE_STRATEGY = "care_strategy"
    SAFETY_REVIEW = "safety_review"


# Adding, removing, renaming or aliasing an Agent is not a supported extension
# point. Skills, tools, services, policies and runtime stages remain outside it.
PRODUCT_AGENT_ROSTER: Final[tuple[AgentId, ...]] = (
    AgentId.SLEEP_CARE,
    AgentId.EVIDENCE_REASONING,
    AgentId.CARE_STRATEGY,
    AgentId.SAFETY_REVIEW,
)


class WorkProductKind(str, Enum):
    SLEEPCARE_PLAN = "sleepcare_plan"
    EVIDENCE_PACKET = "evidence_packet"
    CARE_STRATEGY = "care_strategy"
    SAFETY_DECISION = "safety_decision"
    COMMUNICATION = "communication"


class EpisodeType(str, Enum):
    MORNING_REVIEW = "morning_review"
    TREND_REVIEW = "trend_review"
    GROUNDED_DIALOGUE = "grounded_dialogue"
    CARE_PLAN = "care_plan"
    CARE_FOLLOWUP = "care_followup"
    DATA_QUALITY_RECOVERY = "data_quality_recovery"
    ROLE_MATERIAL = "role_material"
    URGENT_BOUNDARY = "urgent_boundary"


class SourceScopeKind(str, Enum):
    CURRENT_NIGHT = "current_night"
    SEVEN_DAY = "7_day"
    THIRTY_DAY = "30_day"
    HISTORICAL_RANGE = "historical_range"
    GENERAL_KNOWLEDGE = "general_knowledge"


class EpisodeStatus(str, Enum):
    COMPLETE = "complete"
    WAITING_USER = "waiting_user"
    WAITING_CONFIRMATION = "waiting_confirmation"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class ExecutionMode(str, Enum):
    INTELLIGENT = "intelligent"
    SAFE_DEGRADED = "safe_degraded"
    DETERMINISTIC_ONLY = "deterministic_only"


class WorkProductStatus(str, Enum):
    COMPLETED = "completed"
    NEEDS_INPUT = "needs_input"
    REVISE = "revise"
    BLOCKED = "blocked"


class EvidenceSemantic(str, Enum):
    OBSERVED_FACT = "observed_fact"
    USER_REPORTED = "user_reported"
    OBSERVER_REPORTED = "observer_reported"
    GROUNDED_KNOWLEDGE = "grounded_knowledge"
    INFERENCE = "inference"
    UNKNOWN = "unknown"


class EvidenceSourceKind(str, Enum):
    CANONICAL_OBSERVATION = "canonical_observation"
    USER_REPORT = "user_report"
    AUTHORIZED_OBSERVER_REPORT = "authorized_observer_report"
    REVIEWED_KNOWLEDGE = "reviewed_knowledge"
    TREND_TOOL = "trend_tool"
    CONFIRMED_MEMORY = "confirmed_memory"
    CONFIRMED_HABIT = "confirmed_habit"
    ACCEPTED_LEDGER = "accepted_ledger"
    DATA_QUALITY = "data_quality"


class SafetyVerdict(str, Enum):
    APPROVE = "approve"
    REVISE = "revise"
    BLOCK = "block"


class ToolEffect(str, Enum):
    READ_ONLY = "read_only"
    STATE_WRITE = "state_write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"


class InvocationOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    UNKNOWN = "unknown"


class TrustLabel(str, Enum):
    SYSTEM_POLICY = "system_policy"
    AUTHENTICATED_BINDING = "authenticated_binding"
    CANONICAL_FACT = "canonical_fact"
    ACCEPTED_WORK_PRODUCT = "accepted_work_product"
    CONFIRMED_MEMORY = "confirmed_memory"
    CONFIRMED_HABIT = "confirmed_habit"
    USER_DATA = "user_data"
    USER_TEXT_UNTRUSTED = "user_text_untrusted"
    TOOL_OUTPUT_UNTRUSTED = "tool_output_untrusted"
    RETRIEVED_KNOWLEDGE_UNTRUSTED = "retrieved_knowledge_untrusted"
    EPISODIC_HINT_UNTRUSTED = "episodic_hint_untrusted"
    USER_MEMORY_UNTRUSTED_DATA = "user_memory_untrusted_data"


class CrossAgentRequestType(str, Enum):
    EVIDENCE = "evidence"
    CARE = "care"
    SAFETY_REVIEW = "safety_review"
    REVISION = "revision"
    USER_FACT = "user_fact"
    CONFIRMATION = "confirmation"


class SourceScope(FrozenContract):
    kind: SourceScopeKind
    as_of: datetime
    timezone_name: str = Field(..., min_length=1)
    date_start: date | None = None
    date_end: date | None = None
    valid_night_count: int = Field(default=0, ge=0)
    source_page_hint: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "SourceScope":
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone_name must be an IANA timezone") from exc
        if self.kind == SourceScopeKind.GENERAL_KNOWLEDGE:
            if self.date_start or self.date_end or self.valid_night_count:
                raise ValueError("general knowledge cannot claim a personal range")
            return self
        if self.date_start is None or self.date_end is None:
            raise ValueError("personal SourceScope requires date_start/date_end")
        if self.date_start > self.date_end:
            raise ValueError("date_start cannot follow date_end")
        local_as_of = self.as_of.astimezone(ZoneInfo(self.timezone_name)).date()
        if self.date_end > local_as_of:
            raise ValueError("personal SourceScope cannot include future dates")
        days = (self.date_end - self.date_start).days + 1
        required = {
            SourceScopeKind.CURRENT_NIGHT: 1,
            SourceScopeKind.SEVEN_DAY: 7,
            SourceScopeKind.THIRTY_DAY: 30,
        }.get(self.kind)
        if required is not None and days != required:
            raise ValueError(f"{self.kind.value} requires exactly {required} days")
        if self.valid_night_count > days:
            raise ValueError("valid_night_count exceeds SourceScope range")
        return self


class AuthenticatedBinding(FrozenContract):
    actor_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    role: Literal["elder", "family", "doctor", "system"]
    authorization_scope: tuple[str, ...] = ()


class FactSnapshot(FrozenContract):
    fact_snapshot_id: str = Field(..., min_length=1)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    binding: AuthenticatedBinding
    source_scope: SourceScope
    canonical_data_version: str = Field(..., min_length=1)
    entry_ledger_version: int = Field(default=0, ge=0)
    care_context_version: int = Field(default=0, ge=0)
    memory_context_version: int = Field(default=0, ge=0)
    habit_profile_version: int = Field(default=0, ge=0)
    habit_profile_hash: str | None = Field(
        default=None, min_length=64, max_length=64
    )
    memory_read_receipt_refs: tuple[str, ...] = ()
    memory_read_receipt_hashes: tuple[str, ...] = ()
    active_constraint_codes: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    readiness_decision_refs: tuple[str, ...] = ()
    readiness_decision_hashes: tuple[str, ...] = ()
    capability_eligibility_refs: tuple[str, ...] = ()
    capability_eligibility_hashes: tuple[str, ...] = ()
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        fact_snapshot_id: str,
        binding: AuthenticatedBinding,
        source_scope: SourceScope,
        canonical_data_version: str,
        entry_ledger_version: int = 0,
        care_context_version: int = 0,
        memory_context_version: int = 0,
        habit_profile_version: int = 0,
        habit_profile_hash: str | None = None,
        memory_read_receipt_refs: tuple[str, ...] = (),
        memory_read_receipt_hashes: tuple[str, ...] = (),
        active_constraint_codes: tuple[str, ...] = (),
        source_refs: tuple[str, ...] = (),
        readiness_decision_refs: tuple[str, ...] = (),
        readiness_decision_hashes: tuple[str, ...] = (),
        capability_eligibility_refs: tuple[str, ...] = (),
        capability_eligibility_hashes: tuple[str, ...] = (),
        created_at: datetime,
    ) -> "FactSnapshot":
        material = {
            "fact_snapshot_id": fact_snapshot_id,
            "binding": binding,
            "source_scope": source_scope,
            "canonical_data_version": canonical_data_version,
            "entry_ledger_version": entry_ledger_version,
            "care_context_version": care_context_version,
            "memory_context_version": memory_context_version,
            "habit_profile_version": habit_profile_version,
            "habit_profile_hash": habit_profile_hash,
            "memory_read_receipt_refs": memory_read_receipt_refs,
            "memory_read_receipt_hashes": memory_read_receipt_hashes,
            "active_constraint_codes": active_constraint_codes,
            "source_refs": source_refs,
            "readiness_decision_refs": readiness_decision_refs,
            "readiness_decision_hashes": readiness_decision_hashes,
            "capability_eligibility_refs": capability_eligibility_refs,
            "capability_eligibility_hashes": capability_eligibility_hashes,
            "created_at": created_at,
        }
        return cls.model_validate(
            {"fact_snapshot_hash": stable_hash(material), **material}
        )

    @model_validator(mode="after")
    def validate_cold_start_bindings(self) -> "FactSnapshot":
        if (self.habit_profile_version == 0) != (self.habit_profile_hash is None):
            raise ValueError("Habit profile version/hash binding is incomplete")
        if len(self.memory_read_receipt_refs) != len(
            self.memory_read_receipt_hashes
        ):
            raise ValueError("Memory receipt refs and hashes must align")
        if len(set(self.memory_read_receipt_refs)) != len(
            self.memory_read_receipt_refs
        ):
            raise ValueError("Memory receipt refs must be unique")
        if any(
            not ref.startswith("memory-read:")
            for ref in self.memory_read_receipt_refs
        ) or any(len(value) != 64 for value in self.memory_read_receipt_hashes):
            raise ValueError("Memory receipt binding is invalid")
        pairs = (
            (
                self.readiness_decision_refs,
                self.readiness_decision_hashes,
                "readiness decision",
                "cold-start-decision:",
            ),
            (
                self.capability_eligibility_refs,
                self.capability_eligibility_hashes,
                "capability eligibility",
                "capability-eligibility:",
            ),
        )
        for refs, hashes, label, prefix in pairs:
            if len(refs) != len(hashes):
                raise ValueError(f"{label} refs and hashes must align")
            if len(refs) != len(set(refs)):
                raise ValueError(f"{label} refs must be unique")
            if any(not ref.startswith(prefix) for ref in refs):
                raise ValueError(f"{label} ref has an invalid namespace")
            if any(len(value) != 64 for value in hashes):
                raise ValueError(f"{label} hash must be sha256")
            if any(not ref.endswith(value) for ref, value in zip(refs, hashes)):
                raise ValueError(f"{label} ref/hash mismatch")
        return self


class TrustedContextItem(FrozenContract):
    key: str = Field(..., min_length=1)
    trust_label: TrustLabel
    value: Any
    source_refs: tuple[str, ...] = ()


class ContextPacket(FrozenContract):
    context_packet_id: str = Field(..., min_length=1)
    episode_id: str = Field(..., min_length=1)
    invocation_id: str = Field(..., min_length=1)
    agent_id: AgentId
    objective: str = Field(..., min_length=1, max_length=1200)
    fact_snapshot_id: str = Field(..., min_length=1)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    episode_state_revision: int = Field(default=0, ge=0)
    care_context_version: int = Field(default=0, ge=0)
    source_scope: SourceScope
    authorization_scope: tuple[str, ...] = ()
    items: tuple[TrustedContextItem, ...] = ()

    @model_validator(mode="after")
    def reject_non_minimal_identity(self) -> "ContextPacket":
        forbidden = {"phone", "email", "full_name", "national_id"}
        if any(item.key in forbidden for item in self.items):
            raise ValueError("ContextPacket contains non-minimal identifiers")
        return self


_PROVIDER_PRIVATE_CONTEXT_KEYS = frozenset(
    {
        "actor_id",
        "assessment_id",
        "canonical_observation_id",
        "current_risk_id",
        "device_binding_id",
        "device_id",
        "fact_snapshot_id",
        "home_id",
        "night_episode_id",
        "night_episode_revision_id",
        "provider_account_id",
        "radar_device_id",
        "subject_id",
        "tool_invocation_id",
    }
)


def provider_context_projection(context: ContextPacket) -> dict[str, Any]:
    """Project a local ContextPacket into its identifier-free model DTO."""

    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): sanitize(item)
                for key, item in value.items()
                if str(key).lower() not in _PROVIDER_PRIVATE_CONTEXT_KEYS
            }
        if isinstance(value, (list, tuple)):
            return [sanitize(item) for item in value]
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return value

    scope = context.source_scope
    return {
        "agent_id": context.agent_id.value,
        "objective": context.objective,
        # This content hash binds Safety output to the exact local snapshot
        # without disclosing the snapshot row identity or subject binding.
        "fact_snapshot_hash": context.fact_snapshot_hash,
        "episode_state_revision": context.episode_state_revision,
        "care_context_version": context.care_context_version,
        "source_scope": {
            "kind": scope.kind.value,
            "as_of": scope.as_of.isoformat(),
            "timezone_name": scope.timezone_name,
            "date_start": (
                None if scope.date_start is None else scope.date_start.isoformat()
            ),
            "date_end": (
                None if scope.date_end is None else scope.date_end.isoformat()
            ),
            "valid_night_count": scope.valid_night_count,
        },
        "authorization_scope": list(context.authorization_scope),
        "items": [
            {
                "key": item.key,
                "trust_label": item.trust_label.value,
                "value": sanitize(item.value),
                "source_refs": list(item.source_refs),
            }
            for item in context.items
        ],
    }


class ToolRequest(StrictContract):
    request_id: str = Field(..., min_length=1)
    tool_name: str = Field(..., min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class CrossAgentRequest(StrictContract):
    request_id: str = Field(..., min_length=1)
    episode_id: str | None = Field(default=None, min_length=1)
    sender: AgentId
    receiver: AgentId
    request_type: CrossAgentRequestType
    objective: str = Field(..., min_length=1, max_length=800)
    fact_snapshot_id: str | None = Field(default=None, min_length=1)
    fact_snapshot_hash: str | None = Field(
        default=None, min_length=64, max_length=64
    )
    episode_state_revision: int | None = Field(default=None, ge=0)
    source_scope: SourceScope
    input_refs: list[str] = Field(default_factory=list, max_length=30)
    target_type: str | None = None
    target_id: str | None = None
    target_hash: str | None = Field(default=None, min_length=64, max_length=64)
    skill_id: str | None = None
    skill_version: str | None = None
    profile_version: str | None = None
    schema_version: str | None = None
    policy_version: str | None = None
    parent_invocation_id: str | None = None
    expires_at: datetime | None = None
    reason_codes: list[str] = Field(default_factory=list, max_length=20)


class EvidenceClaim(StrictContract):
    claim_id: str = Field(..., min_length=1)
    semantic: EvidenceSemantic
    statement: str = Field(..., min_length=1, max_length=1600)
    source_kind: EvidenceSourceKind
    evidence_refs: list[str] = Field(default_factory=list, max_length=30)
    confidence: float = Field(default=0, ge=0, le=1)
    alternative_explanations: list[str] = Field(default_factory=list, max_length=8)
    date_start: date | None = None
    date_end: date | None = None
    claim_strength: Literal[
        "general_knowledge",
        "single_night_description",
        "short_series_difference",
        "provisional_pattern",
        "established_baseline",
    ] | None = None
    metric_id: str | None = None
    measurement_cohort_ref: str | None = None
    readiness_decision_ref: str | None = None

    @model_validator(mode="after")
    def require_grounding(self) -> "EvidenceClaim":
        if self.semantic != EvidenceSemantic.UNKNOWN and not self.evidence_refs:
            raise ValueError("every non-unknown Evidence claim requires evidence_refs")
        if (
            self.semantic == EvidenceSemantic.INFERENCE
            and not self.alternative_explanations
        ):
            raise ValueError("inference requires an alternative explanation")
        if (
            self.semantic == EvidenceSemantic.OBSERVER_REPORTED
            and self.source_kind
            != EvidenceSourceKind.AUTHORIZED_OBSERVER_REPORT
        ):
            raise ValueError(
                "observer_reported requires authorized_observer_report source"
            )
        if (
            self.source_kind
            == EvidenceSourceKind.AUTHORIZED_OBSERVER_REPORT
            and self.semantic != EvidenceSemantic.OBSERVER_REPORTED
        ):
            raise ValueError(
                "authorized observer source cannot become another Evidence semantic"
            )
        cold_start_fields = (
            self.claim_strength,
            self.metric_id,
            self.measurement_cohort_ref,
            self.readiness_decision_ref,
        )
        if self.claim_strength == "general_knowledge":
            if any(cold_start_fields[1:]):
                raise ValueError(
                    "general knowledge cannot bind a personal readiness decision"
                )
            if self.semantic != EvidenceSemantic.GROUNDED_KNOWLEDGE:
                raise ValueError(
                    "general_knowledge strength requires grounded knowledge"
                )
        elif any(cold_start_fields) and not all(cold_start_fields):
            raise ValueError(
                "personal cold-start claim requires strength, metric, cohort, "
                "and readiness decision"
            )
        return self


class EvidencePacket(StrictContract):
    packet_id: str = Field(..., min_length=1)
    claims: list[EvidenceClaim] = Field(default_factory=list, max_length=30)
    source_scope: SourceScope
    coverage_ratio: float | None = Field(default=None, ge=0, le=1)
    conflicts: list[str] = Field(default_factory=list, max_length=12)
    unknowns: list[str] = Field(default_factory=list, max_length=12)


class OnlineEventType(str, Enum):
    NIGHT_OUT_OF_BED = "night_out_of_bed"


class RelativeBaselineDeviation(str, Enum):
    UNAVAILABLE = "unavailable"
    WITHIN_BASELINE = "within_baseline"
    MINOR = "minor"
    SIGNIFICANT = "significant"


class MultiSourceConsistency(str, Enum):
    UNAVAILABLE = "unavailable"
    SINGLE_SOURCE = "single_source"
    CONSISTENT = "consistent"
    MIXED = "mixed"
    CONFLICTING = "conflicting"


class OnlineDataQuality(str, Enum):
    UNUSABLE = "unusable"
    LIMITED = "limited"
    USABLE = "usable"


class CurrentContextRisk(str, Enum):
    ROUTINE = "routine"
    UNCERTAIN = "uncertain"
    CONCERNING = "concerning"


class LongitudinalTrend(str, Enum):
    UNAVAILABLE = "unavailable"
    STABLE = "stable"
    IMPROVING = "improving"
    WORSENING = "worsening"


class OnlineRiskLevel(str, Enum):
    NORMAL = "normal"
    WATCH = "watch"
    ESCALATE = "escalate"


class MultifactorSafetyInput(StrictContract):
    absolute_red_flag: bool = False
    absolute_red_flag_codes: tuple[str, ...] = ()
    absolute_red_flag_requires_urgent: bool = False
    relative_baseline_deviation: RelativeBaselineDeviation = (
        RelativeBaselineDeviation.UNAVAILABLE
    )
    multi_source_consistency: MultiSourceConsistency = (
        MultiSourceConsistency.UNAVAILABLE
    )
    data_quality: OnlineDataQuality = OnlineDataQuality.LIMITED
    current_context: CurrentContextRisk = CurrentContextRisk.UNCERTAIN
    longitudinal_trend: LongitudinalTrend = LongitudinalTrend.UNAVAILABLE
    source_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_red_flag(self) -> "MultifactorSafetyInput":
        if self.absolute_red_flag and not self.absolute_red_flag_codes:
            raise ValueError("absolute red flag requires at least one code")
        if self.absolute_red_flag_requires_urgent and not self.absolute_red_flag:
            raise ValueError("urgent red flag requires absolute_red_flag=true")
        return self


class MultifactorSafetyDecision(StrictContract):
    risk_level: OnlineRiskLevel
    safety_required: bool
    urgent_required: bool
    reason_codes: tuple[str, ...]
    personalization_effect: Literal[
        "none", "noncritical_noise_reduction", "explanation_only"
    ]
    source_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def enforce_escalation(self) -> "MultifactorSafetyDecision":
        if self.risk_level == OnlineRiskLevel.ESCALATE and not self.safety_required:
            raise ValueError("risk_level=escalate requires Safety")
        if self.urgent_required and self.risk_level != OnlineRiskLevel.ESCALATE:
            raise ValueError("urgent path requires risk_level=escalate")
        return self


class OnlineReasoningEvent(StrictContract):
    event_id: str = Field(..., min_length=1)
    event_type: OnlineEventType
    occurred_at: datetime
    current_signals: dict[str, Any] = Field(default_factory=dict)
    current_signal_refs: tuple[str, ...] = ()
    quality_refs: tuple[str, ...] = ()
    trend_refs: tuple[str, ...] = ()
    clinical_context_refs: tuple[str, ...] = ()
    safety_factors: MultifactorSafetyInput


class EventContextResolution(StrictContract):
    event_id: str = Field(..., min_length=1)
    event_type: OnlineEventType
    evidence_gap_code: str = Field(..., min_length=1)
    required_concept_ids: tuple[str, ...]
    care_delivery_concept_ids: tuple[str, ...]
    baseline_metric_ids: tuple[str, ...]
    required_current_signal_keys: tuple[str, ...]
    current_signals: dict[str, Any]
    current_signal_refs: tuple[str, ...]
    missing_current_signal_keys: tuple[str, ...]
    required_quality_ref_kinds: tuple[str, ...]
    quality_refs: tuple[str, ...]
    required_trend_ref_kinds: tuple[str, ...]
    trend_refs: tuple[str, ...]
    required_clinical_ref_kinds: tuple[str, ...]
    clinical_context_refs: tuple[str, ...]
    source_refs: tuple[str, ...]


class CareDeliveryTiming(str, Enum):
    IMMEDIATE = "immediate"
    MORNING = "morning"


class CareDeliveryModality(str, Enum):
    VOICE = "voice"
    LIGHT = "light"
    SILENT = "silent"


class InterruptionBurden(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CareDeliveryDecision(StrictContract):
    timing: CareDeliveryTiming
    modality: CareDeliveryModality
    interruption_burden: InterruptionBurden
    notify_family: bool = False
    voice_volume_percent: int | None = Field(default=None, ge=0, le=100)
    voice_tone: Literal["gentle", "neutral", "urgent"] | None = None
    quiet_hours_active: bool = False
    quiet_hours_override: bool = False
    conservative_default_applied: bool = False
    device_policy_ref: str = Field(..., min_length=1)
    coordination_policy_ref: str | None = Field(default=None, min_length=1)
    preference_evidence_refs: tuple[str, ...] = ()
    safety_reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_delivery(self) -> "CareDeliveryDecision":
        if self.modality == CareDeliveryModality.VOICE:
            if self.voice_volume_percent is None or self.voice_tone is None:
                raise ValueError("voice delivery requires volume and tone")
        elif self.voice_volume_percent is not None or self.voice_tone is not None:
            raise ValueError("non-voice delivery cannot set voice controls")
        if self.notify_family and not self.coordination_policy_ref:
            raise ValueError("family notification requires coordination policy")
        if (
            self.quiet_hours_active
            and self.timing == CareDeliveryTiming.IMMEDIATE
            and not self.quiet_hours_override
        ):
            raise ValueError("immediate delivery in quiet hours requires override")
        if self.quiet_hours_override and not self.safety_reason_codes:
            raise ValueError("quiet-hours override requires a safety reason")
        if self.conservative_default_applied and (
            self.timing != CareDeliveryTiming.MORNING
            or self.modality != CareDeliveryModality.SILENT
            or self.notify_family
            or self.quiet_hours_override
            or self.interruption_burden
            not in {InterruptionBurden.NONE, InterruptionBurden.LOW}
        ):
            raise ValueError("conservative default must be morning and silent")
        return self


class CareActionCandidate(StrictContract):
    candidate_id: str = Field(..., min_length=1)
    candidate_version: int = Field(..., ge=1)
    candidate_hash: str = Field(..., min_length=64, max_length=64)
    care_action_id: str | None = None
    care_action_version: int | None = Field(default=None, ge=1)
    title: str = Field(..., min_length=1, max_length=500)
    rationale_evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    parameters: dict[str, Any] = Field(default_factory=dict)
    delivery: CareDeliveryDecision | None = None
    duration_days: int | None = Field(default=None, ge=1, le=90)
    objective_metric: str | None = None
    subjective_question_id: str | None = None
    stop_conditions: list[str] = Field(default_factory=list, max_length=12)
    confirmation_required: bool = True
    activatable: bool = False

    @classmethod
    def create(cls, **values: Any) -> "CareActionCandidate":
        material = dict(values)
        material.pop("candidate_hash", None)
        return cls(candidate_hash=stable_hash(material), **material)

    @model_validator(mode="after")
    def require_catalog_identity(self) -> "CareActionCandidate":
        if self.activatable and (
            not self.care_action_id or self.care_action_version is None
        ):
            raise ValueError("activatable Care action requires a catalog entry")
        return self


class CoordinationCandidate(StrictContract):
    candidate_id: str = Field(..., min_length=1)
    recipient_role: Literal["family", "doctor", "device"]
    reason: str = Field(..., min_length=1, max_length=600)
    timing: str = Field(..., min_length=1)
    dedupe_key: str = Field(..., min_length=1)
    stop_conditions: list[str] = Field(default_factory=list)


class CareStrategy(StrictContract):
    strategy_id: str = Field(..., min_length=1)
    disposition: Literal[
        "no_action", "propose", "maintain", "adjust", "pause", "complete", "end"
    ]
    evidence_packet_refs: list[str] = Field(default_factory=list, max_length=10)
    primary_action: CareActionCandidate | None = None
    coordination_candidates: list[CoordinationCandidate] = Field(
        default_factory=list, max_length=4
    )
    evidence_request: CrossAgentRequest | None = None
    transition_confirmation_required: bool | None = None

    @model_validator(mode="after")
    def enforce_single_action(self) -> "CareStrategy":
        if self.disposition == "propose" and self.primary_action is None:
            raise ValueError("propose disposition requires primary_action")
        if self.disposition == "adjust" and self.primary_action is None:
            raise ValueError("adjust disposition requires primary_action")
        required = self.disposition in {
            "propose",
            "adjust",
            "pause",
            "complete",
            "end",
        }
        if self.transition_confirmation_required is None:
            object.__setattr__(
                self,
                "transition_confirmation_required",
                required,
            )
        elif required and not self.transition_confirmation_required:
            raise ValueError("material Care transition requires confirmation")
        if self.evidence_request and (
            self.evidence_request.sender != AgentId.CARE_STRATEGY
            or self.evidence_request.receiver != AgentId.EVIDENCE_REASONING
        ):
            raise ValueError("Care evidence_request must target EvidenceReasoning")
        return self


class SafetyDecision(StrictContract):
    verdict: SafetyVerdict
    review_target_type: str = Field(..., min_length=1)
    review_target_id: str = Field(..., min_length=1)
    review_target_hash: str = Field(..., min_length=64, max_length=64)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    reviewed_episode_state_revision: int = Field(..., ge=0)
    policy_version: str = Field(..., min_length=1)
    expires_at: datetime
    issue_locations: list[str] = Field(default_factory=list, max_length=20)
    reason_codes: list[str] = Field(default_factory=list, max_length=20)
    responsible_agent: AgentId | None = None
    conservative_fallback: str | None = Field(default=None, max_length=600)

    @model_validator(mode="after")
    def require_revision_details(self) -> "SafetyDecision":
        if self.verdict in {SafetyVerdict.REVISE, SafetyVerdict.BLOCK}:
            if not self.reason_codes:
                raise ValueError("revise/block requires reason_codes")
        if self.verdict == SafetyVerdict.REVISE and self.responsible_agent is None:
            raise ValueError("revise requires responsible_agent")
        if self.verdict == SafetyVerdict.REVISE and not self.issue_locations:
            raise ValueError("revise requires exact issue_locations")
        if self.verdict == SafetyVerdict.BLOCK and not self.conservative_fallback:
            raise ValueError("block requires a conservative fallback")
        return self


class CommunicationSemanticBinding(StrictContract):
    binding_id: str = Field(..., min_length=1)
    source_kind: Literal["evidence_claim", "care_candidate", "general_knowledge"]
    source_ref: str = Field(..., min_length=1)
    rendered_text: str = Field(..., min_length=1, max_length=1600)
    preserved_numbers: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def canonicalize_preserved_numbers(self) -> "CommunicationSemanticBinding":
        # Numeric tokenization is deterministic provenance metadata, not an LLM
        # judgment. Governance still checks every token against the bound source.
        object.__setattr__(
            self,
            "preserved_numbers",
            sorted(
                set(
                    re.findall(
                        r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?%?",
                        self.rendered_text,
                    )
                )
            ),
        )
        return self


class CommunicationDraft(StrictContract):
    draft_id: str = Field(..., min_length=1)
    audience_role: Literal["elder", "family", "doctor"]
    text: str = Field(..., min_length=1, max_length=6000)
    claim_refs: list[str] = Field(default_factory=list, max_length=40)
    care_candidate_refs: list[str] = Field(default_factory=list, max_length=10)
    semantic_bindings: list[CommunicationSemanticBinding] = Field(
        default_factory=list, max_length=60
    )
    memory_change_candidates: list["MemoryChangeCandidate"] = Field(
        default_factory=list, max_length=8
    )
    context_notice: str = Field(..., min_length=1, max_length=500)
    artifact_kind: str | None = None

    @model_validator(mode="after")
    def require_rendered_semantic_bindings(self) -> "CommunicationDraft":
        if any(
            binding.rendered_text not in self.text
            for binding in self.semantic_bindings
        ):
            raise ValueError(
                "every semantic binding rendered_text must be an exact substring "
                "of Communication text"
            )
        bound_numbers: set[str] = set()
        for binding in self.semantic_bindings:
            rendered_numbers = set(
                re.findall(
                    r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?%?",
                    binding.rendered_text,
                )
            )
            if set(binding.preserved_numbers) != rendered_numbers:
                raise ValueError(
                    "every semantic binding preserved_numbers must enumerate "
                    "exactly the numbers in rendered_text"
                )
            bound_numbers.update(rendered_numbers)
        text_numbers = set(
            re.findall(
                r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?%?",
                self.text,
            )
        )
        unbound_numbers = sorted(text_numbers.difference(bound_numbers))
        if unbound_numbers:
            raise ValueError(
                "every number in Communication text must be covered by a "
                f"semantic binding; remove these unbound tokens: {unbound_numbers}"
            )
        return self


class MemoryChangeCandidate(StrictContract):
    candidate_id: str = Field(..., min_length=1)
    candidate_version: int = Field(default=1, ge=1)
    candidate_hash: str | None = Field(default=None, min_length=64, max_length=64)
    operation: Literal["create", "replace", "expire", "forget"]
    subject_id: str = Field(..., min_length=1)
    memory_type: Literal[
        "preference",
        "routine",
        "environment",
        "communication_preference",
    ]
    concept_id: str = Field(
        ...,
        pattern=r"^[a-z0-9][a-z0-9_.-]{1,159}$",
    )
    value_schema_id: Literal[
        "bounded_string.v1",
        "boolean.v1",
        "number.v1",
        "enum.v1",
    ]
    value_schema_version: str = Field(default="1", min_length=1)
    typed_value: Any
    provenance_type: Literal[
        "elder_confirmed",
        "authorized_observer",
        "accepted_evidence",
    ]
    source_ref: str = Field(..., min_length=1)
    sensitivity_class: Literal["personal", "sensitive_personal"]
    allowed_roles: tuple[AgentId, ...] = Field(min_length=1)
    allowed_purposes: tuple[
        Literal[
            "personal_evidence_context",
            "care_preference_context",
            "explicit_memory_review",
            "explicit_memory_change",
            "explicit_memory_forget",
        ],
        ...,
    ] = Field(min_length=1)
    valid_until: datetime | None = None
    retention_policy_version: str = Field(
        default="sleepagent-retention.v1",
        min_length=1,
    )
    explicit_user_authorization: bool = False
    confirmation_required: bool = True

    @property
    def value(self) -> str:
        if isinstance(self.typed_value, bool):
            return "true" if self.typed_value else "false"
        return str(self.typed_value)

    @model_validator(mode="after")
    def bind_candidate_hash(self) -> "MemoryChangeCandidate":
        if not self.confirmation_required and not self.explicit_user_authorization:
            raise ValueError(
                "unconfirmed Memory write requires explicit elder authorization"
            )
        if not set(self.allowed_roles).issubset(
            {
                AgentId.SLEEP_CARE,
                AgentId.EVIDENCE_REASONING,
                AgentId.CARE_STRATEGY,
            }
        ):
            raise ValueError("Safety cannot receive governed personal Memory")
        if self.value_schema_id == "bounded_string.v1":
            if (
                type(self.typed_value) is not str
                or not 1 <= len(self.typed_value) <= 500
            ):
                raise ValueError("bounded_string.v1 requires 1..500 characters")
        elif self.value_schema_id == "boolean.v1":
            if type(self.typed_value) is not bool:
                raise ValueError("boolean.v1 requires a strict boolean")
        elif self.value_schema_id == "number.v1":
            if (
                type(self.typed_value) not in {int, float}
                or not math.isfinite(float(self.typed_value))
            ):
                raise ValueError("number.v1 requires a strict number")
        elif self.value_schema_id == "enum.v1":
            if (
                type(self.typed_value) is not str
                or not re.fullmatch(
                    r"[a-z0-9][a-z0-9_.-]{0,79}",
                    self.typed_value,
                )
            ):
                raise ValueError("enum.v1 requires a normalized enum code")
        material = self.model_dump(exclude={"candidate_hash"}, mode="json")
        expected = stable_hash(material)
        if self.candidate_hash is not None and self.candidate_hash != expected:
            raise ValueError("Memory candidate_hash does not bind candidate content")
        if self.candidate_hash is None:
            object.__setattr__(self, "candidate_hash", expected)
        return self


class ExternalActionTarget(StrictContract):
    target_id: str = Field(..., min_length=1)
    target_version: int = Field(default=1, ge=1)
    tool_name: Literal["external.notify", "external.share", "external.export"]
    actor_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    action_scope: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime


Payload = EvidencePacket | CareStrategy | SafetyDecision | CommunicationDraft


class AgentEnvelope(StrictContract):
    episode_id: str = Field(..., min_length=1)
    invocation_id: str = Field(..., min_length=1)
    parent_invocation_id: str | None = None
    fact_snapshot_id: str = Field(..., min_length=1)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    episode_state_revision: int = Field(..., ge=0)
    source_scope: SourceScope
    input_work_product_refs: list[str] = Field(default_factory=list, max_length=40)
    target_type: str = Field(..., min_length=1)
    target_id: str = Field(..., min_length=1)
    target_hash: str = Field(..., min_length=64, max_length=64)
    agent_id: AgentId
    agent_version: str = Field(..., min_length=1)
    profile_version: str = Field(default="unlocked", min_length=1)
    profile_hash: str = Field(
        default="0" * 64, min_length=64, max_length=64
    )
    skill_id: str = Field(..., min_length=1)
    skill_version: str = Field(..., min_length=1)
    skill_package_hash: str = Field(
        default="0" * 64, min_length=64, max_length=64
    )
    skill_lock_hash: str = Field(
        default="0" * 64, min_length=64, max_length=64
    )
    prompt_bundle_hash: str = Field(
        default="0" * 64, min_length=64, max_length=64
    )
    schema_version: str = Field(..., min_length=1)
    policy_version: str = Field(..., min_length=1)
    status: WorkProductStatus
    summary: str = Field(..., min_length=1, max_length=1800)
    tool_requests: list[ToolRequest] = Field(default_factory=list, max_length=8)
    collaboration_requests: list[CrossAgentRequest] = Field(
        default_factory=list, max_length=4
    )
    reason_codes: list[str] = Field(default_factory=list, max_length=20)
    expires_at: datetime | None = None
    output_payload: Payload

    @model_validator(mode="after")
    def require_unique_payload(self) -> "AgentEnvelope":
        expected = {
            AgentId.EVIDENCE_REASONING: EvidencePacket,
            AgentId.CARE_STRATEGY: CareStrategy,
            AgentId.SAFETY_REVIEW: SafetyDecision,
            AgentId.SLEEP_CARE: CommunicationDraft,
        }[self.agent_id]
        if not isinstance(self.output_payload, expected):
            raise ValueError(
                f"{self.agent_id.value} requires {expected.__name__}"
            )
        if any(  # type: ignore[unreachable]
            req.sender != self.agent_id for req in self.collaboration_requests
        ):
            raise ValueError("collaboration sender must match envelope agent")
        return self


class ToolReceipt(StrictContract):
    tool_invocation_id: str = Field(..., min_length=1)
    tool_name: str = Field(..., min_length=1)
    tool_version: str = Field(..., min_length=1)
    caller: str = Field(..., min_length=1)
    fact_snapshot_id: str = Field(..., min_length=1)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    input_hash: str = Field(..., min_length=64, max_length=64)
    effect: ToolEffect
    outcome: InvocationOutcome
    observed_at: datetime
    output: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list, max_length=50)
    idempotency_key: str | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def require_idempotency(self) -> "ToolReceipt":
        if self.effect != ToolEffect.READ_ONLY and not self.idempotency_key:
            raise ValueError("state-changing ToolReceipt requires idempotency_key")
        return self


class EpisodeBudget(StrictContract):
    agent_call_limit: int = Field(..., ge=0)
    total_model_call_limit: int = Field(..., ge=0)
    tool_call_limit: int = Field(..., ge=0)
    replan_limit: int = Field(default=2, ge=0, le=2)
    safety_revision_limit: int = Field(default=2, ge=0, le=2)
    soft_deadline_seconds: int = Field(..., ge=1)


class EpisodePlan(StrictContract):
    plan_id: str = Field(..., min_length=1)
    episode_id: str = Field(..., min_length=1)
    episode_type: EpisodeType
    objective: str = Field(..., min_length=1, max_length=1200)
    required_work_products: list[WorkProductKind]
    conditional_work_products: list[WorkProductKind] = Field(default_factory=list)
    allowed_agents: list[AgentId]
    allowed_tools: list[str]
    safety_checkpoints: list[str] = Field(default_factory=list)
    exit_conditions: list[str]
    expected_agent_calls: int = Field(..., ge=0)
    expected_tool_calls: int = Field(..., ge=0)


class EpisodeReceipt(StrictContract):
    episode_id: str = Field(..., min_length=1)
    episode_type: EpisodeType
    receipt_revision: int = Field(..., ge=1)
    terminal: bool
    execution_mode: ExecutionMode
    status: EpisodeStatus
    goal_achieved: bool
    fact_snapshot_id: str = Field(..., min_length=1)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    source_scope: SourceScope
    final_episode_state_revision: int = Field(..., ge=0)
    agent_invocation_ids: list[str] = Field(default_factory=list)
    accepted_work_product_refs: list[str] = Field(default_factory=list)
    tool_receipt_ids: list[str] = Field(default_factory=list)
    safety_decision_refs: list[str] = Field(default_factory=list)
    failure_codes: list[str] = Field(default_factory=list)
    trace_ref: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_truth(self) -> "EpisodeReceipt":
        waiting = self.status in {
            EpisodeStatus.WAITING_USER,
            EpisodeStatus.WAITING_CONFIRMATION,
        }
        if waiting == self.terminal:
            raise ValueError("waiting receipts must be non-terminal; other receipts terminal")
        if self.execution_mode == ExecutionMode.DETERMINISTIC_ONLY:
            if self.agent_invocation_ids:
                raise ValueError("deterministic-only receipt cannot claim Agent calls")
        if self.goal_achieved and self.status != EpisodeStatus.COMPLETE:
            raise ValueError("only complete Episode can achieve goal")
        return self


def stable_hash(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def agent_target_hash(
    *,
    episode_id: str,
    target_type: str,
    target_id: str,
    fact_snapshot_id: str,
    fact_snapshot_hash: str,
    episode_state_revision: int,
    source_scope: SourceScope,
    input_work_product_refs: list[str],
    agent_id: AgentId,
    agent_version: str,
    profile_hash: str,
    skill_id: str,
    skill_version: str,
    skill_package_hash: str,
    schema_version: str,
    policy_version: str,
    output_payload: BaseModel | dict[str, Any],
) -> str:
    return stable_hash(
        {
            "episode_id": episode_id,
            "target_type": target_type,
            "target_id": target_id,
            "fact_snapshot_id": fact_snapshot_id,
            "fact_snapshot_hash": fact_snapshot_hash,
            "episode_state_revision": episode_state_revision,
            "source_scope": source_scope.model_dump(mode="json"),
            "input_work_product_refs": sorted(input_work_product_refs),
            "agent_id": agent_id.value,
            "agent_version": agent_version,
            "profile_hash": profile_hash,
            "skill_id": skill_id,
            "skill_version": skill_version,
            "skill_package_hash": skill_package_hash,
            "schema_version": schema_version,
            "policy_version": policy_version,
            "output_payload": (
                output_payload.model_dump(mode="json")
                if isinstance(output_payload, BaseModel)
                else output_payload
            ),
        }
    )


__all__ = [
    "PRODUCT_AGENT_CONTRACT_VERSION",
    "PRODUCT_AGENT_ROSTER",
    "AgentEnvelope",
    "AgentId",
    "AuthenticatedBinding",
    "CareActionCandidate",
    "CareDeliveryDecision",
    "CareDeliveryModality",
    "CareDeliveryTiming",
    "CareStrategy",
    "CommunicationDraft",
    "CommunicationSemanticBinding",
    "ContextPacket",
    "CoordinationCandidate",
    "CrossAgentRequest",
    "CrossAgentRequestType",
    "EpisodeBudget",
    "EpisodePlan",
    "EpisodeReceipt",
    "EpisodeStatus",
    "EpisodeType",
    "EvidenceClaim",
    "EvidencePacket",
    "EvidenceSemantic",
    "EvidenceSourceKind",
    "EventContextResolution",
    "ExecutionMode",
    "ExternalActionTarget",
    "FactSnapshot",
    "FrozenContract",
    "InvocationOutcome",
    "InterruptionBurden",
    "LongitudinalTrend",
    "MemoryChangeCandidate",
    "MultifactorSafetyDecision",
    "MultifactorSafetyInput",
    "MultiSourceConsistency",
    "OnlineDataQuality",
    "OnlineEventType",
    "OnlineReasoningEvent",
    "OnlineRiskLevel",
    "CurrentContextRisk",
    "RelativeBaselineDeviation",
    "SafetyDecision",
    "SafetyVerdict",
    "SourceScope",
    "SourceScopeKind",
    "StrictContract",
    "ToolEffect",
    "ToolReceipt",
    "ToolRequest",
    "TrustLabel",
    "TrustedContextItem",
    "WorkProductKind",
    "WorkProductStatus",
    "agent_target_hash",
    "stable_hash",
]

# 运行时 Protocol 与 DTO 共置，避免无行为的 ports 微模块。

from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol

from pydantic import Field

from sleepagent.runtime.contracts import (
    AgentId,
    CommunicationDraft,
    FactSnapshot,
    StrictContract,
    ToolReceipt,
    TrustedContextItem,
)
if TYPE_CHECKING:
    from sleepagent.runtime.results import (
        ProductEpisodeRunRequest,
        ProductEpisodeRunResult,
    )


class PublicationPublisher(Protocol):
    def publish(self, draft: CommunicationDraft) -> bool: ...


class MemoryPublicationGate(Protocol):
    def validate_prepublication(
        self,
        receipt_outputs: tuple[dict[str, Any], ...],
        *,
        subject_id: str,
        actor_id: str,
    ) -> None: ...


class PublicationJournalEntryPort(Protocol):
    @property
    def intent_id(self) -> str: ...

    @property
    def episode_id(self) -> str: ...

    @property
    def command_hash(self) -> str | None: ...

    @property
    def draft_hash(self) -> str: ...

    @property
    def state(self) -> Literal["reserved", "delivered", "failed"]: ...

    @property
    def delivered(self) -> bool | None: ...


class ProductEpisodeResultStore(Protocol):
    def append_nonterminal(
        self,
        result: ProductEpisodeRunResult,
        *,
        subject_id: str,
    ) -> None: ...

    def append_terminal_bundle(
        self,
        result: ProductEpisodeRunResult,
        *,
        subject_id: str,
        now: datetime | None = None,
    ) -> object: ...

    def latest(self, episode_id: str) -> ProductEpisodeRunResult: ...

    def history(self, episode_id: str) -> list[ProductEpisodeRunResult]: ...

    def reserve_publication(
        self,
        *,
        command_hash: str,
        episode_id: str,
        draft_hash: str,
        now: datetime | None = None,
    ) -> tuple[PublicationJournalEntryPort, bool]: ...

    def complete_publication(
        self,
        *,
        intent_id: str,
        delivered: bool,
        now: datetime | None = None,
    ) -> PublicationJournalEntryPort: ...


class ProductEpisodeRunnerPort(Protocol):
    """The narrow Product Agent facade consumed by process adapters."""

    def run(
        self,
        request: ProductEpisodeRunRequest,
    ) -> ProductEpisodeRunResult: ...


class ProductToolExecutionContext(StrictContract):
    caller: AgentId | str
    fact_snapshot: FactSnapshot
    authorization_scope: tuple[str, ...] = ()
    episode_id: str | None = None
    plan_id: str | None = None
    plan_revision: int | None = Field(default=None, ge=0)
    allowed_plan_step_ids: tuple[str, ...] = ()
    plan_step_id: str | None = None
    invocation_id: str = Field(default="runtime:unbound", min_length=1)
    user_intent_ref: str | None = None
    user_intent_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    user_intent_purpose: Literal[
        "explicit_memory_review",
        "explicit_memory_change",
        "explicit_memory_forget",
    ] | None = None
    max_memory_items: int = Field(default=8, ge=1, le=20)
    memory_token_budget: int = Field(default=1200, ge=64, le=4000)


class ProductToolResult(StrictContract):
    receipt: ToolReceipt
    context_item: TrustedContextItem | None = None


ToolHandler = Callable[
    [dict[str, Any], ProductToolExecutionContext],
    dict[str, Any],
]


class ProductToolExecutorPort(Protocol):
    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        context: ProductToolExecutionContext,
    ) -> ProductToolResult: ...

    def release_episode(self, episode_id: str) -> None: ...


FactSnapshotRevalidator = Callable[[FactSnapshot], bool]


__all__ = [
    "FactSnapshotRevalidator",
    "MemoryPublicationGate",
    "ProductEpisodeResultStore",
    "ProductEpisodeRunnerPort",
    "ProductToolExecutionContext",
    "ProductToolExecutorPort",
    "ProductToolResult",
    "PublicationJournalEntryPort",
    "PublicationPublisher",
    "ToolHandler",
]
