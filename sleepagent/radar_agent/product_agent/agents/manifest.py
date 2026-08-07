from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sleepagent.radar_agent.product_agent.agents.care_strategy import (
    CareStrategyAgent,
)
from sleepagent.radar_agent.product_agent.agents.evidence_reasoning import (
    EvidenceReasoningAgent,
)
from sleepagent.radar_agent.product_agent.agents.ports import (
    AgentControlPortBoundary,
    AgentImplementationBoundary,
    PurposeScopedReceipt,
    RoleContextBoundary,
)
from sleepagent.radar_agent.product_agent.agents.safety_review import (
    SafetyReviewAgent,
)
from sleepagent.radar_agent.product_agent.agents.sleepcare import (
    SleepCareAgent,
)
from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    PRODUCT_AGENT_ROSTER,
    TrustLabel,
)
from sleepagent.radar_agent.product_agent.invocation import (
    MODEL_OUTPUT_BY_AGENT,
    StructuredAgentModel,
)
from sleepagent.radar_agent.product_agent.registry import (
    AGENT_DEFINITIONS,
    COLLABORATION_ALLOWLIST,
    TOOL_INVOCATION_ALLOWLIST,
)
from sleepagent.radar_agent.product_agent.skills import (
    SkillRegistry,
    default_skill_packages,
)


IMPLEMENTATION_MANIFEST_VERSION = "sleepagent-product-agent-implementation.v1"


_EXPECTED_CONTEXT_BOUNDARIES = {
    AgentId.SLEEP_CARE: RoleContextBoundary(
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
    ),
    AgentId.EVIDENCE_REASONING: RoleContextBoundary(
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
    ),
    AgentId.CARE_STRATEGY: RoleContextBoundary(
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
    ),
    AgentId.SAFETY_REVIEW: RoleContextBoundary(
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
    ),
}

_SLEEPCARE_CONTROL_CONTEXT = RoleContextBoundary(
    allowed_trust_labels=(TrustLabel.SYSTEM_POLICY,),
    allowed_context_keys=("runtime_episode_context",),
    required_context_keys=("runtime_episode_context",),
)

_EXPECTED_CONTROL_PORTS = {
    AgentId.SLEEP_CARE: (
        AgentControlPortBoundary(
            operation="plan",
            responsibility="propose a bounded Episode plan for Runtime validation",
            input_contract="SleepCarePlanInput",
            output_contract="SleepCarePlanOutput",
            payload_contract="EpisodePlanProposal",
            agent_version="sleepcare.v1",
            context=_SLEEPCARE_CONTROL_CONTEXT,
        ),
        AgentControlPortBoundary(
            operation="evaluate",
            responsibility="judge progress without mutating Episode state",
            input_contract="SleepCareEvaluationInput",
            output_contract="SleepCareEvaluationOutput",
            payload_contract="SleepCareEvaluation",
            agent_version="sleepcare.v1",
            context=_SLEEPCARE_CONTROL_CONTEXT,
        ),
    ),
    AgentId.EVIDENCE_REASONING: (),
    AgentId.CARE_STRATEGY: (),
    AgentId.SAFETY_REVIEW: (),
}


class ConcreteAgentManifestError(RuntimeError):
    pass


AGENT_IMPLEMENTATIONS: Mapping[AgentId, type] = MappingProxyType(
    {
        AgentId.SLEEP_CARE: SleepCareAgent,
        AgentId.EVIDENCE_REASONING: EvidenceReasoningAgent,
        AgentId.CARE_STRATEGY: CareStrategyAgent,
        AgentId.SAFETY_REVIEW: SafetyReviewAgent,
    }
)


def validate_concrete_agent_manifest() -> None:
    keys = tuple(AGENT_IMPLEMENTATIONS)
    if keys != PRODUCT_AGENT_ROSTER or any(type(item) is not AgentId for item in keys):
        raise ConcreteAgentManifestError(
            "concrete Agent implementations must match the frozen roster"
        )
    expected_types = (
        SleepCareAgent,
        EvidenceReasoningAgent,
        CareStrategyAgent,
        SafetyReviewAgent,
    )
    if tuple(AGENT_IMPLEMENTATIONS.values()) != expected_types:
        raise ConcreteAgentManifestError("concrete Agent class mapping drifted")
    if len(set(expected_types)) != len(PRODUCT_AGENT_ROSTER):
        raise ConcreteAgentManifestError("one class cannot implement two Agent roles")
    if tuple(MODEL_OUTPUT_BY_AGENT) != PRODUCT_AGENT_ROSTER:
        raise ConcreteAgentManifestError("bound model output roster drifted")

    skills_by_owner = {
        agent_id: tuple(
            item.skill_id
            for item in default_skill_packages()
            if item.owner_agent is agent_id
        )
        for agent_id in PRODUCT_AGENT_ROSTER
    }
    work_product_by_agent = {
        AgentId.SLEEP_CARE: "communication",
        AgentId.EVIDENCE_REASONING: "evidence_packet",
        AgentId.CARE_STRATEGY: "care_strategy",
        AgentId.SAFETY_REVIEW: "safety_decision",
    }
    for agent_id, implementation in AGENT_IMPLEMENTATIONS.items():
        boundary: AgentImplementationBoundary = implementation.boundary
        definition = AGENT_DEFINITIONS[agent_id]
        if boundary.agent_id is not agent_id:
            raise ConcreteAgentManifestError("concrete Agent identity mismatch")
        expected_path = f"{implementation.__module__}.{implementation.__name__}"
        if boundary.implementation != expected_path:
            raise ConcreteAgentManifestError("concrete Agent implementation path drifted")
        if implementation.input_contract.__name__ != boundary.input_contract:
            raise ConcreteAgentManifestError("concrete Agent input Contract drifted")
        if implementation.output_contract.__name__ != boundary.output_contract:
            raise ConcreteAgentManifestError("concrete Agent output Contract drifted")
        if boundary.payload_contract != definition.payload_schema:
            raise ConcreteAgentManifestError("concrete Agent payload Contract drifted")
        payload_annotation = MODEL_OUTPUT_BY_AGENT[agent_id].model_fields[
            "output_payload"
        ].annotation
        if getattr(payload_annotation, "__name__", None) != definition.payload_schema:
            raise ConcreteAgentManifestError("bound model output Contract drifted")
        if boundary.work_product_kind.value != work_product_by_agent[agent_id]:
            raise ConcreteAgentManifestError("concrete Agent work-product kind drifted")
        expected_caller: AgentId | str = (
            "runtime"
            if agent_id is AgentId.SLEEP_CARE
            else AgentId.SLEEP_CARE
        )
        if (
            boundary.caller != expected_caller
            or type(boundary.caller) is not type(expected_caller)
        ):
            raise ConcreteAgentManifestError("concrete Agent caller topology drifted")
        if boundary.allowed_tools != tuple(
            sorted(TOOL_INVOCATION_ALLOWLIST[agent_id])
        ):
            raise ConcreteAgentManifestError("concrete Agent Tool boundary drifted")
        if boundary.allowed_skill_ids != skills_by_owner[agent_id]:
            raise ConcreteAgentManifestError("concrete Agent Skill boundary drifted")
        expected_collaboration = {
            (receiver, tuple(sorted(requests, key=lambda item: item.value)))
            for (sender, receiver), requests in COLLABORATION_ALLOWLIST.items()
            if sender is agent_id
        }
        actual_collaboration = {
            (item.receiver, item.request_types)
            for item in boundary.collaboration_permissions
        }
        if actual_collaboration != expected_collaboration:
            raise ConcreteAgentManifestError(
                "concrete Agent collaboration boundary drifted"
            )
        permissions = (
            boundary.may_publish,
            boundary.may_mutate_shared_state,
            boundary.may_execute_side_effects,
        )
        expected_permissions = (
            definition.may_publish,
            definition.may_mutate_shared_state,
            definition.may_execute_side_effects,
        )
        if permissions != expected_permissions:
            raise ConcreteAgentManifestError("concrete Agent permissions drifted")
        if boundary.context != _EXPECTED_CONTEXT_BOUNDARIES[agent_id]:
            raise ConcreteAgentManifestError("concrete Agent Context boundary drifted")
        if boundary.control_ports != _EXPECTED_CONTROL_PORTS[agent_id]:
            raise ConcreteAgentManifestError("concrete Agent control port drifted")
        if any(
            type(item) is not AgentId
            or item not in PRODUCT_AGENT_ROSTER
            for item in boundary.context.visible_accepted_agents
        ):
            raise ConcreteAgentManifestError("concrete Context names an unknown Agent")


@dataclass(frozen=True)
class ProductAgentRoster:
    sleepcare: SleepCareAgent
    evidence_reasoning: EvidenceReasoningAgent
    care_strategy: CareStrategyAgent
    safety_review: SafetyReviewAgent

    def __post_init__(self) -> None:
        expected = (
            (self.sleepcare, SleepCareAgent, AgentId.SLEEP_CARE),
            (
                self.evidence_reasoning,
                EvidenceReasoningAgent,
                AgentId.EVIDENCE_REASONING,
            ),
            (self.care_strategy, CareStrategyAgent, AgentId.CARE_STRATEGY),
            (self.safety_review, SafetyReviewAgent, AgentId.SAFETY_REVIEW),
        )
        for instance, implementation, agent_id in expected:
            if type(instance) is not implementation:
                raise TypeError(
                    f"{agent_id.value} requires {implementation.__name__}"
                )
            if instance.agent_id is not agent_id:
                raise TypeError("concrete Agent roster identity mismatch")

    def by_id(
        self,
        agent_id: AgentId,
    ) -> SleepCareAgent | EvidenceReasoningAgent | CareStrategyAgent | SafetyReviewAgent:
        if type(agent_id) is not AgentId:
            raise TypeError("Agent roster lookup requires an exact AgentId")
        return self.as_mapping()[agent_id]

    def as_mapping(
        self,
    ) -> Mapping[
        AgentId,
        SleepCareAgent
        | EvidenceReasoningAgent
        | CareStrategyAgent
        | SafetyReviewAgent,
    ]:
        return MappingProxyType(
            {
                AgentId.SLEEP_CARE: self.sleepcare,
                AgentId.EVIDENCE_REASONING: self.evidence_reasoning,
                AgentId.CARE_STRATEGY: self.care_strategy,
                AgentId.SAFETY_REVIEW: self.safety_review,
            }
        )

    def __iter__(
        self,
    ) -> Iterator[
        SleepCareAgent
        | EvidenceReasoningAgent
        | CareStrategyAgent
        | SafetyReviewAgent
    ]:
        return iter(self.as_mapping().values())


class ProductAgentFactory:
    @staticmethod
    def create(
        *,
        sleepcare_model: StructuredAgentModel,
        evidence_reasoning_model: StructuredAgentModel,
        care_strategy_model: StructuredAgentModel,
        safety_review_model: StructuredAgentModel,
        sleepcare_planning_model: StructuredAgentModel | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> ProductAgentRoster:
        validate_concrete_agent_manifest()
        return ProductAgentRoster(
            sleepcare=SleepCareAgent(
                sleepcare_model,
                planning_model=sleepcare_planning_model,
                skill_registry=skill_registry,
            ),
            evidence_reasoning=EvidenceReasoningAgent(
                evidence_reasoning_model,
                skill_registry=skill_registry,
            ),
            care_strategy=CareStrategyAgent(
                care_strategy_model,
                skill_registry=skill_registry,
            ),
            safety_review=SafetyReviewAgent(
                safety_review_model,
                skill_registry=skill_registry,
            ),
        )

    @staticmethod
    def from_models(
        models: Mapping[AgentId, StructuredAgentModel],
        *,
        sleepcare_planning_model: StructuredAgentModel | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> ProductAgentRoster:
        keys = tuple(models)
        if (
            len(keys) != len(PRODUCT_AGENT_ROSTER)
            or any(type(item) is not AgentId for item in keys)
            or frozenset(keys) != frozenset(PRODUCT_AGENT_ROSTER)
        ):
            raise ValueError("model bindings must match the exact typed Agent roster")
        return ProductAgentFactory.create(
            sleepcare_model=models[AgentId.SLEEP_CARE],
            evidence_reasoning_model=models[AgentId.EVIDENCE_REASONING],
            care_strategy_model=models[AgentId.CARE_STRATEGY],
            safety_review_model=models[AgentId.SAFETY_REVIEW],
            sleepcare_planning_model=sleepcare_planning_model,
            skill_registry=skill_registry,
        )


def concrete_agent_manifest() -> dict[str, object]:
    validate_concrete_agent_manifest()
    return {
        "implementation_manifest_version": IMPLEMENTATION_MANIFEST_VERSION,
        "agents": {
            agent_id.value: implementation.boundary.model_dump(mode="json")
            for agent_id, implementation in AGENT_IMPLEMENTATIONS.items()
        },
    }


validate_concrete_agent_manifest()


__all__ = [
    "AGENT_IMPLEMENTATIONS",
    "IMPLEMENTATION_MANIFEST_VERSION",
    "ConcreteAgentManifestError",
    "ProductAgentFactory",
    "ProductAgentRoster",
    "concrete_agent_manifest",
    "validate_concrete_agent_manifest",
]
