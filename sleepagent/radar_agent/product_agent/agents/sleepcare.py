from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import TypeVar

from pydantic import Field, model_validator

from sleepagent.radar_agent.product_agent.agents.ports import (
    AgentControlPortBoundary,
    PurposeScopedReceipt,
    RoleContextBoundary,
    RoleInvocationInput,
    RoleInvocationOutput,
    RuntimeRoleInvocation,
    _ModelBackedRole,
    build_implementation_boundary,
    validate_role_output,
)
from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    CommunicationDraft,
    ContextPacket,
    EpisodeBudget,
    EpisodeType,
    FactSnapshot,
    FrozenContract,
    SourceScope,
    StrictContract,
    TrustLabel,
    TrustedContextItem,
    WorkProductKind,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.invocation import (
    AgentInvocationRecord,
    StructuredAgentModel,
)
from sleepagent.radar_agent.product_agent.registry import (
    TOOL_INVOCATION_ALLOWLIST,
)
from sleepagent.radar_agent.product_agent.skills import (
    SkillRegistry,
)


class EvaluationDecision(str, Enum):
    CONTINUE = "continue"
    REPLAN = "replan"
    WAIT_USER = "wait_user"
    WAIT_CONFIRMATION = "wait_confirmation"
    FINISH = "finish"
    BLOCK = "block"


class EpisodePlanProposal(StrictContract):
    objective: str = Field(..., min_length=1, max_length=1200)
    required_work_products: list[WorkProductKind]
    conditional_work_products: list[WorkProductKind] = Field(default_factory=list)
    safety_checkpoints: list[str] = Field(default_factory=list)
    exit_conditions: list[str]
    expected_agent_calls: int = Field(..., ge=0)
    expected_tool_calls: int = Field(..., ge=0)


class SleepCareEvaluation(StrictContract):
    decision: EvaluationDecision
    summary: str = Field(..., min_length=1, max_length=1000)
    replan_reason: str | None = None
    missing_work_products: list[WorkProductKind] = Field(default_factory=list)


class SleepCareInvocationInput(RoleInvocationInput):
    """Minimal user-facing Context for the final communication responsibility."""

    @model_validator(mode="after")
    def validate_sleepcare_context(self) -> "SleepCareInvocationInput":
        invocation = self.invocation
        if invocation.context.agent_id is not AgentId.SLEEP_CARE:
            raise ValueError("SleepCareInvocationInput requires SleepCare Context")
        if invocation.audience_role is None:
            raise ValueError("SleepCare communication requires an audience")
        if invocation.accepted_evidence_ref or invocation.review_target is not None:
            raise ValueError("SleepCare input contains another role's binding")
        audience_items = [
            item
            for item in invocation.context.items
            if item.key == "requested_audience_role"
        ]
        if len(audience_items) != 1:
            raise ValueError("SleepCare requires one requested audience")
        if audience_items[0].value != invocation.audience_role:
            raise ValueError("SleepCare audience binding mismatch")
        allowed = {
            "accepted:evidence_packet",
            "accepted:care_strategy",
            "accepted:safety_decision",
        }
        if any(
            item.key.startswith("accepted:") and item.key not in allowed
            for item in invocation.context.items
        ):
            raise ValueError("SleepCare received an unsupported work product")
        return self


class SleepCareInvocationOutput(RoleInvocationOutput):
    payload: CommunicationDraft

    @model_validator(mode="after")
    def validate_sleepcare_output(self) -> "SleepCareInvocationOutput":
        validate_role_output(
            self,
            agent_id=AgentId.SLEEP_CARE,
            payload_type=CommunicationDraft,
        )
        if self.payload != self.envelope.output_payload:
            raise ValueError("SleepCare payload differs from its envelope")
        return self


class SleepCarePlanContext(FrozenContract):
    episode_id: str = Field(..., min_length=1)
    episode_type: EpisodeType
    objective: str = Field(..., min_length=1, max_length=1200)
    fact_snapshot_id: str = Field(..., min_length=1)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    source_scope: SourceScope
    registry_required_work_products: tuple[WorkProductKind, ...]
    request_required_work_products: tuple[WorkProductKind, ...]
    allowed_work_products: tuple[WorkProductKind, ...]
    required_tools: tuple[str, ...]
    allowed_agents: tuple[AgentId, ...]
    available_safety_checkpoints: tuple[str, ...]
    request_required_safety_checkpoints: tuple[str, ...]
    exit_conditions: tuple[str, ...]
    budget: EpisodeBudget


class SleepCareEvaluationContext(FrozenContract):
    episode_id: str = Field(..., min_length=1)
    episode_state_revision: int = Field(..., ge=0)
    latest_kind: WorkProductKind
    latest_ref: str | None = Field(default=None, min_length=1)
    failure_code: str | None = Field(default=None, min_length=1)
    accepted_work_products: tuple[WorkProductKind, ...]
    required_work_products: tuple[WorkProductKind, ...]
    remaining_agent_calls: int
    remaining_replans: int


class _SleepCareDecisionInput(FrozenContract):
    episode_id: str = Field(..., min_length=1)
    episode_type: EpisodeType
    fact_snapshot: FactSnapshot
    episode_state_revision: int = Field(..., ge=0)
    invocation_ordinal: int = Field(..., ge=1)
    repair_attempt: int = Field(..., ge=0, le=1)
    previous_error_type: str | None = None


class SleepCarePlanInput(_SleepCareDecisionInput):
    runtime_context: SleepCarePlanContext

    @model_validator(mode="after")
    def validate_plan_binding(self) -> "SleepCarePlanInput":
        context = self.runtime_context
        if (
            context.episode_id != self.episode_id
            or context.episode_type is not self.episode_type
            or context.fact_snapshot_id != self.fact_snapshot.fact_snapshot_id
            or context.fact_snapshot_hash != self.fact_snapshot.fact_snapshot_hash
            or context.source_scope != self.fact_snapshot.source_scope
        ):
            raise ValueError("SleepCare plan Context binding mismatch")
        return self


class SleepCareEvaluationInput(_SleepCareDecisionInput):
    runtime_context: SleepCareEvaluationContext

    @model_validator(mode="after")
    def validate_evaluation_binding(self) -> "SleepCareEvaluationInput":
        context = self.runtime_context
        if (
            context.episode_id != self.episode_id
            or context.episode_state_revision != self.episode_state_revision
        ):
            raise ValueError("SleepCare evaluation Context binding mismatch")
        return self


class SleepCarePlanOutput(FrozenContract):
    proposal: EpisodePlanProposal
    record: AgentInvocationRecord


class SleepCareEvaluationOutput(FrozenContract):
    evaluation: SleepCareEvaluation
    record: AgentInvocationRecord


SLEEPCARE_CONTEXT_BOUNDARY = RoleContextBoundary(
    allowed_trust_labels=(
        TrustLabel.SYSTEM_POLICY,
        TrustLabel.ACCEPTED_WORK_PRODUCT,
        TrustLabel.CANONICAL_FACT,
        TrustLabel.USER_DATA,
        TrustLabel.USER_TEXT_UNTRUSTED,
        TrustLabel.TOOL_OUTPUT_UNTRUSTED,
        TrustLabel.RETRIEVED_KNOWLEDGE_UNTRUSTED,
        TrustLabel.USER_MEMORY_UNTRUSTED_DATA,
    ),
    allowed_context_keys=(
        "user_text",
        "revision_reason",
        "requested_audience_role",
        "collaboration_request",
        "runtime:cold_start_readiness",
    ),
    allowed_context_key_prefixes=(
        "accepted:",
        "tool:",
        "tool_receipt:",
        "user_fact_response:",
    ),
    visible_accepted_agents=(
        AgentId.EVIDENCE_REASONING,
        AgentId.CARE_STRATEGY,
        AgentId.SAFETY_REVIEW,
    ),
    visible_tool_receipts=tuple(
        sorted(
            {
                *TOOL_INVOCATION_ALLOWLIST[AgentId.SLEEP_CARE],
                "cold_start.evaluate",
            }
            - {"profile.read", "questionnaire.capture_profile"}
        )
    ),
    purpose_scoped_receipts=(
        PurposeScopedReceipt(
            tool_name="profile.read",
            purposes=("profile_review",),
        ),
    ),
    raw_user_text_visible=True,
    user_fact_responses_visible=True,
    audience_visible=True,
    memory_intent_visible=True,
)


SLEEPCARE_CONTROL_CONTEXT_BOUNDARY = RoleContextBoundary(
    allowed_trust_labels=(TrustLabel.SYSTEM_POLICY,),
    allowed_context_keys=("runtime_episode_context",),
    required_context_keys=("runtime_episode_context",),
)


DecisionT = TypeVar("DecisionT", bound=StrictContract)


class SleepCareAgent(
    _ModelBackedRole[SleepCareInvocationInput, SleepCareInvocationOutput]
):
    """Own Episode judgment, bounded coordination and final communication."""

    agent_id = AgentId.SLEEP_CARE
    input_contract = SleepCareInvocationInput
    output_contract = SleepCareInvocationOutput
    boundary = build_implementation_boundary(
        agent_id=agent_id,
        implementation=(
            "sleepagent.radar_agent.product_agent.agents.sleepcare.SleepCareAgent"
        ),
        input_contract="SleepCareInvocationInput",
        output_contract="SleepCareInvocationOutput",
        work_product_kind=WorkProductKind.COMMUNICATION,
        agent_version="sleep_care.v1",
        caller="runtime",
        success_conditions=(
            "propose a bounded Episode path and publish a grounded communication",
        ),
        waiting_conditions=(
            "request one necessary user fact or an exact confirmation target",
        ),
        failure_conditions=(
            "do not impersonate Evidence or Care; runtime may use only reviewed fallback",
        ),
        context=SLEEPCARE_CONTEXT_BOUNDARY,
        control_ports=(
            AgentControlPortBoundary(
                operation="plan",
                responsibility="propose a bounded Episode plan for Runtime validation",
                input_contract="SleepCarePlanInput",
                output_contract="SleepCarePlanOutput",
                payload_contract="EpisodePlanProposal",
                agent_version="sleepcare.v1",
                context=SLEEPCARE_CONTROL_CONTEXT_BOUNDARY,
            ),
            AgentControlPortBoundary(
                operation="evaluate",
                responsibility="judge progress without mutating Episode state",
                input_contract="SleepCareEvaluationInput",
                output_contract="SleepCareEvaluationOutput",
                payload_contract="SleepCareEvaluation",
                agent_version="sleepcare.v1",
                context=SLEEPCARE_CONTROL_CONTEXT_BOUNDARY,
            ),
        ),
    )

    def __init__(
        self,
        model: StructuredAgentModel,
        *,
        planning_model: StructuredAgentModel | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        super().__init__(model, skill_registry=skill_registry)
        self.planning_model = planning_model or model

    @staticmethod
    def select_skill(
        episode_type: EpisodeType,
        *,
        doctor_material: bool = False,
    ) -> str:
        if doctor_material:
            return "draft_doctor_material"
        if episode_type is EpisodeType.ROLE_MATERIAL:
            return "draft_user_material"
        if episode_type is EpisodeType.GROUNDED_DIALOGUE:
            return "answer_grounded_question"
        return "explain_for_elder"

    def bind(self, invocation: RuntimeRoleInvocation) -> SleepCareInvocationInput:
        return self._bind_role(invocation, SleepCareInvocationInput)

    def invoke(self, command: SleepCareInvocationInput) -> SleepCareInvocationOutput:
        if type(command) is not SleepCareInvocationInput:
            raise TypeError("SleepCareAgent requires SleepCareInvocationInput")
        envelope, record = self._invoke_model(command)
        if not isinstance(envelope.output_payload, CommunicationDraft):
            raise TypeError("SleepCareAgent returned a non-Communication payload")
        return SleepCareInvocationOutput(
            envelope=envelope,
            record=record,
            payload=envelope.output_payload,
        )

    def plan(self, command: SleepCarePlanInput) -> SleepCarePlanOutput:
        if type(command) is not SleepCarePlanInput:
            raise TypeError("SleepCareAgent.plan requires SleepCarePlanInput")
        proposal, record = self._invoke_decision(
            command=command,
            schema=EpisodePlanProposal,
            skill_id="plan_episode",
            prompt_version="sleepcare.plan.v1",
            invocation_kind="plan",
            safe_summary=f"planned {command.episode_type.value}",
        )
        return SleepCarePlanOutput(proposal=proposal, record=record)

    def evaluate(
        self,
        command: SleepCareEvaluationInput,
    ) -> SleepCareEvaluationOutput:
        if type(command) is not SleepCareEvaluationInput:
            raise TypeError(
                "SleepCareAgent.evaluate requires SleepCareEvaluationInput"
            )
        evaluation, record = self._invoke_decision(
            command=command,
            schema=SleepCareEvaluation,
            skill_id="evaluate_work_product",
            prompt_version="sleepcare.evaluate.v1",
            invocation_kind="evaluate",
        )
        return SleepCareEvaluationOutput(evaluation=evaluation, record=record)

    def _invoke_decision(
        self,
        *,
        command: SleepCarePlanInput | SleepCareEvaluationInput,
        schema: type[DecisionT],
        skill_id: str,
        prompt_version: str,
        invocation_kind: str,
        safe_summary: str | None = None,
    ) -> tuple[DecisionT, AgentInvocationRecord]:
        context = self._decision_context(command, skill_id=skill_id)
        bundle, lock = self.skill_resolver.resolve(
            episode_id=command.episode_id,
            episode_type=command.episode_type,
            agent_id=AgentId.SLEEP_CARE,
            mandatory_skill_ids=[skill_id],
            subject_id=command.fact_snapshot.binding.subject_id,
        )
        package = bundle.packages[0]
        compiled = self.prompt_compiler.compile(
            global_policy=(
                "Runtime owns Episode state, completion and budgets.",
                "SleepCare may propose plans but cannot skip required work.",
            ),
            profile=self.profile,
            bundle=bundle,
            context=context,
        )
        output = self.planning_model.generate(
            messages=list(compiled.messages),
            schema=schema,
            prompt_version=f"{prompt_version}:{package.version}",
            context_packet_id=context.context_packet_id,
        )
        if not isinstance(output, schema):
            raise TypeError(
                f"SleepCare model returned {type(output).__name__}, "
                f"expected {schema.__name__}"
            )
        now = datetime.now(timezone.utc)
        record = AgentInvocationRecord(
            invocation_id=(
                f"sleepcare:{invocation_kind}:{command.episode_id}:"
                f"{command.invocation_ordinal}"
            ),
            episode_id=command.episode_id,
            agent_id=AgentId.SLEEP_CARE,
            agent_version="sleepcare.v1",
            profile_version=self.profile.version,
            profile_hash=self.profile.profile_hash,
            skill_id=skill_id,
            skill_version=package.version,
            skill_package_hash=package.package_hash,
            skill_lock_hash=lock.lock_hash,
            prompt_bundle_hash=compiled.receipt.prompt_bundle_hash,
            schema_version=f"{schema.__name__}.v1",
            prompt_version=prompt_version,
            policy_version="product-safety.v3",
            context_packet_id=context.context_packet_id,
            context_hash=stable_hash(context),
            target_hash=stable_hash(
                {
                    "episode_id": command.episode_id,
                    "kind": invocation_kind,
                    "context": command.runtime_context.model_dump(mode="json"),
                    "output": output.model_dump(mode="json"),
                    "skill_lock_hash": lock.lock_hash,
                }
            ),
            provider=self.planning_model.provider,
            model_id=self.planning_model.model_id,
            provider_request_id=getattr(
                self.planning_model,
                "last_provider_request_id",
                None,
            ),
            started_at=now,
            ended_at=now,
            latency_ms=0,
            validation_status="runtime_validated",
            safe_summary=(safe_summary or getattr(output, "summary", skill_id))[:500],
        )
        return output, record

    def _decision_context(
        self,
        command: SleepCarePlanInput | SleepCareEvaluationInput,
        *,
        skill_id: str,
    ) -> ContextPacket:
        invocation_id = (
            f"sleepcare:{skill_id}:{command.episode_id}:"
            f"{command.invocation_ordinal}:{command.repair_attempt}"
        )
        runtime_context = command.runtime_context.model_dump(mode="json")
        packet = ContextPacket(
            context_packet_id=f"context:{invocation_id}",
            episode_id=command.episode_id,
            invocation_id=invocation_id,
            agent_id=AgentId.SLEEP_CARE,
            objective=str(runtime_context.get("objective", skill_id)),
            fact_snapshot_id=command.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=command.fact_snapshot.fact_snapshot_hash,
            episode_state_revision=command.episode_state_revision,
            care_context_version=command.fact_snapshot.care_context_version,
            source_scope=command.fact_snapshot.source_scope,
            authorization_scope=command.fact_snapshot.binding.authorization_scope,
            items=(
                TrustedContextItem(
                    key="runtime_episode_context",
                    trust_label=TrustLabel.SYSTEM_POLICY,
                    value={
                        **runtime_context,
                        "repair_attempt": command.repair_attempt,
                        "previous_error": command.previous_error_type,
                    },
                ),
            ),
        )
        boundary = SLEEPCARE_CONTROL_CONTEXT_BOUNDARY
        if (
            packet.agent_id is not AgentId.SLEEP_CARE
            or tuple(item.key for item in packet.items)
            != boundary.required_context_keys
            or any(
                item.trust_label not in boundary.allowed_trust_labels
                or not boundary.allows_context_key(item.key)
                for item in packet.items
            )
        ):
            raise ValueError("SleepCare control Context exceeds its boundary")
        return packet


__all__ = [
    "EpisodePlanProposal",
    "EvaluationDecision",
    "SLEEPCARE_CONTROL_CONTEXT_BOUNDARY",
    "SLEEPCARE_CONTEXT_BOUNDARY",
    "SleepCareAgent",
    "SleepCareEvaluation",
    "SleepCareEvaluationContext",
    "SleepCareEvaluationInput",
    "SleepCareEvaluationOutput",
    "SleepCareInvocationInput",
    "SleepCareInvocationOutput",
    "SleepCarePlanInput",
    "SleepCarePlanContext",
    "SleepCarePlanOutput",
]
