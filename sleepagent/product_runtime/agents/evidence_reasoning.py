from __future__ import annotations

from pydantic import model_validator

from sleepagent.product_runtime.agents.ports import (
    RoleContextBoundary,
    RoleInvocationInput,
    RoleInvocationOutput,
    RuntimeRoleInvocation,
    _ModelBackedRole,
    build_implementation_boundary,
    validate_role_output,
)
from sleepagent.product_runtime.contracts import (
    AgentId,
    EpisodeType,
    EvidencePacket,
    TrustLabel,
    WorkProductKind,
)
from sleepagent.product_runtime.registry import (
    TOOL_INVOCATION_ALLOWLIST,
)


class EvidenceReasoningInput(RoleInvocationInput):
    """Minimum Context from which personal Evidence may be formed."""

    @model_validator(mode="after")
    def validate_evidence_context(self) -> "EvidenceReasoningInput":
        invocation = self.invocation
        if invocation.context.agent_id is not AgentId.EVIDENCE_REASONING:
            raise ValueError("EvidenceReasoningInput requires Evidence Context")
        if any(
            (
                invocation.accepted_evidence_ref,
                invocation.review_target,
                invocation.audience_role,
            )
        ):
            raise ValueError("Evidence input contains another role's binding")
        if any(
            item.key.startswith("accepted:")
            and item.key != "accepted:evidence_packet"
            for item in invocation.context.items
        ):
            raise ValueError("Evidence cannot read another role's work product")
        return self


class EvidenceReasoningOutput(RoleInvocationOutput):
    payload: EvidencePacket

    @model_validator(mode="after")
    def validate_evidence_output(self) -> "EvidenceReasoningOutput":
        validate_role_output(
            self,
            agent_id=AgentId.EVIDENCE_REASONING,
            payload_type=EvidencePacket,
        )
        if self.payload != self.envelope.output_payload:
            raise ValueError("Evidence payload differs from its envelope")
        return self


EVIDENCE_CONTEXT_BOUNDARY = RoleContextBoundary(
    allowed_trust_labels=(
        TrustLabel.SYSTEM_POLICY,
        TrustLabel.ACCEPTED_WORK_PRODUCT,
        TrustLabel.CANONICAL_FACT,
        TrustLabel.USER_DATA,
        TrustLabel.USER_TEXT_UNTRUSTED,
        TrustLabel.TOOL_OUTPUT_UNTRUSTED,
        TrustLabel.RETRIEVED_KNOWLEDGE_UNTRUSTED,
        TrustLabel.EPISODIC_HINT_UNTRUSTED,
        TrustLabel.USER_MEMORY_UNTRUSTED_DATA,
    ),
    allowed_context_keys=(
        "user_text",
        "revision_reason",
        "collaboration_request",
        "runtime:cold_start_readiness",
    ),
    allowed_context_key_prefixes=(
        "accepted:",
        "tool:",
        "tool_receipt:",
        "user_fact_response:",
    ),
    visible_accepted_agents=(AgentId.EVIDENCE_REASONING,),
    visible_tool_receipts=tuple(
        sorted(
            {
                *TOOL_INVOCATION_ALLOWLIST[AgentId.EVIDENCE_REASONING],
                "cold_start.evaluate",
                "questionnaire.capture_profile",
            }
        )
    ),
    raw_user_text_visible=True,
    user_fact_responses_visible=True,
)


class EvidenceReasoningAgent(
    _ModelBackedRole[EvidenceReasoningInput, EvidenceReasoningOutput]
):
    """Own personal Evidence claims, data-quality judgment and uncertainty."""

    agent_id = AgentId.EVIDENCE_REASONING
    input_contract = EvidenceReasoningInput
    output_contract = EvidenceReasoningOutput
    boundary = build_implementation_boundary(
        agent_id=agent_id,
        implementation=(
            "sleepagent.product_runtime.agents."
            "evidence_reasoning.EvidenceReasoningAgent"
        ),
        input_contract="EvidenceReasoningInput",
        output_contract="EvidenceReasoningOutput",
        work_product_kind=WorkProductKind.EVIDENCE_PACKET,
        agent_version="evidence_reasoning.v1",
        caller=AgentId.SLEEP_CARE,
        success_conditions=(
            "return a source-bound EvidencePacket with explicit uncertainty",
        ),
        waiting_conditions=(
            "request one necessary user fact or missing evidence source",
        ),
        failure_conditions=(
            "emit no new personal claim when evidence is unavailable or invalid",
        ),
        context=EVIDENCE_CONTEXT_BOUNDARY,
    )

    @staticmethod
    def select_skill(
        episode_type: EpisodeType,
        *,
        doctor_material: bool = False,
    ) -> str:
        del doctor_material
        return (
            "interpret_longitudinal_pattern"
            if episode_type is EpisodeType.TREND_REVIEW
            else "interpret_scoped_evidence"
        )

    def bind(self, invocation: RuntimeRoleInvocation) -> EvidenceReasoningInput:
        return self._bind_role(invocation, EvidenceReasoningInput)

    def invoke(self, command: EvidenceReasoningInput) -> EvidenceReasoningOutput:
        if type(command) is not EvidenceReasoningInput:
            raise TypeError("EvidenceReasoningAgent requires EvidenceReasoningInput")
        envelope, record = self._invoke_model(command)
        if not isinstance(envelope.output_payload, EvidencePacket):
            raise TypeError("EvidenceReasoningAgent returned a non-Evidence payload")
        return EvidenceReasoningOutput(
            envelope=envelope,
            record=record,
            payload=envelope.output_payload,
        )


__all__ = [
    "EVIDENCE_CONTEXT_BOUNDARY",
    "EvidenceReasoningAgent",
    "EvidenceReasoningInput",
    "EvidenceReasoningOutput",
]
