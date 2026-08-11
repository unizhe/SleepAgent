from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, ClassVar, Generic, Literal, Protocol, TypeVar, cast

from pydantic import Field, model_validator

from sleepagent.radar_agent.product_agent.contracts import (
    AgentEnvelope,
    AgentId,
    ContextPacket,
    CrossAgentRequest,
    CrossAgentRequestType,
    EpisodeBudget,
    EpisodeType,
    FactSnapshot,
    FrozenContract,
    SourceScope,
    StrictContract,
    ToolReceipt,
    TrustLabel,
    WorkProductKind,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.invocation import (
    AgentInvocationRecord,
    ProductAgentInvoker,
    StructuredAgentModel,
)
from sleepagent.radar_agent.product_agent.governance import (
    PRODUCT_SAFETY_POLICY_VERSION,
)
from sleepagent.radar_agent.product_agent.registry import (
    AGENT_DEFINITIONS,
    COLLABORATION_ALLOWLIST,
    TOOL_INVOCATION_ALLOWLIST,
)
from sleepagent.radar_agent.product_agent.skills import (
    SkillRegistry,
    default_agent_profiles,
    default_skill_packages,
)


AudienceRole = Literal["elder", "family", "doctor"]
AgentCaller = AgentId | Literal["runtime"]


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


class SleepCareControlInvocationPort(Protocol):
    """Runtime-owned control-call seam used by the SleepCare role."""

    def invoke_sleepcare_plan(
        self,
        *,
        command: SleepCarePlanInput,
        model: StructuredAgentModel,
    ) -> SleepCarePlanOutput: ...

    def invoke_sleepcare_evaluation(
        self,
        *,
        command: SleepCareEvaluationInput,
        model: StructuredAgentModel,
    ) -> SleepCareEvaluationOutput: ...


class ReviewTargetBinding(FrozenContract):
    work_product_ref: str = Field(..., min_length=1)
    target_id: str = Field(..., min_length=1)
    target_hash: str = Field(..., min_length=64, max_length=64)
    episode_state_revision: int = Field(..., ge=0)


class RuntimeRoleInvocation(FrozenContract):
    """Runtime-owned transport that a concrete role binds to its own Contract."""

    context: ContextPacket
    episode_type: EpisodeType
    subject_id: str = Field(..., min_length=1)
    doctor_material: bool = False
    parent_invocation_id: str | None = None
    target_id: str = Field(..., min_length=1)
    target_hash_material: dict[str, Any]
    skill_id: str = Field(..., min_length=1)
    skill_version: str = Field(..., min_length=1)
    prompt_version: str = Field(..., min_length=1)
    policy_version: str = Field(..., min_length=1)
    profile_version: str = Field(..., min_length=1)
    profile_hash: str = Field(..., min_length=64, max_length=64)
    skill_package_hash: str = Field(..., min_length=64, max_length=64)
    skill_lock_hash: str = Field(..., min_length=64, max_length=64)
    prompt_bundle_hash: str = Field(..., min_length=64, max_length=64)
    compiled_messages: tuple[dict[str, str], ...] = Field(min_length=1)
    profile_purpose: str | None = None
    accepted_evidence_ref: str | None = Field(default=None, min_length=1)
    review_target: ReviewTargetBinding | None = None
    audience_role: AudienceRole | None = None


class RoleInvocationInput(FrozenContract):
    invocation: RuntimeRoleInvocation
    runtime_binding_hash: str = Field(..., min_length=64, max_length=64)


class RoleInvocationOutput(FrozenContract):
    envelope: AgentEnvelope
    record: AgentInvocationRecord


class CollaborationPermission(FrozenContract):
    receiver: AgentId
    request_types: tuple[CrossAgentRequestType, ...]


class PurposeScopedReceipt(FrozenContract):
    tool_name: str = Field(..., min_length=1)
    purposes: tuple[str, ...] = Field(min_length=1)


class RoleContextBoundary(FrozenContract):
    allowed_trust_labels: tuple[TrustLabel, ...]
    allowed_context_keys: tuple[str, ...] = ()
    allowed_context_key_prefixes: tuple[str, ...] = ()
    required_context_keys: tuple[str, ...] = ()
    visible_accepted_agents: tuple[AgentId, ...] = ()
    visible_tool_receipts: tuple[str, ...] = ()
    purpose_scoped_receipts: tuple[PurposeScopedReceipt, ...] = ()
    raw_user_text_visible: bool = False
    user_fact_responses_visible: bool = False
    audience_visible: bool = False
    memory_intent_visible: bool = False

    def allows_context_key(self, key: str) -> bool:
        return key in self.allowed_context_keys or any(
            key.startswith(prefix) for prefix in self.allowed_context_key_prefixes
        )

    def allows_tool_receipt(
        self,
        receipt: ToolReceipt,
        *,
        agent_id: AgentId,
        profile_purpose: str | None,
    ) -> bool:
        if receipt.tool_name == "runtime.build_fact_snapshot":
            return False
        if receipt.tool_name.startswith("memory.") and (
            receipt.caller != agent_id.value
        ):
            return False
        return self.allows_tool_name(
            receipt.tool_name,
            profile_purpose=profile_purpose,
        )

    def allows_tool_name(
        self,
        tool_name: str,
        *,
        profile_purpose: str | None,
    ) -> bool:
        scoped = next(
            (
                item
                for item in self.purpose_scoped_receipts
                if item.tool_name == tool_name
            ),
            None,
        )
        if scoped is not None:
            return profile_purpose in scoped.purposes
        return tool_name in self.visible_tool_receipts


SLEEPCARE_CONTROL_CONTEXT_BOUNDARY = RoleContextBoundary(
    allowed_trust_labels=(TrustLabel.SYSTEM_POLICY,),
    allowed_context_keys=("runtime_episode_context",),
    required_context_keys=("runtime_episode_context",),
)


class AgentControlPortBoundary(FrozenContract):
    operation: str = Field(..., min_length=1)
    responsibility: str = Field(..., min_length=1)
    input_contract: str = Field(..., min_length=1)
    output_contract: str = Field(..., min_length=1)
    payload_contract: str = Field(..., min_length=1)
    agent_version: str = Field(..., min_length=1)
    context: RoleContextBoundary


class AgentImplementationBoundary(FrozenContract):
    agent_id: AgentId
    implementation: str = Field(..., min_length=1)
    responsibility: str = Field(..., min_length=1)
    input_contract: str = Field(..., min_length=1)
    output_contract: str = Field(..., min_length=1)
    payload_contract: str = Field(..., min_length=1)
    work_product_kind: WorkProductKind
    agent_version: str = Field(..., min_length=1)
    caller: AgentCaller
    success_conditions: tuple[str, ...] = Field(min_length=1)
    waiting_conditions: tuple[str, ...] = ()
    failure_conditions: tuple[str, ...] = Field(min_length=1)
    control_ports: tuple[AgentControlPortBoundary, ...] = ()
    allowed_skill_ids: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    collaboration_permissions: tuple[CollaborationPermission, ...]
    context: RoleContextBoundary
    may_publish: bool
    may_mutate_shared_state: bool
    may_execute_side_effects: bool
    profile_version: str = Field(..., min_length=1)
    profile_hash: str = Field(..., min_length=64, max_length=64)


def build_implementation_boundary(
    *,
    agent_id: AgentId,
    implementation: str,
    input_contract: str,
    output_contract: str,
    work_product_kind: WorkProductKind,
    agent_version: str,
    caller: AgentCaller,
    success_conditions: tuple[str, ...],
    waiting_conditions: tuple[str, ...],
    failure_conditions: tuple[str, ...],
    context: RoleContextBoundary,
    control_ports: tuple[AgentControlPortBoundary, ...] = (),
) -> AgentImplementationBoundary:
    definition = AGENT_DEFINITIONS[agent_id]
    profile = default_agent_profiles()[agent_id]
    skills = tuple(
        item.skill_id
        for item in default_skill_packages()
        if item.owner_agent is agent_id
    )
    collaboration = tuple(
        CollaborationPermission(
            receiver=receiver,
            request_types=tuple(sorted(request_types, key=lambda item: item.value)),
        )
        for (sender, receiver), request_types in COLLABORATION_ALLOWLIST.items()
        if sender is agent_id
    )
    if not set(context.allowed_trust_labels).issubset(
        set(profile.allowed_context_labels)
    ):
        raise ValueError("concrete Agent Context exceeds its frozen AgentProfile")
    return AgentImplementationBoundary(
        agent_id=agent_id,
        implementation=implementation,
        responsibility=definition.responsibility,
        input_contract=input_contract,
        output_contract=output_contract,
        payload_contract=definition.payload_schema,
        work_product_kind=work_product_kind,
        agent_version=agent_version,
        caller=caller,
        success_conditions=success_conditions,
        waiting_conditions=waiting_conditions,
        failure_conditions=failure_conditions,
        control_ports=control_ports,
        allowed_skill_ids=skills,
        allowed_tools=tuple(sorted(TOOL_INVOCATION_ALLOWLIST[agent_id])),
        collaboration_permissions=collaboration,
        context=context,
        may_publish=definition.may_publish,
        may_mutate_shared_state=definition.may_mutate_shared_state,
        may_execute_side_effects=definition.may_execute_side_effects,
        profile_version=profile.version,
        profile_hash=profile.profile_hash,
    )


InputT = TypeVar("InputT", bound=RoleInvocationInput)
OutputT = TypeVar("OutputT", bound=RoleInvocationOutput)


class TypedAgentPort(Protocol, Generic[InputT, OutputT]):
    agent_id: AgentId
    boundary: AgentImplementationBoundary
    model: StructuredAgentModel
    skill_registry: SkillRegistry

    def bind(self, invocation: RuntimeRoleInvocation) -> InputT: ...

    def invoke(self, command: InputT) -> OutputT: ...

    def invoke_bound(self, command: RoleInvocationInput) -> RoleInvocationOutput: ...

    def select_skill(
        self,
        episode_type: EpisodeType,
        *,
        doctor_material: bool = False,
    ) -> str: ...


class RuntimeAgentPort(Protocol):
    """Common typed seam used by Runtime after choosing a concrete role."""

    agent_id: ClassVar[AgentId]
    boundary: ClassVar[AgentImplementationBoundary]
    skill_registry: SkillRegistry

    @property
    def model(self) -> StructuredAgentModel: ...

    def bind(self, invocation: RuntimeRoleInvocation) -> RoleInvocationInput: ...

    def invoke_bound(self, command: RoleInvocationInput) -> RoleInvocationOutput: ...

    def select_skill(
        self,
        episode_type: EpisodeType,
        *,
        doctor_material: bool = False,
    ) -> str: ...

    def can_view_work_product(self, producer: AgentId) -> bool: ...

    def can_view_tool_receipt(
        self,
        receipt: ToolReceipt,
        *,
        profile_purpose: str | None,
    ) -> bool: ...


class _ModelBackedRole(RuntimeAgentPort, ABC, Generic[InputT, OutputT]):
    """Shared model transport for a role; it is not an additional Agent identity."""

    agent_id: ClassVar[AgentId]
    boundary: ClassVar[AgentImplementationBoundary]
    input_contract: ClassVar[type[RoleInvocationInput]]
    output_contract: ClassVar[type[RoleInvocationOutput]]

    def __init__(
        self,
        model: StructuredAgentModel,
        *,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self._invoker = ProductAgentInvoker(agent_id=self.agent_id, model=model)
        self.skill_registry = skill_registry or SkillRegistry(
            default_skill_packages()
        )

    @property
    def model(self) -> StructuredAgentModel:
        return self._invoker.model

    @abstractmethod
    def bind(self, invocation: RuntimeRoleInvocation) -> InputT:
        raise NotImplementedError

    @abstractmethod
    def invoke(self, command: InputT) -> OutputT:
        raise NotImplementedError

    @abstractmethod
    def select_skill(
        self,
        episode_type: EpisodeType,
        *,
        doctor_material: bool = False,
    ) -> str:
        raise NotImplementedError

    def invoke_bound(
        self,
        command: RoleInvocationInput,
    ) -> RoleInvocationOutput:
        if type(command) is not self.input_contract:
            raise TypeError(
                f"{self.agent_id.value} requires {self.input_contract.__name__}"
            )
        return self.invoke(cast(InputT, command))

    def can_view_work_product(self, producer: AgentId) -> bool:
        return (
            type(producer) is AgentId
            and producer in self.boundary.context.visible_accepted_agents
        )

    def can_view_tool_receipt(
        self,
        receipt: ToolReceipt,
        *,
        profile_purpose: str | None,
    ) -> bool:
        return self.boundary.context.allows_tool_receipt(
            receipt,
            agent_id=self.agent_id,
            profile_purpose=profile_purpose,
        )

    def _invoke_model(
        self,
        command: RoleInvocationInput,
    ) -> tuple[AgentEnvelope, AgentInvocationRecord]:
        invocation = command.invocation
        if command.runtime_binding_hash != stable_hash(invocation):
            raise ValueError(
                f"{self.agent_id.value} prompt binding is not canonical"
            )
        self._validate_runtime_invocation(invocation)
        return self._invoker.invoke(
            caller=self.boundary.caller,
            context=invocation.context,
            parent_invocation_id=invocation.parent_invocation_id,
            target_type=self.boundary.work_product_kind.value,
            target_id=invocation.target_id,
            target_hash_material=invocation.target_hash_material,
            skill_id=invocation.skill_id,
            skill_version=invocation.skill_version,
            prompt_version=invocation.prompt_version,
            agent_version=self.boundary.agent_version,
            policy_version=invocation.policy_version,
            profile_version=invocation.profile_version,
            profile_hash=invocation.profile_hash,
            skill_package_hash=invocation.skill_package_hash,
            skill_lock_hash=invocation.skill_lock_hash,
            prompt_bundle_hash=invocation.prompt_bundle_hash,
            compiled_messages=list(invocation.compiled_messages),
        )

    def _bind_role(
        self,
        invocation: RuntimeRoleInvocation,
        input_contract: type[InputT],
    ) -> InputT:
        bound = input_contract(
            invocation=invocation,
            runtime_binding_hash=stable_hash(invocation),
        )
        self._validate_runtime_invocation(invocation)
        return bound

    def _validate_runtime_invocation(
        self,
        invocation: RuntimeRoleInvocation,
    ) -> None:
        self._validate_context(invocation)
        if type(invocation.episode_type) is not EpisodeType:
            raise TypeError("role invocation requires an exact EpisodeType")
        expected_skill_id = self.select_skill(
            invocation.episode_type,
            doctor_material=invocation.doctor_material,
        )
        if invocation.skill_id != expected_skill_id:
            raise ValueError(
                f"{self.agent_id.value} invocation selected a non-canonical Skill"
            )
        if invocation.skill_id not in self.boundary.allowed_skill_ids:
            raise ValueError(
                f"{self.agent_id.value} does not own Skill {invocation.skill_id}"
            )
        if invocation.prompt_version != (
            f"{invocation.skill_id}.prompt.{invocation.skill_version}"
        ):
            raise ValueError("Agent invocation prompt version is not Skill-bound")
        if invocation.policy_version != PRODUCT_SAFETY_POLICY_VERSION:
            raise ValueError("Agent invocation does not match Runtime policy")
        if (
            invocation.profile_version != self.boundary.profile_version
            or invocation.profile_hash != self.boundary.profile_hash
        ):
            raise ValueError("Agent invocation does not match its frozen profile")

    def _validate_context(self, invocation: RuntimeRoleInvocation) -> None:
        context = invocation.context
        if type(context.agent_id) is not AgentId or context.agent_id is not self.agent_id:
            raise ValueError("ContextPacket is bound to another Agent role")
        boundary = self.boundary.context
        labels = {item.trust_label for item in context.items}
        if not labels.issubset(set(boundary.allowed_trust_labels)):
            raise ValueError("Context trust label exceeds concrete Agent boundary")
        invalid_keys = [
            item.key for item in context.items if not boundary.allows_context_key(item.key)
        ]
        if invalid_keys:
            raise ValueError(
                f"Context key exceeds {self.agent_id.value} boundary: {invalid_keys[0]}"
            )
        present = {item.key for item in context.items}
        missing = set(boundary.required_context_keys) - present
        if missing:
            raise ValueError(
                f"Context omits required {self.agent_id.value} input: {sorted(missing)}"
            )
        for item in context.items:
            tool_name: str | None = None
            if item.key.startswith("tool:"):
                tool_name = item.key.removeprefix("tool:")
            elif item.key.startswith("tool_receipt:"):
                tool_name = item.key.removeprefix("tool_receipt:")
            if tool_name is not None and not boundary.allows_tool_name(
                tool_name,
                profile_purpose=invocation.profile_purpose,
            ):
                raise ValueError(
                    f"Tool receipt {tool_name} exceeds {self.agent_id.value} visibility"
                )
            if item.key == "collaboration_request":
                request = CrossAgentRequest.model_validate(item.value)
                if request.receiver is not self.agent_id:
                    raise ValueError("collaboration request targets another Agent")
                if request.request_type not in COLLABORATION_ALLOWLIST.get(
                    (request.sender, request.receiver),
                    frozenset(),
                ):
                    raise ValueError("collaboration request exceeds role allowlist")


def validate_role_output(
    output: RoleInvocationOutput,
    *,
    agent_id: AgentId,
    payload_type: type[StrictContract],
) -> None:
    if output.envelope.agent_id is not agent_id or output.record.agent_id is not agent_id:
        raise ValueError("typed Agent output identity mismatch")
    if output.envelope.target_hash != output.record.target_hash:
        raise ValueError("typed Agent output target hash mismatch")
    if not isinstance(output.envelope.output_payload, payload_type):
        raise ValueError("typed Agent output payload mismatch")


__all__ = [
    "AgentControlPortBoundary",
    "AgentImplementationBoundary",
    "AudienceRole",
    "CollaborationPermission",
    "EpisodePlanProposal",
    "EvaluationDecision",
    "PurposeScopedReceipt",
    "ReviewTargetBinding",
    "RoleContextBoundary",
    "RoleInvocationInput",
    "RoleInvocationOutput",
    "RuntimeAgentPort",
    "RuntimeRoleInvocation",
    "SLEEPCARE_CONTROL_CONTEXT_BOUNDARY",
    "SleepCareControlInvocationPort",
    "SleepCareEvaluation",
    "SleepCareEvaluationContext",
    "SleepCareEvaluationInput",
    "SleepCareEvaluationOutput",
    "SleepCarePlanContext",
    "SleepCarePlanInput",
    "SleepCarePlanOutput",
    "TypedAgentPort",
]
