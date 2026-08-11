from __future__ import annotations

from pydantic import model_validator

from sleepagent.product_runtime.agents.ports import (
    AgentControlPortBoundary,
    EpisodePlanProposal,
    EvaluationDecision,
    PurposeScopedReceipt,
    RoleContextBoundary,
    RoleInvocationInput,
    RoleInvocationOutput,
    RuntimeRoleInvocation,
    SLEEPCARE_CONTROL_CONTEXT_BOUNDARY,
    SleepCareControlInvocationPort,
    SleepCareEvaluation,
    SleepCareEvaluationContext,
    SleepCareEvaluationInput,
    SleepCareEvaluationOutput,
    SleepCarePlanContext,
    SleepCarePlanInput,
    SleepCarePlanOutput,
    _ModelBackedRole,
    build_implementation_boundary,
    validate_role_output,
)
from sleepagent.product_runtime.contracts import (
    AgentId,
    CommunicationDraft,
    EpisodeType,
    TrustLabel,
    WorkProductKind,
)
from sleepagent.product_runtime.invocation import StructuredAgentModel
from sleepagent.product_runtime.registry import (
    TOOL_INVOCATION_ALLOWLIST,
)
from sleepagent.product_runtime.skills import (
    SkillRegistry,
)


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
            "sleepagent.product_runtime.agents.sleepcare.SleepCareAgent"
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
        self._control_invoker: SleepCareControlInvocationPort | None = None

    @property
    def control_invoker(self) -> SleepCareControlInvocationPort | None:
        return self._control_invoker

    def bind_control_invoker(
        self,
        invoker: SleepCareControlInvocationPort,
    ) -> None:
        if self._control_invoker is not None and self._control_invoker is not invoker:
            raise RuntimeError(
                "SleepCareAgent is already bound to another control coordinator"
            )
        self._control_invoker = invoker

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
        if self._control_invoker is None:
            raise RuntimeError(
                "SleepCareAgent control calls require AgentInvocationCoordinator"
            )
        return self._control_invoker.invoke_sleepcare_plan(
            command=command,
            model=self.planning_model,
        )

    def evaluate(
        self,
        command: SleepCareEvaluationInput,
    ) -> SleepCareEvaluationOutput:
        if type(command) is not SleepCareEvaluationInput:
            raise TypeError(
                "SleepCareAgent.evaluate requires SleepCareEvaluationInput"
            )
        if self._control_invoker is None:
            raise RuntimeError(
                "SleepCareAgent control calls require AgentInvocationCoordinator"
            )
        return self._control_invoker.invoke_sleepcare_evaluation(
            command=command,
            model=self.planning_model,
        )


__all__ = [
    "EpisodePlanProposal",
    "EvaluationDecision",
    "SLEEPCARE_CONTROL_CONTEXT_BOUNDARY",
    "SLEEPCARE_CONTEXT_BOUNDARY",
    "SleepCareAgent",
    "SleepCareControlInvocationPort",
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
