from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from sleepagent.radar_agent.product_agent.cold_start import (
    CapabilityEligibilityReceipt,
    MetricReadinessDecision,
)
from sleepagent.radar_agent.product_agent.contracts import (
    AgentEnvelope,
    AgentId,
    CommunicationDraft,
    EpisodeReceipt,
    EpisodeStatus,
    EpisodeType,
    ExternalActionTarget,
    FactSnapshot,
    OnlineReasoningEvent,
    StrictContract,
    ToolReceipt,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.governance import AcceptedWorkProduct
from sleepagent.radar_agent.product_agent.habit_profile import (
    HabitProfileChangeSet,
)
from sleepagent.radar_agent.product_agent.invocation import AgentInvocationRecord
from sleepagent.radar_agent.product_agent.registry import EPISODE_DEFINITIONS
from sleepagent.radar_agent.questionnaire import (
    CapturedHabitAnswer,
    HabitConceptStatus,
    HabitQuestionAnswer,
    HabitQuestionCapture,
    HabitQuestionSelectionReceipt,
    HabitQuestionTrigger,
)


PRODUCT_EPISODE_RUNNER_VERSION = "sleepagent-product-runner.v45"
PRODUCT_EPISODE_RESULT_SCHEMA_VERSION = "ProductEpisodeRunResult.v38"


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
    online_events: tuple[OnlineReasoningEvent, ...] = ()
    personalized: bool = True
    doctor_material: bool = False
    external_action: bool = False
    external_action_target: ExternalActionTarget | None = None
    idempotency_key: str | None = None
    profile_purpose: Literal[
        "evidence",
        "care",
        "profile_review",
        "doctor_material",
        "family_coordination",
    ] | None = None
    profile_relevant_concept_ids: tuple[str, ...] = ()
    habit_question_trigger: HabitQuestionTrigger | None = None
    habit_candidate_concept_ids: tuple[str, ...] = ()
    habit_decision_gap_ref: str | None = None
    habit_alternative_explanations: tuple[str, ...] = ()
    habit_concept_states: dict[str, HabitConceptStatus] = Field(
        default_factory=dict
    )
    habit_profile_update_requested: bool = False
    habit_question_max: int = Field(default=3, ge=1, le=3)
    habit_selection: HabitQuestionSelectionReceipt | None = None
    habit_answers: tuple[HabitQuestionAnswer, ...] = ()
    habit_profile_candidate_answers: tuple[CapturedHabitAnswer, ...] = ()

    @model_validator(mode="after")
    def validate_habit_flow(self) -> ProductEpisodeRunRequest:
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
        event_ids = [item.event_id for item in self.online_events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("online event IDs must be unique")
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
        if self.habit_answers and self.habit_selection is None:
            raise ValueError("Habit answers require a Selection receipt")
        if self.habit_profile_candidate_answers and (
            self.fact_snapshot.binding.role != "elder"
            or not self.habit_profile_update_requested
        ):
            raise ValueError(
                "Profile candidates require an elder review/update request"
            )
        if (
            self.habit_question_trigger
            == HabitQuestionTrigger.EXPLICIT_PROFILE_REVIEW
            and (
                self.profile_purpose != "profile_review"
                or not self.profile_relevant_concept_ids
                or not self.habit_profile_update_requested
            )
        ):
            raise ValueError(
                "Profile review questions require prior summary and update request"
            )
        return self


class PendingConfirmationTarget(StrictContract):
    confirmation_id: str = Field(..., min_length=1)
    decision_id: str | None = Field(default=None, min_length=1)
    proposal_id: str | None = Field(default=None, min_length=1)
    target_kind: Literal["memory", "care", "external_action", "habit_profile"]
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
    receipt: EpisodeReceipt
    publication: CommunicationDraft | None = None
    publication_delivered: bool = False
    envelopes: list[AgentEnvelope] = Field(default_factory=list)
    agent_invocations: list[AgentInvocationRecord] = Field(default_factory=list)
    tool_receipts: list[ToolReceipt] = Field(default_factory=list)
    accepted_work_products: list[AcceptedWorkProduct] = Field(default_factory=list)
    habit_selection: HabitQuestionSelectionReceipt | None = None
    habit_capture: HabitQuestionCapture | None = None
    habit_change_set: HabitProfileChangeSet | None = None
    committed_memory_candidate_ids: list[str] = Field(default_factory=list)
    committed_habit_change_set_id: str | None = None
    declined_confirmation_ids: list[str] = Field(default_factory=list)
    committed_care_candidate_id: str | None = None
    external_action_target_id: str | None = None
    external_action_target: ExternalActionTarget | None = None
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
        if self.external_action_target is not None and (
            self.external_action_target_id != self.external_action_target.target_id
        ):
            raise ValueError("frozen external action target ID mismatch")
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
        return self


class ReexecuteWithAddedFact(StrictContract):
    """A WAITING_USER continuation that deliberately re-enters reasoning."""

    request: ProductEpisodeRunRequest
    frozen_result: ProductEpisodeRunResult
    added_fact: ProductUserFactResponse

    @model_validator(mode="after")
    def validate_checkpoint_binding(self) -> ReexecuteWithAddedFact:
        pending = self.frozen_result.pending_user_input
        if (
            self.frozen_result.receipt.status != EpisodeStatus.WAITING_USER
            or pending is None
        ):
            raise ValueError("added-fact re-execution requires WAITING_USER")
        if (
            self.frozen_result.receipt.episode_id != self.request.episode_id
            or self.frozen_result.receipt.fact_snapshot_id
            != self.request.fact_snapshot.fact_snapshot_id
            or self.frozen_result.receipt.fact_snapshot_hash
            != self.request.fact_snapshot.fact_snapshot_hash
        ):
            raise ValueError("added-fact checkpoint binding mismatch")
        if self.added_fact.request_id != pending.request_id:
            raise ValueError("added fact does not answer the frozen request")
        if any(
            item.request_id == self.added_fact.request_id
            for item in self.request.user_fact_responses
        ):
            raise ValueError("added fact request was already answered")
        binding = self.request.fact_snapshot.binding
        if (
            self.added_fact.actor_id != binding.actor_id
            or self.added_fact.actor_role != binding.role
            or self.added_fact.subject_id != binding.subject_id
        ):
            raise ValueError("added fact does not match authenticated binding")
        return self

    def reexecution_request(self) -> ProductEpisodeRunRequest:
        responses = {
            item.request_id: item for item in self.request.user_fact_responses
        }
        responses[self.added_fact.request_id] = self.added_fact
        return ProductEpisodeRunRequest.model_validate(
            self.request.model_copy(
                update={
                    "user_fact_responses": tuple(
                        responses[key] for key in sorted(responses)
                    )
                }
            ).model_dump(mode="python")
        )


class CommitFrozenConfirmedAction(StrictContract):
    """A WAITING_CONFIRMATION continuation that cannot re-enter reasoning."""

    request: ProductEpisodeRunRequest
    frozen_result: ProductEpisodeRunResult

    @model_validator(mode="after")
    def validate_checkpoint_binding(self) -> CommitFrozenConfirmedAction:
        if (
            self.frozen_result.receipt.status
            != EpisodeStatus.WAITING_CONFIRMATION
        ):
            raise ValueError(
                "frozen confirmed commit requires WAITING_CONFIRMATION"
            )
        if (
            self.frozen_result.receipt.episode_id != self.request.episode_id
            or self.frozen_result.receipt.fact_snapshot_id
            != self.request.fact_snapshot.fact_snapshot_id
            or self.frozen_result.receipt.fact_snapshot_hash
            != self.request.fact_snapshot.fact_snapshot_hash
        ):
            raise ValueError("frozen confirmed commit checkpoint binding mismatch")
        if any(
            target.decision_id is None or target.proposal_id is None
            for target in self.frozen_result.pending_confirmations
        ):
            raise ValueError(
                "frozen confirmed commit requires explicit authority bindings"
            )
        return self


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
    "CommitFrozenConfirmedAction",
    "PRODUCT_EPISODE_RESULT_SCHEMA_VERSION",
    "PRODUCT_EPISODE_RUNNER_VERSION",
    "PendingConfirmationTarget",
    "PendingUserInputTarget",
    "ProductEpisodeRunRequest",
    "ProductEpisodeRunResult",
    "ProductUserFactResponse",
    "ReexecuteWithAddedFact",
    "doctor_safety_checkpoint",
    "effective_audience_role",
    "uses_doctor_material_semantics",
]
