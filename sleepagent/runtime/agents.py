"""四个 Product Agent 的身份、边界与实现。"""

from __future__ import annotations


# 合并自 agents/ports.py。
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, ClassVar, Generic, Literal, Protocol, TypeVar, cast

from pydantic import Field, model_validator

from sleepagent.runtime.contracts import (
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
from sleepagent.runtime.invocation import (
    AgentInvocationRecord,
    ProductAgentInvoker,
    StructuredAgentModel,
)
from sleepagent.runtime.governance import (
    PRODUCT_SAFETY_POLICY_VERSION,
)
from sleepagent.runtime.registry import (
    AGENT_DEFINITIONS,
    COLLABORATION_ALLOWLIST,
    TOOL_INVOCATION_ALLOWLIST,
)
from sleepagent.runtime.registry import (
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
OutputT = TypeVar("OutputT", bound=RoleInvocationOutput, covariant=True)


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


# 合并自 agents/sleepcare.py。
from pydantic import model_validator

from sleepagent.runtime.agents import (
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
from sleepagent.runtime.contracts import (
    AgentId,
    CommunicationDraft,
    EpisodeType,
    TrustLabel,
    WorkProductKind,
)
from sleepagent.runtime.invocation import StructuredAgentModel
from sleepagent.runtime.registry import (
    TOOL_INVOCATION_ALLOWLIST,
)
from sleepagent.runtime.registry import (
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
            "sleepagent.runtime.agents.SleepCareAgent"
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


# 合并自 agents/evidence_reasoning.py。
from pydantic import model_validator

from sleepagent.runtime.agents import (
    RoleContextBoundary,
    RoleInvocationInput,
    RoleInvocationOutput,
    RuntimeRoleInvocation,
    _ModelBackedRole,
    build_implementation_boundary,
    validate_role_output,
)
from sleepagent.runtime.contracts import (
    AgentId,
    EpisodeType,
    EvidencePacket,
    TrustLabel,
    WorkProductKind,
)
from sleepagent.runtime.registry import (
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
        implementation="sleepagent.runtime.agents.EvidenceReasoningAgent",
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


# 合并自 agents/care_strategy.py。
from pydantic import model_validator

from sleepagent.runtime.agents import (
    RoleContextBoundary,
    RoleInvocationInput,
    RoleInvocationOutput,
    RuntimeRoleInvocation,
    _ModelBackedRole,
    build_implementation_boundary,
    validate_role_output,
)
from sleepagent.runtime.contracts import (
    AgentId,
    CareStrategy,
    EpisodeType,
    TrustLabel,
    WorkProductKind,
)
from sleepagent.runtime.registry import (
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
        implementation="sleepagent.runtime.agents.CareStrategyAgent",
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


# 合并自 agents/safety_review.py。
from pydantic import model_validator

from sleepagent.runtime.agents import (
    RoleContextBoundary,
    RoleInvocationInput,
    RoleInvocationOutput,
    RuntimeRoleInvocation,
    _ModelBackedRole,
    build_implementation_boundary,
    validate_role_output,
)
from sleepagent.runtime.contracts import (
    AgentId,
    EpisodeType,
    SafetyDecision,
    TrustLabel,
    WorkProductKind,
)
from sleepagent.runtime.registry import (
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
        implementation="sleepagent.runtime.agents.SafetyReviewAgent",
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


# 合并自 agents/manifest.py。
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sleepagent.runtime.agents import (
    CareStrategyAgent,
)
from sleepagent.runtime.agents import (
    EvidenceReasoningAgent,
)
from sleepagent.runtime.agents import (
    AgentControlPortBoundary,
    AgentImplementationBoundary,
    PurposeScopedReceipt,
    RoleContextBoundary,
)
from sleepagent.runtime.agents import (
    SafetyReviewAgent,
)
from sleepagent.runtime.agents import (
    SleepCareAgent,
)
from sleepagent.runtime.contracts import (
    AgentId,
    PRODUCT_AGENT_ROSTER,
    TrustLabel,
)
from sleepagent.runtime.invocation import (
    MODEL_OUTPUT_BY_AGENT,
    StructuredAgentModel,
)
from sleepagent.runtime.registry import (
    AGENT_DEFINITIONS,
    COLLABORATION_ALLOWLIST,
    TOOL_INVOCATION_ALLOWLIST,
)
from sleepagent.runtime.registry import (
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


AGENT_IMPLEMENTATIONS: Mapping[
    AgentId, type[_ModelBackedRole[Any, Any]]
] = MappingProxyType(
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
