from __future__ import annotations

from pydantic import model_validator

from sleepagent.radar_agent.product_agent.agents.ports import (
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
    CareStrategy,
    EpisodeType,
    TrustLabel,
    WorkProductKind,
)
from sleepagent.radar_agent.product_agent.registry import (
    TOOL_INVOCATION_ALLOWLIST,
)


class CareStrategyInput(RoleInvocationInput):
    """Accepted Evidence and the minimum Care-only Context for one strategy."""

    @model_validator(mode="after")
    def validate_care_context(self) -> "CareStrategyInput":
        invocation = self.invocation
        if invocation.context.agent_id is not AgentId.CARE_STRATEGY:
            raise ValueError("CareStrategyInput requires Care Context")
        if not invocation.accepted_evidence_ref:
            raise ValueError("CareStrategyInput requires accepted Evidence")
        if invocation.review_target is not None or invocation.audience_role is not None:
            raise ValueError("Care input contains another role's binding")
        evidence_items = [
            item
            for item in invocation.context.items
            if item.key == "accepted:evidence_packet"
        ]
        if len(evidence_items) != 1:
            raise ValueError("Care requires exactly one accepted Evidence input")
        if invocation.accepted_evidence_ref not in evidence_items[0].source_refs:
            raise ValueError("Care Evidence reference is not present in Context")
        if any(
            item.key.startswith("accepted:")
            and item.key != "accepted:evidence_packet"
            for item in invocation.context.items
        ):
            raise ValueError("Care cannot read non-Evidence work products")
        return self


class CareStrategyOutput(RoleInvocationOutput):
    payload: CareStrategy

    @model_validator(mode="after")
    def validate_care_output(self) -> "CareStrategyOutput":
        validate_role_output(
            self,
            agent_id=AgentId.CARE_STRATEGY,
            payload_type=CareStrategy,
        )
        if self.payload != self.envelope.output_payload:
            raise ValueError("Care payload differs from its envelope")
        return self


CARE_CONTEXT_BOUNDARY = RoleContextBoundary(
    allowed_trust_labels=(
        TrustLabel.SYSTEM_POLICY,
        TrustLabel.ACCEPTED_WORK_PRODUCT,
        TrustLabel.TOOL_OUTPUT_UNTRUSTED,
        TrustLabel.RETRIEVED_KNOWLEDGE_UNTRUSTED,
    ),
    allowed_context_keys=("revision_reason", "collaboration_request"),
    allowed_context_key_prefixes=("accepted:", "tool:", "tool_receipt:"),
    required_context_keys=("accepted:evidence_packet",),
    visible_accepted_agents=(AgentId.EVIDENCE_REASONING,),
    visible_tool_receipts=tuple(
        sorted(TOOL_INVOCATION_ALLOWLIST[AgentId.CARE_STRATEGY])
    ),
)


class CareStrategyAgent(_ModelBackedRole[CareStrategyInput, CareStrategyOutput]):
    """Own the single-action Care lifecycle and bounded follow-up decisions."""

    agent_id = AgentId.CARE_STRATEGY
    input_contract = CareStrategyInput
    output_contract = CareStrategyOutput
    boundary = build_implementation_boundary(
        agent_id=agent_id,
        implementation=(
            "sleepagent.radar_agent.product_agent.agents."
            "care_strategy.CareStrategyAgent"
        ),
        input_contract="CareStrategyInput",
        output_contract="CareStrategyOutput",
        work_product_kind=WorkProductKind.CARE_STRATEGY,
        agent_version="care_strategy.v1",
        caller=AgentId.SLEEP_CARE,
        success_conditions=(
            "return zero or one primary Care action bound to accepted Evidence",
        ),
        waiting_conditions=(
            "request missing Evidence, one user fact, or required confirmation",
        ),
        failure_conditions=(
            "keep current Care state and do not invent a replacement action",
        ),
        context=CARE_CONTEXT_BOUNDARY,
    )

    @staticmethod
    def select_skill(
        episode_type: EpisodeType,
        *,
        doctor_material: bool = False,
    ) -> str:
        del doctor_material
        return (
            "assess_followup_outcome"
            if episode_type is EpisodeType.CARE_FOLLOWUP
            else "propose_single_care_action"
        )

    def bind(self, invocation: RuntimeRoleInvocation) -> CareStrategyInput:
        return self._bind_role(invocation, CareStrategyInput)

    def invoke(self, command: CareStrategyInput) -> CareStrategyOutput:
        if type(command) is not CareStrategyInput:
            raise TypeError("CareStrategyAgent requires CareStrategyInput")
        envelope, record = self._invoke_model(command)
        if not isinstance(envelope.output_payload, CareStrategy):
            raise TypeError("CareStrategyAgent returned a non-Care payload")
        return CareStrategyOutput(
            envelope=envelope,
            record=record,
            payload=envelope.output_payload,
        )


__all__ = [
    "CARE_CONTEXT_BOUNDARY",
    "CareStrategyAgent",
    "CareStrategyInput",
    "CareStrategyOutput",
]
