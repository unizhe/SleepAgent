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
    SafetyDecision,
    TrustLabel,
    WorkProductKind,
)
from sleepagent.product_runtime.registry import (
    TOOL_INVOCATION_ALLOWLIST,
)


class SafetyReviewInput(RoleInvocationInput):
    """One exact, hash-bound review target and its deterministic policy Context."""

    @model_validator(mode="after")
    def validate_safety_context(self) -> "SafetyReviewInput":
        invocation = self.invocation
        if invocation.context.agent_id is not AgentId.SAFETY_REVIEW:
            raise ValueError("SafetyReviewInput requires Safety Context")
        if invocation.review_target is None:
            raise ValueError("SafetyReviewInput requires an exact review target")
        if invocation.accepted_evidence_ref or invocation.audience_role:
            raise ValueError("Safety input contains another role's binding")
        target_items = [
            item
            for item in invocation.context.items
            if item.key == "safety_review_target"
        ]
        if len(target_items) != 1:
            raise ValueError("Safety requires exactly one review target")
        target = invocation.review_target
        item = target_items[0]
        if target.work_product_ref not in item.source_refs:
            raise ValueError("Safety target reference is absent from Context")
        if not isinstance(item.value, dict):
            raise ValueError("Safety target Context must be structured")
        actual = (
            item.value.get("target_id"),
            item.value.get("target_hash"),
            item.value.get("episode_state_revision"),
        )
        expected = (
            target.target_id,
            target.target_hash,
            target.episode_state_revision,
        )
        if actual != expected:
            raise ValueError("Safety target binding does not match Context")
        return self


class SafetyReviewOutput(RoleInvocationOutput):
    payload: SafetyDecision

    @model_validator(mode="after")
    def validate_safety_output(self) -> "SafetyReviewOutput":
        validate_role_output(
            self,
            agent_id=AgentId.SAFETY_REVIEW,
            payload_type=SafetyDecision,
        )
        if self.payload != self.envelope.output_payload:
            raise ValueError("Safety payload differs from its envelope")
        return self


SAFETY_CONTEXT_BOUNDARY = RoleContextBoundary(
    allowed_trust_labels=(
        TrustLabel.SYSTEM_POLICY,
        TrustLabel.ACCEPTED_WORK_PRODUCT,
        TrustLabel.CANONICAL_FACT,
        TrustLabel.TOOL_OUTPUT_UNTRUSTED,
    ),
    allowed_context_keys=(
        "revision_reason",
        "collaboration_request",
        "safety_review_target",
        "runtime:cold_start_readiness",
    ),
    allowed_context_key_prefixes=("tool:", "tool_receipt:"),
    required_context_keys=("safety_review_target",),
    visible_tool_receipts=tuple(
        sorted(
            {
                *TOOL_INVOCATION_ALLOWLIST[AgentId.SAFETY_REVIEW],
                "cold_start.evaluate",
            }
        )
    ),
)


class SafetyReviewAgent(_ModelBackedRole[SafetyReviewInput, SafetyReviewOutput]):
    """Own target-bound approve, revise or block judgment; never execution."""

    agent_id = AgentId.SAFETY_REVIEW
    input_contract = SafetyReviewInput
    output_contract = SafetyReviewOutput
    boundary = build_implementation_boundary(
        agent_id=agent_id,
        implementation=(
            "sleepagent.product_runtime.agents."
            "safety_review.SafetyReviewAgent"
        ),
        input_contract="SafetyReviewInput",
        output_contract="SafetyReviewOutput",
        work_product_kind=WorkProductKind.SAFETY_DECISION,
        agent_version="safety_review.v1",
        caller=AgentId.SLEEP_CARE,
        success_conditions=(
            "approve, revise, or block only the exact hash-bound review target",
        ),
        waiting_conditions=(
            "no ordinary waiting path; a missing exact target is rejected",
        ),
        failure_conditions=(
            "block the target when mandatory review fails or times out",
        ),
        context=SAFETY_CONTEXT_BOUNDARY,
    )

    @staticmethod
    def select_skill(
        episode_type: EpisodeType,
        *,
        doctor_material: bool = False,
    ) -> str:
        del episode_type, doctor_material
        return "review_action_and_publication"

    def bind(self, invocation: RuntimeRoleInvocation) -> SafetyReviewInput:
        return self._bind_role(invocation, SafetyReviewInput)

    def invoke(self, command: SafetyReviewInput) -> SafetyReviewOutput:
        if type(command) is not SafetyReviewInput:
            raise TypeError("SafetyReviewAgent requires SafetyReviewInput")
        envelope, record = self._invoke_model(command)
        if not isinstance(envelope.output_payload, SafetyDecision):
            raise TypeError("SafetyReviewAgent returned a non-Safety payload")
        return SafetyReviewOutput(
            envelope=envelope,
            record=record,
            payload=envelope.output_payload,
        )


__all__ = [
    "SAFETY_CONTEXT_BOUNDARY",
    "SafetyReviewAgent",
    "SafetyReviewInput",
    "SafetyReviewOutput",
]
