# 本模块定义 Product Episode 请求、终态结果与持久化发布契约。
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from sleepagent.runtime.cold_start import (
    CapabilityEligibilityReceipt,
    MetricReadinessDecision,
)
from sleepagent.runtime.contracts import (
    AgentEnvelope,
    AgentId,
    CommunicationDraft,
    EpisodeReceipt,
    EpisodeStatus,
    EpisodeType,
    FactSnapshot,
    StrictContract,
    ToolReceipt,
    stable_hash,
)
from sleepagent.runtime.governance import AcceptedWorkProduct
from sleepagent.runtime.invocation import AgentInvocationRecord
from sleepagent.runtime.registry import EPISODE_DEFINITIONS


PRODUCT_EPISODE_RUNNER_VERSION = "sleepagent-product-runner.v47"
PRODUCT_EPISODE_RESULT_SCHEMA_VERSION = "ProductEpisodeRunResult.v40"
LEGACY_UNBOUND_WAITING_RESULT_SCHEMA_VERSION = "ProductEpisodeRunResult.v38"


def product_episode_request_hash(request: "ProductEpisodeRunRequest") -> str:
    """Bind a continuation to the exact request persisted at its checkpoint."""

    return stable_hash(request.model_dump(mode="json"))


class ProductUserFactResponse(StrictContract):
    request_id: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1, max_length=1000)
    actor_id: str = Field(..., min_length=1)
    actor_role: Literal["elder", "family", "doctor"]
    subject_id: str = Field(..., min_length=1)
    observed_at: datetime

    @property
    def source_ref(self) -> str:
        prefix = (
            "user_report"
            if self.actor_role == "elder"
            else "authorized_observer_report"
        )
        return f"{prefix}:{stable_hash(self.model_dump(mode='json'))[:24]}"


class ProductEpisodeRunRequest(StrictContract):
    episode_id: str = Field(..., min_length=1)
    episode_type: EpisodeType
    objective: str = Field(..., min_length=1, max_length=1200)
    fact_snapshot: FactSnapshot
    runtime_readiness_decisions: tuple[MetricReadinessDecision, ...] = ()
    runtime_capability_receipts: tuple[CapabilityEligibilityReceipt, ...] = ()
    user_text: str = Field(default="", max_length=4000)
    user_fact_responses: tuple[ProductUserFactResponse, ...] = ()
    audience_role: Literal["elder", "family", "doctor"] | None = None
    tool_inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    personalized: bool = True
    doctor_material: bool = False
    idempotency_key: str | None = None
    profile_purpose: Literal[
        "evidence",
        "care",
        "profile_review",
        "doctor_material",
        "family_coordination",
    ] | None = None
    profile_relevant_concept_ids: tuple[str, ...] = ()
    @model_validator(mode="after")
    def validate_current_inputs(self) -> ProductEpisodeRunRequest:
        decision_refs = tuple(
            item.decision_ref for item in self.runtime_readiness_decisions
        )
        decision_hashes = tuple(
            item.decision_hash for item in self.runtime_readiness_decisions
        )
        capability_refs = tuple(
            item.receipt_ref for item in self.runtime_capability_receipts
        )
        capability_hashes = tuple(
            item.receipt_hash for item in self.runtime_capability_receipts
        )
        if (
            decision_refs != self.fact_snapshot.readiness_decision_refs
            or decision_hashes != self.fact_snapshot.readiness_decision_hashes
            or capability_refs
            != self.fact_snapshot.capability_eligibility_refs
            or capability_hashes
            != self.fact_snapshot.capability_eligibility_hashes
        ):
            raise ValueError(
                "runtime cold-start inputs must exactly match FactSnapshot"
            )
        response_ids = [item.request_id for item in self.user_fact_responses]
        if len(response_ids) != len(set(response_ids)):
            raise ValueError("user fact response IDs must be unique")
        for response in self.user_fact_responses:
            if (
                response.actor_id != self.fact_snapshot.binding.actor_id
                or response.actor_role != self.fact_snapshot.binding.role
                or response.subject_id != self.fact_snapshot.binding.subject_id
            ):
                raise ValueError(
                    "user fact response must match authenticated binding"
                )
        if self.profile_relevant_concept_ids and self.profile_purpose is None:
            raise ValueError("Profile concept read requires an explicit purpose")
        if self.doctor_material:
            if self.audience_role not in {None, "doctor"}:
                raise ValueError(
                    "doctor_material cannot target an elder or family audience"
                )
            if (
                self.episode_type is not EpisodeType.DATA_QUALITY_RECOVERY
                and doctor_safety_checkpoint(self.episode_type) is None
            ):
                raise ValueError(
                    "doctor_material requires an Episode with a registered "
                    "Safety checkpoint"
                )
        resolved_doctor_audience = self.audience_role == "doctor" or (
            self.audience_role is None
            and self.fact_snapshot.binding.role == "doctor"
        )
        if (
            resolved_doctor_audience
            and self.episode_type is not EpisodeType.ROLE_MATERIAL
            and not self.doctor_material
        ):
            raise ValueError(
                "doctor audience outside role material requires "
                "doctor_material safety semantics"
            )
        doctor_target = self.doctor_material or (
            self.episode_type is EpisodeType.ROLE_MATERIAL
            and (
                self.audience_role == "doctor"
                or (
                    self.audience_role is None
                    and self.fact_snapshot.binding.role == "doctor"
                )
            )
        )
        if doctor_target and "draft_material" not in (
            self.fact_snapshot.binding.authorization_scope
        ):
            raise ValueError(
                "doctor material requires draft_material authorization"
            )
        return self


class PendingConfirmationTarget(StrictContract):
    confirmation_id: str = Field(..., min_length=1)
    decision_id: str | None = Field(default=None, min_length=1)
    proposal_id: str | None = Field(default=None, min_length=1)
    target_kind: Literal["care"]
    candidate_id: str = Field(..., min_length=1)
    candidate_hash: str = Field(..., min_length=64, max_length=64)
    actor_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    action_scope: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1, max_length=600)
    expires_at: datetime

    @model_validator(mode="after")
    def authority_binding_is_complete(self) -> PendingConfirmationTarget:
        if (self.decision_id is None) != (self.proposal_id is None):
            raise ValueError(
                "confirmation authority binding requires decision and proposal IDs"
            )
        return self


class PendingUserInputTarget(StrictContract):
    request_id: str = Field(..., min_length=1)
    question_text: str = Field(..., min_length=1, max_length=500)
    why_needed: str = Field(..., min_length=1, max_length=500)
    decision_scope: str = Field(..., min_length=1, max_length=500)
    target_role: Literal["elder", "family", "doctor"]
    source_agent: AgentId
    expires_at: datetime


class ProductEpisodeRunResult(StrictContract):
    schema_version: str = PRODUCT_EPISODE_RESULT_SCHEMA_VERSION
    runner_version: str = PRODUCT_EPISODE_RUNNER_VERSION
    registry_hash: str
    continuation_request_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    continuation_checkpoint_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    continuation_kind: Literal[
        "reexecute_with_added_fact",
        "commit_frozen_confirmed_action",
    ] | None = None
    continuation_parent_checkpoint_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    continuation_command_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    receipt: EpisodeReceipt
    publication: CommunicationDraft | None = None
    publication_delivered: bool = False
    envelopes: list[AgentEnvelope] = Field(default_factory=list)
    agent_invocations: list[AgentInvocationRecord] = Field(default_factory=list)
    tool_receipts: list[ToolReceipt] = Field(default_factory=list)
    accepted_work_products: list[AcceptedWorkProduct] = Field(default_factory=list)
    # 旧 durable schema 字段保留为固定空值，避免重写历史迁移或 artifact reader。
    habit_selection: None = None
    habit_capture: None = None
    habit_change_set: None = None
    committed_memory_candidate_ids: list[str] = Field(default_factory=list)
    committed_habit_change_set_id: str | None = None
    declined_confirmation_ids: list[str] = Field(default_factory=list)
    committed_care_candidate_id: str | None = None
    external_action_target_id: str | None = None
    external_action_target: None = None
    external_action_target_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    external_action_receipt_id: str | None = None
    external_action_delivery_status: Literal["pending", "delivered"] | None = None
    pending_confirmations: list[PendingConfirmationTarget] = Field(
        default_factory=list
    )
    pending_user_input: PendingUserInputTarget | None = None

    @model_validator(mode="after")
    def pending_state_matches_receipt(self) -> ProductEpisodeRunResult:
        if (
            self.receipt.status
            in {EpisodeStatus.WAITING_USER, EpisodeStatus.WAITING_CONFIRMATION}
            and self.continuation_request_hash is None
            and self.schema_version
            != LEGACY_UNBOUND_WAITING_RESULT_SCHEMA_VERSION
        ):
            raise ValueError(
                "waiting Episode result requires an exact continuation request hash"
            )
        if len(self.declined_confirmation_ids) != len(
            set(self.declined_confirmation_ids)
        ):
            raise ValueError("declined confirmation IDs must be unique")
        if bool(self.external_action_receipt_id) != bool(
            self.external_action_delivery_status
        ):
            raise ValueError(
                "external action receipt and delivery status must be recorded together"
            )
        if bool(self.external_action_target_id) != bool(
            self.external_action_target_hash
        ):
            raise ValueError(
                "external action target ID and hash must be recorded together"
            )
        if self.external_action_receipt_id and self.external_action_target_hash is None:
            raise ValueError("external action receipt requires its exact target")
        if self.pending_confirmations and (
            self.receipt.status != EpisodeStatus.WAITING_CONFIRMATION
        ):
            raise ValueError(
                "pending confirmations require a waiting-confirmation receipt"
            )
        if (
            self.receipt.status == EpisodeStatus.WAITING_CONFIRMATION
            and not self.pending_confirmations
        ):
            raise ValueError(
                "waiting-confirmation receipt requires exact pending targets"
            )
        if self.pending_user_input is not None and (
            self.receipt.status != EpisodeStatus.WAITING_USER
        ):
            raise ValueError("pending user input requires a waiting-user receipt")
        if (
            self.receipt.status == EpisodeStatus.WAITING_USER
            and self.pending_user_input is None
        ):
            raise ValueError(
                "waiting-user receipt requires an exact user-input request"
            )
        if self.pending_confirmations and self.pending_user_input is not None:
            raise ValueError(
                "an Episode cannot wait for confirmation and user input together"
            )
        if (
            self.continuation_checkpoint_hash is not None
            and self.continuation_checkpoint_hash
            != product_episode_checkpoint_hash(self)
        ):
            raise ValueError("continuation checkpoint hash mismatch")
        lineage_fields = (
            self.continuation_kind,
            self.continuation_parent_checkpoint_hash,
            self.continuation_command_hash,
        )
        if any(item is not None for item in lineage_fields) and not all(
            item is not None for item in lineage_fields
        ):
            raise ValueError("continuation lineage must be complete")
        return self


def product_episode_checkpoint_hash(result: ProductEpisodeRunResult) -> str:
    """Hash the exact frozen continuation result without self-reference."""

    return stable_hash(
        result.model_dump(
            mode="json",
            exclude={"continuation_checkpoint_hash"},
        )
    )


def product_episode_frozen_identity_hash(
    result: ProductEpisodeRunResult,
) -> str:
    """Hash frozen semantics while ignoring API-added HDS binding metadata."""

    payload = result.model_dump(
        mode="json",
        exclude={"continuation_checkpoint_hash"},
    )
    for target in payload.get("pending_confirmations", []):
        target["decision_id"] = None
        target["proposal_id"] = None
    return stable_hash(payload)


def bind_product_episode_checkpoint(
    result: ProductEpisodeRunResult,
) -> ProductEpisodeRunResult:
    """Return a validated result bound to its exact continuation checkpoint."""

    unbound = result.model_copy(
        update={"continuation_checkpoint_hash": None}
    )
    return ProductEpisodeRunResult.model_validate(
        unbound.model_copy(
            update={
                "continuation_checkpoint_hash": (
                    product_episode_checkpoint_hash(unbound)
                )
            }
        ).model_dump(mode="python")
    )


def effective_audience_role(
    request: ProductEpisodeRunRequest,
) -> Literal["elder", "family", "doctor"]:
    if request.doctor_material:
        return "doctor"
    if request.audience_role is not None:
        return request.audience_role
    role = request.fact_snapshot.binding.role
    return role if role != "system" else "elder"


def uses_doctor_material_semantics(request: ProductEpisodeRunRequest) -> bool:
    return request.doctor_material or (
        request.episode_type is EpisodeType.ROLE_MATERIAL
        and effective_audience_role(request) == "doctor"
    )


def doctor_safety_checkpoint(episode_type: EpisodeType) -> str | None:
    registered = EPISODE_DEFINITIONS[episode_type].conditional_safety_checkpoints
    for checkpoint in (
        "doctor_material_safety",
        "personal_claim_safety",
        "care_candidate_safety",
        "external_action_safety",
    ):
        if checkpoint in registered:
            return checkpoint
    return None


__all__ = [
    "PRODUCT_EPISODE_RESULT_SCHEMA_VERSION",
    "PRODUCT_EPISODE_RUNNER_VERSION",
    "PendingConfirmationTarget",
    "PendingUserInputTarget",
    "ProductEpisodeRunRequest",
    "ProductEpisodeRunResult",
    "ProductUserFactResponse",
    "bind_product_episode_checkpoint",
    "product_episode_checkpoint_hash",
    "product_episode_frozen_identity_hash",
    "product_episode_request_hash",
    "doctor_safety_checkpoint",
    "effective_audience_role",
    "uses_doctor_material_semantics",
]
