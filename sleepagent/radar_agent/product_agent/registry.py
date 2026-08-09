from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    CrossAgentRequestType,
    EpisodeBudget,
    EpisodePlan,
    EpisodeType,
    PRODUCT_AGENT_ROSTER,
    ToolEffect,
    WorkProductKind,
)


REGISTRY_VERSION = "sleepagent-product-registry.v13"


class InvocationPolicyError(ValueError):
    pass


class EpisodePlanPolicyError(ValueError):
    pass


class RosterPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentDefinition:
    agent_id: AgentId
    responsibility: str
    payload_schema: str
    may_publish: bool = False
    may_mutate_shared_state: bool = False
    may_execute_side_effects: bool = False


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    effect: ToolEffect
    owner: str
    confirmation_required: bool = False
    version: str = "v1"


@dataclass(frozen=True)
class EpisodeDefinition:
    episode_type: EpisodeType
    required_work_products: frozenset[WorkProductKind]
    allowed_work_products: frozenset[WorkProductKind]
    required_tools: frozenset[str]
    allowed_agents: frozenset[AgentId]
    allowed_tools: frozenset[str]
    conditional_safety_checkpoints: frozenset[str]
    exit_conditions: frozenset[str]
    budget: EpisodeBudget


AGENT_DEFINITIONS: Mapping[AgentId, AgentDefinition] = MappingProxyType({
    item.agent_id: item
    for item in (
        AgentDefinition(
            AgentId.SLEEP_CARE,
            "user goal, bounded orchestration, memory intent and final publication",
            "CommunicationDraft",
            may_publish=True,
        ),
        AgentDefinition(
            AgentId.EVIDENCE_REASONING,
            "personal Evidence claims, limited interpretation and uncertainty",
            "EvidencePacket",
        ),
        AgentDefinition(
            AgentId.CARE_STRATEGY,
            "single-action care strategy and cross-day follow-up",
            "CareStrategy",
        ),
        AgentDefinition(
            AgentId.SAFETY_REVIEW,
            "target-bound conditional semantic safety review",
            "SafetyDecision",
        ),
    )
})


TOOL_DEFINITIONS: Mapping[str, ToolDefinition] = MappingProxyType({
    item.name: item
    for item in (
        ToolDefinition("radar.get_night_evidence", ToolEffect.READ_ONLY, "data"),
        ToolDefinition("radar.get_range_evidence", ToolEffect.READ_ONLY, "data"),
        ToolDefinition("radar.assess_data_quality", ToolEffect.READ_ONLY, "quality"),
        ToolDefinition("radar.get_device_status", ToolEffect.READ_ONLY, "quality"),
        ToolDefinition("runtime.build_fact_snapshot", ToolEffect.READ_ONLY, "runtime"),
        ToolDefinition("cold_start.evaluate", ToolEffect.READ_ONLY, "runtime"),
        ToolDefinition("trend.calculate_metrics", ToolEffect.READ_ONLY, "trend"),
        ToolDefinition("risk.classify_signal", ToolEffect.READ_ONLY, "risk"),
        ToolDefinition("risk.match_urgent_boundary", ToolEffect.READ_ONLY, "risk"),
        ToolDefinition(
            "reasoning.resolve_event_context",
            ToolEffect.READ_ONLY,
            "reasoning",
        ),
        ToolDefinition("baseline.read", ToolEffect.READ_ONLY, "trend"),
        ToolDefinition("knowledge.retrieve_reviewed", ToolEffect.READ_ONLY, "knowledge"),
        ToolDefinition(
            "care.read_state", ToolEffect.READ_ONLY, "care_state", version="v2"
        ),
        ToolDefinition(
            "care.read_catalog", ToolEffect.READ_ONLY, "care_catalog", version="v2"
        ),
        ToolDefinition(
            "care.read_constraints",
            ToolEffect.READ_ONLY,
            "care_catalog",
            version="v2",
        ),
        ToolDefinition(
            "questionnaire.select_profile",
            ToolEffect.STATE_WRITE,
            "questionnaire",
            version="v2",
        ),
        ToolDefinition(
            "questionnaire.capture_profile",
            ToolEffect.STATE_WRITE,
            "questionnaire",
            version="v2",
        ),
        ToolDefinition("artifact.render", ToolEffect.READ_ONLY, "artifact"),
        ToolDefinition("memory.read", ToolEffect.READ_ONLY, "memory"),
        ToolDefinition(
            "memory.resolve_source",
            ToolEffect.READ_ONLY,
            "memory",
        ),
        ToolDefinition(
            "memory.review_candidates",
            ToolEffect.READ_ONLY,
            "memory",
        ),
        ToolDefinition(
            "memory.prepare_candidate",
            ToolEffect.READ_ONLY,
            "memory",
        ),
        ToolDefinition("profile.read", ToolEffect.READ_ONLY, "memory"),
        ToolDefinition(
            "profile.build_change_set", ToolEffect.READ_ONLY, "memory"
        ),
        ToolDefinition(
            "coordination.read_policy",
            ToolEffect.READ_ONLY,
            "coordination",
            version="v2",
        ),
        ToolDefinition(
            "device.read_delivery_policy",
            ToolEffect.READ_ONLY,
            "device",
            version="v2",
        ),
        ToolDefinition(
            "policy.read", ToolEffect.READ_ONLY, "policy", version="v2"
        ),
        ToolDefinition(
            "state.commit_care", ToolEffect.STATE_WRITE, "commit_controller", True
        ),
        ToolDefinition(
            "state.commit_memory", ToolEffect.STATE_WRITE, "commit_controller", True
        ),
        ToolDefinition(
            "state.commit_habit_profile",
            ToolEffect.STATE_WRITE,
            "commit_controller",
            True,
        ),
        ToolDefinition(
            "external.notify",
            ToolEffect.EXTERNAL_SIDE_EFFECT,
            "commit_controller",
            True,
        ),
        ToolDefinition(
            "external.share",
            ToolEffect.EXTERNAL_SIDE_EFFECT,
            "commit_controller",
            True,
        ),
        ToolDefinition(
            "external.export",
            ToolEffect.EXTERNAL_SIDE_EFFECT,
            "commit_controller",
            True,
        ),
    )
})


AGENT_INVOCATION_ALLOWLIST: Mapping[
    AgentId | str, frozenset[AgentId]
] = MappingProxyType({
    "runtime": frozenset({AgentId.SLEEP_CARE}),
    AgentId.SLEEP_CARE: frozenset(
        {
            AgentId.SLEEP_CARE,
            AgentId.EVIDENCE_REASONING,
            AgentId.CARE_STRATEGY,
            AgentId.SAFETY_REVIEW,
        }
    ),
    AgentId.EVIDENCE_REASONING: frozenset(),
    AgentId.CARE_STRATEGY: frozenset(),
    AgentId.SAFETY_REVIEW: frozenset(),
})


COLLABORATION_ALLOWLIST: Mapping[
    tuple[AgentId, AgentId], frozenset[CrossAgentRequestType]
] = MappingProxyType({
    (AgentId.SLEEP_CARE, AgentId.EVIDENCE_REASONING): frozenset(
        {CrossAgentRequestType.EVIDENCE, CrossAgentRequestType.REVISION}
    ),
    (AgentId.SLEEP_CARE, AgentId.CARE_STRATEGY): frozenset(
        {CrossAgentRequestType.CARE, CrossAgentRequestType.REVISION}
    ),
    (AgentId.SLEEP_CARE, AgentId.SAFETY_REVIEW): frozenset(
        {CrossAgentRequestType.SAFETY_REVIEW}
    ),
    (AgentId.CARE_STRATEGY, AgentId.EVIDENCE_REASONING): frozenset(
        {CrossAgentRequestType.EVIDENCE}
    ),
    (AgentId.SAFETY_REVIEW, AgentId.SLEEP_CARE): frozenset(
        {CrossAgentRequestType.REVISION}
    ),
    (AgentId.SAFETY_REVIEW, AgentId.EVIDENCE_REASONING): frozenset(
        {CrossAgentRequestType.REVISION}
    ),
    (AgentId.SAFETY_REVIEW, AgentId.CARE_STRATEGY): frozenset(
        {CrossAgentRequestType.REVISION}
    ),
    (AgentId.EVIDENCE_REASONING, AgentId.SLEEP_CARE): frozenset(
        {CrossAgentRequestType.USER_FACT}
    ),
    (AgentId.CARE_STRATEGY, AgentId.SLEEP_CARE): frozenset(
        {CrossAgentRequestType.USER_FACT, CrossAgentRequestType.CONFIRMATION}
    ),
})


TOOL_INVOCATION_ALLOWLIST: Mapping[
    AgentId, frozenset[str]
] = MappingProxyType({
    AgentId.SLEEP_CARE: frozenset(
        {
            "policy.read",
            "knowledge.retrieve_reviewed",
            "artifact.render",
            "memory.read",
            "memory.review_candidates",
            "memory.prepare_candidate",
            "profile.read",
            "profile.build_change_set",
        }
    ),
    AgentId.EVIDENCE_REASONING: frozenset(
        {
            "radar.get_night_evidence",
            "radar.get_range_evidence",
            "radar.assess_data_quality",
            "radar.get_device_status",
            "trend.calculate_metrics",
            "knowledge.retrieve_reviewed",
            "memory.read",
            "memory.resolve_source",
            "profile.read",
            "baseline.read",
            "reasoning.resolve_event_context",
        }
    ),
    AgentId.CARE_STRATEGY: frozenset(
        {
            "care.read_state",
            "care.read_catalog",
            "care.read_constraints",
            "coordination.read_policy",
            "device.read_delivery_policy",
            "knowledge.retrieve_reviewed",
        }
    ),
    AgentId.SAFETY_REVIEW: frozenset(
        {
            "policy.read",
            "risk.classify_signal",
            "care.read_catalog",
            "care.read_constraints",
        }
    ),
})


RUNTIME_INTERACTION_TOOLS = frozenset(
    {
        "questionnaire.select_profile",
        "questionnaire.capture_profile",
    }
)
COMMIT_CONTROLLER_TOOLS = frozenset(
    name for name, item in TOOL_DEFINITIONS.items()
    if item.owner == "commit_controller"
)
_COMMON_TOOLS = frozenset(
    {"runtime.build_fact_snapshot", "risk.match_urgent_boundary", "policy.read"}
)
_ALL_READ_TOOLS = frozenset(
    name for name, item in TOOL_DEFINITIONS.items() if item.effect == ToolEffect.READ_ONLY
)


def _definition(
    episode_type: EpisodeType,
    *,
    required: set[WorkProductKind],
    allowed: set[WorkProductKind],
    tools: set[str],
    safety: set[str] | None = None,
    exits: set[str],
) -> EpisodeDefinition:
    deterministic = episode_type == EpisodeType.URGENT_BOUNDARY
    complex_episode = episode_type in {
        EpisodeType.CARE_PLAN,
        EpisodeType.CARE_FOLLOWUP,
        EpisodeType.ROLE_MATERIAL,
    }
    return EpisodeDefinition(
        episode_type=episode_type,
        required_work_products=frozenset(required),
        allowed_work_products=frozenset(allowed),
        required_tools=frozenset(tools) if deterministic else frozenset(tools) | _COMMON_TOOLS,
        allowed_agents=(
            frozenset()
            if deterministic
            else frozenset(AGENT_DEFINITIONS)
        ),
        allowed_tools=frozenset(tools) | _ALL_READ_TOOLS,
        conditional_safety_checkpoints=frozenset(safety or set()),
        exit_conditions=frozenset(exits),
        budget=EpisodeBudget(
            agent_call_limit=0 if deterministic else (16 if complex_episode else 14),
            total_model_call_limit=0 if deterministic else (24 if complex_episode else 20),
            tool_call_limit=6 if deterministic else (20 if complex_episode else 14),
            soft_deadline_seconds=90 if complex_episode else 30,
        ),
    )


_PLAN = WorkProductKind.SLEEPCARE_PLAN
_EVIDENCE = WorkProductKind.EVIDENCE_PACKET
_CARE = WorkProductKind.CARE_STRATEGY
_SAFETY = WorkProductKind.SAFETY_DECISION
_COMMUNICATION = WorkProductKind.COMMUNICATION

EPISODE_DEFINITIONS: Mapping[EpisodeType, EpisodeDefinition] = MappingProxyType({
    item.episode_type: item
    for item in (
        _definition(
            EpisodeType.MORNING_REVIEW,
            required={_PLAN, _EVIDENCE, _COMMUNICATION},
            allowed={_PLAN, _EVIDENCE, _SAFETY, _COMMUNICATION},
            tools={
                "radar.get_night_evidence",
                "radar.assess_data_quality",
                "risk.classify_signal",
            },
            safety={"personal_claim_safety"},
            exits={"communication_published", "safe_degraded", "blocked"},
        ),
        _definition(
            EpisodeType.TREND_REVIEW,
            required={_PLAN, _EVIDENCE, _COMMUNICATION},
            allowed={_PLAN, _EVIDENCE, _SAFETY, _COMMUNICATION},
            tools={
                "radar.get_range_evidence",
                "radar.assess_data_quality",
                "trend.calculate_metrics",
            },
            safety={"personal_claim_safety"},
            exits={"communication_published", "safe_degraded", "blocked"},
        ),
        _definition(
            EpisodeType.GROUNDED_DIALOGUE,
            required={_PLAN, _COMMUNICATION},
            allowed={_PLAN, _EVIDENCE, _CARE, _SAFETY, _COMMUNICATION},
            tools=set(),
            safety={"personal_claim_safety", "external_action_safety"},
            exits={
                "answer_published",
                "waiting_user",
                "waiting_confirmation",
                "safe_degraded",
                "blocked",
            },
        ),
        _definition(
            EpisodeType.CARE_PLAN,
            required={_PLAN, _EVIDENCE, _CARE, _COMMUNICATION},
            allowed={_PLAN, _EVIDENCE, _CARE, _SAFETY, _COMMUNICATION},
            tools={"care.read_catalog", "care.read_state"},
            safety={"care_candidate_safety", "external_action_safety"},
            exits={
                "no_action_published",
                "waiting_confirmation",
                "state_committed",
                "safe_degraded",
                "blocked",
            },
        ),
        _definition(
            EpisodeType.CARE_FOLLOWUP,
            required={_PLAN, _EVIDENCE, _CARE, _COMMUNICATION},
            allowed={_PLAN, _EVIDENCE, _CARE, _SAFETY, _COMMUNICATION},
            tools={"care.read_state"},
            safety={"care_candidate_safety", "external_action_safety"},
            exits={
                "followup_published",
                "waiting_confirmation",
                "safe_degraded",
                "blocked",
            },
        ),
        _definition(
            EpisodeType.DATA_QUALITY_RECOVERY,
            required=set(),
            allowed=set(),
            tools={"radar.assess_data_quality", "radar.get_device_status"},
            exits={"registered_recovery_step_published", "blocked"},
        ),
        _definition(
            EpisodeType.ROLE_MATERIAL,
            required={_PLAN, _EVIDENCE, _COMMUNICATION},
            allowed={_PLAN, _EVIDENCE, _CARE, _COMMUNICATION, _SAFETY},
            tools={"artifact.render"},
            safety={"doctor_material_safety", "external_action_safety"},
            exits={"artifact_published", "waiting_confirmation", "blocked"},
        ),
        _definition(
            EpisodeType.URGENT_BOUNDARY,
            required=set(),
            allowed=set(),
            tools={"risk.match_urgent_boundary"},
            exits={"urgent_message_delivered"},
        ),
    )
})


def _is_exact_agent_roster(values: object) -> bool:
    try:
        items = tuple(values)  # type: ignore[arg-type]
    except TypeError:
        return False
    return (
        len(items) == len(PRODUCT_AGENT_ROSTER)
        and all(type(item) is AgentId for item in items)
        and frozenset(items) == frozenset(PRODUCT_AGENT_ROSTER)
    )


def validate_product_agent_registry() -> None:
    """Fail closed if any registration surface escapes the four-role roster."""

    enum_members = tuple(AgentId.__members__.values())
    if enum_members != PRODUCT_AGENT_ROSTER:
        raise RosterPolicyError("AgentId must contain exactly the frozen roster")

    definition_keys = tuple(AGENT_DEFINITIONS)
    if definition_keys != PRODUCT_AGENT_ROSTER or not all(
        type(item) is AgentId for item in definition_keys
    ):
        raise RosterPolicyError("Agent definitions must match the frozen roster")
    if any(
        definition.agent_id is not agent_id
        for agent_id, definition in AGENT_DEFINITIONS.items()
    ):
        raise RosterPolicyError("Agent definition identity mismatch")
    if any(
        TOOL_DEFINITIONS[name].effect is not ToolEffect.STATE_WRITE
        or TOOL_DEFINITIONS[name].owner != "questionnaire"
        or TOOL_DEFINITIONS[name].confirmation_required
        for name in RUNTIME_INTERACTION_TOOLS
    ):
        raise RosterPolicyError("runtime interaction Tool contract mismatch")
    if any(
        TOOL_DEFINITIONS[name].effect is ToolEffect.READ_ONLY
        or TOOL_DEFINITIONS[name].owner != "commit_controller"
        for name in COMMIT_CONTROLLER_TOOLS
    ):
        raise RosterPolicyError("Commit Controller Tool contract mismatch")
    non_read_tools = {
        name
        for name, definition in TOOL_DEFINITIONS.items()
        if definition.effect is not ToolEffect.READ_ONLY
    }
    if non_read_tools != set(RUNTIME_INTERACTION_TOOLS) | set(
        COMMIT_CONTROLLER_TOOLS
    ):
        raise RosterPolicyError("state-changing Tool owner set is incomplete")

    tool_allowlist_keys = tuple(TOOL_INVOCATION_ALLOWLIST)
    if tool_allowlist_keys != PRODUCT_AGENT_ROSTER or not all(
        type(item) is AgentId for item in tool_allowlist_keys
    ):
        raise RosterPolicyError("Agent tool allowlists must match the frozen roster")

    invocation_keys = tuple(AGENT_INVOCATION_ALLOWLIST)
    if (
        not invocation_keys
        or type(invocation_keys[0]) is not str
        or invocation_keys[0] != "runtime"
        or invocation_keys[1:] != PRODUCT_AGENT_ROSTER
        or any(type(item) is not AgentId for item in invocation_keys[1:])
    ):
        raise RosterPolicyError(
            "Agent invocation callers must be runtime plus the frozen roster"
        )
    runtime_targets = AGENT_INVOCATION_ALLOWLIST["runtime"]
    if tuple(runtime_targets) != (AgentId.SLEEP_CARE,) or any(
        type(item) is not AgentId for item in runtime_targets
    ):
        raise RosterPolicyError("runtime may invoke only SleepCareAgent")
    sleepcare_targets = AGENT_INVOCATION_ALLOWLIST[AgentId.SLEEP_CARE]
    if not _is_exact_agent_roster(sleepcare_targets):
        raise RosterPolicyError("SleepCareAgent must coordinate the frozen roster")
    if any(
        AGENT_INVOCATION_ALLOWLIST[agent_id]
        for agent_id in PRODUCT_AGENT_ROSTER
        if agent_id is not AgentId.SLEEP_CARE
    ):
        raise RosterPolicyError("specialist Agents cannot directly invoke Agents")

    for sender, receiver in COLLABORATION_ALLOWLIST:
        if (
            type(sender) is not AgentId
            or type(receiver) is not AgentId
            or sender not in PRODUCT_AGENT_ROSTER
            or receiver not in PRODUCT_AGENT_ROSTER
        ):
            raise RosterPolicyError(
                "collaboration endpoint is outside the frozen roster"
            )

    expected_agents = frozenset(PRODUCT_AGENT_ROSTER)
    for episode_type, definition in EPISODE_DEFINITIONS.items():
        if episode_type is EpisodeType.URGENT_BOUNDARY:
            if definition.allowed_agents:
                raise RosterPolicyError("urgent boundary cannot invoke model Agents")
        elif not _is_exact_agent_roster(definition.allowed_agents):
            raise RosterPolicyError(
                f"{episode_type.value} must use the frozen Agent roster"
            )
        if any(type(item) is not AgentId for item in definition.allowed_agents):
            raise RosterPolicyError("Episode Agent allowlist contains a string alias")
        if definition.allowed_agents and definition.allowed_agents != expected_agents:
            raise RosterPolicyError("Episode Agent allowlist expands the frozen roster")

    publishers = tuple(
        agent_id
        for agent_id, definition in AGENT_DEFINITIONS.items()
        if definition.may_publish
    )
    if publishers != (AgentId.SLEEP_CARE,):
        raise RosterPolicyError("SleepCareAgent must be the unique publisher")
    if any(
        definition.may_mutate_shared_state
        or definition.may_execute_side_effects
        for definition in AGENT_DEFINITIONS.values()
    ):
        raise RosterPolicyError("model Agents cannot own writes or side effects")


validate_product_agent_registry()


def authorize_agent_invocation(caller: AgentId | str, target: AgentId) -> None:
    if type(target) is not AgentId:
        raise InvocationPolicyError("agent target must be a registered AgentId")
    if type(caller) is str:
        if caller != "runtime":
            raise InvocationPolicyError(f"unknown agent caller: {caller}")
    elif type(caller) is not AgentId:
        raise InvocationPolicyError("agent caller must be runtime or a registered AgentId")
    if target not in AGENT_INVOCATION_ALLOWLIST.get(caller, frozenset()):
        caller_name = caller.value if type(caller) is AgentId else caller
        raise InvocationPolicyError(
            f"agent invocation denied by {REGISTRY_VERSION}: {caller_name} -> {target.value}"
        )


def authorize_collaboration(
    sender: AgentId,
    receiver: AgentId,
    request_type: CrossAgentRequestType,
) -> None:
    if type(sender) is not AgentId or type(receiver) is not AgentId:
        raise InvocationPolicyError(
            "collaboration endpoints must be registered AgentId values"
        )
    if type(request_type) is not CrossAgentRequestType:
        raise InvocationPolicyError(
            "collaboration request type must be CrossAgentRequestType"
        )
    if request_type not in COLLABORATION_ALLOWLIST.get(
        (sender, receiver), frozenset()
    ):
        raise InvocationPolicyError(
            "collaboration denied by "
            f"{REGISTRY_VERSION}: {sender.value} -> {receiver.value}:{request_type.value}"
        )


def authorize_tool_invocation(caller: AgentId | str, tool_name: str) -> None:
    definition = TOOL_DEFINITIONS.get(tool_name)
    if definition is None:
        raise InvocationPolicyError(f"unknown tool: {tool_name}")
    if caller == "commit_controller":
        if tool_name not in COMMIT_CONTROLLER_TOOLS:
            raise InvocationPolicyError(f"commit controller cannot execute {tool_name}")
        return
    if caller == "runtime":
        if (
            definition.effect != ToolEffect.READ_ONLY
            and tool_name not in RUNTIME_INTERACTION_TOOLS
        ):
            raise InvocationPolicyError("runtime cannot execute state-changing tools")
        return
    if not isinstance(caller, AgentId):
        raise InvocationPolicyError(f"unknown caller: {caller}")
    if tool_name not in TOOL_INVOCATION_ALLOWLIST.get(caller, frozenset()):
        raise InvocationPolicyError(
            f"tool invocation denied by {REGISTRY_VERSION}: {caller.value} -> {tool_name}"
        )
    if definition.effect != ToolEffect.READ_ONLY:
        raise InvocationPolicyError("model Agents cannot execute state-changing tools")


def validate_episode_plan(
    plan: EpisodePlan,
    *,
    required_work_products: set[WorkProductKind] | frozenset[WorkProductKind] = frozenset(),
    required_safety_checkpoints: set[str] | frozenset[str] = frozenset(),
) -> EpisodeDefinition:
    definition = EPISODE_DEFINITIONS[plan.episode_type]
    requested = set(plan.required_work_products) | set(plan.conditional_work_products)
    required = set(definition.required_work_products) | set(required_work_products)
    if not required.issubset(plan.required_work_products):
        missing = required - set(plan.required_work_products)
        raise EpisodePlanPolicyError(
            f"plan omits required work products: {sorted(item.value for item in missing)}"
        )
    if not requested.issubset(definition.allowed_work_products):
        invalid = requested - definition.allowed_work_products
        raise EpisodePlanPolicyError(
            f"plan includes disallowed work products: {sorted(item.value for item in invalid)}"
        )
    if set(plan.allowed_agents) != set(definition.allowed_agents):
        raise EpisodePlanPolicyError("plan Agent allowlist must equal the Episode registry")
    if not definition.required_tools.issubset(plan.allowed_tools):
        raise EpisodePlanPolicyError("plan omits required tools")
    if not set(plan.allowed_tools).issubset(definition.allowed_tools | _COMMON_TOOLS):
        raise EpisodePlanPolicyError("plan expands the tool allowlist")
    required_checkpoints = set(required_safety_checkpoints)
    if _SAFETY in requested and not plan.safety_checkpoints:
        raise EpisodePlanPolicyError("SafetyReview requires a target checkpoint")
    if not required_checkpoints.issubset(plan.safety_checkpoints):
        raise EpisodePlanPolicyError("plan omits required Safety checkpoint")
    if not set(plan.safety_checkpoints).issubset(
        definition.conditional_safety_checkpoints
    ):
        raise EpisodePlanPolicyError("plan includes an unregistered Safety checkpoint")
    if not set(plan.exit_conditions).issubset(definition.exit_conditions):
        raise EpisodePlanPolicyError("plan expands Episode exit conditions")
    if not set(plan.exit_conditions):
        raise EpisodePlanPolicyError("plan requires at least one exit condition")
    if plan.expected_agent_calls > definition.budget.agent_call_limit:
        raise EpisodePlanPolicyError("plan exceeds Agent-call budget")
    if plan.expected_tool_calls > definition.budget.tool_call_limit:
        raise EpisodePlanPolicyError("plan exceeds Tool-call budget")
    return definition


def product_agent_manifest() -> dict[str, object]:
    validate_product_agent_registry()
    return {
        "registry_version": REGISTRY_VERSION,
        "agents": {
            key.value: {
                "responsibility": value.responsibility,
                "payload_schema": value.payload_schema,
                "may_publish": value.may_publish,
                "may_mutate_shared_state": value.may_mutate_shared_state,
                "may_execute_side_effects": value.may_execute_side_effects,
            }
            for key, value in AGENT_DEFINITIONS.items()
        },
        "agent_invocation_allowlist": {
            key.value if isinstance(key, AgentId) else key: sorted(
                item.value for item in values
            )
            for key, values in AGENT_INVOCATION_ALLOWLIST.items()
        },
        "tool_invocation_allowlist": {
            key.value: sorted(values) for key, values in TOOL_INVOCATION_ALLOWLIST.items()
        },
        "tool_definitions": {
            key: {
                "effect": value.effect.value,
                "owner": value.owner,
                "confirmation_required": value.confirmation_required,
                "version": value.version,
            }
            for key, value in TOOL_DEFINITIONS.items()
        },
        "commit_controller_tools": sorted(COMMIT_CONTROLLER_TOOLS),
        "runtime_interaction_tools": sorted(RUNTIME_INTERACTION_TOOLS),
        "collaboration_allowlist": {
            f"{sender.value}->{receiver.value}": sorted(
                request_type.value for request_type in request_types
            )
            for (sender, receiver), request_types in COLLABORATION_ALLOWLIST.items()
        },
        "episodes": {
            key.value: {
                "required_work_products": sorted(
                    item.value for item in value.required_work_products
                ),
                "allowed_work_products": sorted(
                    item.value for item in value.allowed_work_products
                ),
                "allowed_agents": sorted(item.value for item in value.allowed_agents),
                "required_tools": sorted(value.required_tools),
                "allowed_tools": sorted(value.allowed_tools),
                "safety_checkpoints": sorted(value.conditional_safety_checkpoints),
                "exit_conditions": sorted(value.exit_conditions),
                "budget": value.budget.model_dump(mode="json"),
            }
            for key, value in EPISODE_DEFINITIONS.items()
        },
    }


__all__ = [
    "AGENT_DEFINITIONS",
    "AGENT_INVOCATION_ALLOWLIST",
    "COLLABORATION_ALLOWLIST",
    "COMMIT_CONTROLLER_TOOLS",
    "EPISODE_DEFINITIONS",
    "REGISTRY_VERSION",
    "RUNTIME_INTERACTION_TOOLS",
    "RosterPolicyError",
    "TOOL_DEFINITIONS",
    "TOOL_INVOCATION_ALLOWLIST",
    "AgentDefinition",
    "EpisodeDefinition",
    "EpisodePlanPolicyError",
    "InvocationPolicyError",
    "ToolDefinition",
    "authorize_agent_invocation",
    "authorize_collaboration",
    "authorize_tool_invocation",
    "product_agent_manifest",
    "validate_episode_plan",
    "validate_product_agent_registry",
]
