from __future__ import annotations

from dataclasses import dataclass

from sleepagent.radar_agent.dynamic.contracts import (
    AgentKind,
    CompletionStatus,
    GoalType,
    PlanStep,
    PlanStepKind,
    UserGoal,
)
from sleepagent.radar_agent.dynamic.model import PlanProposal
from sleepagent.radar_agent.dynamic.tool_registry import ALLOWED_DYNAMIC_TOOLS

ALLOWED_CAPABILITY_AGENTS = frozenset(
    {
        AgentKind.EVIDENCE_ANALYSIS,
        AgentKind.CARE_PLANNING,
        AgentKind.SAFETY_REVIEW,
        AgentKind.DIALOGUE,
    }
)

_SAFETY_OWNED_TOOLS = frozenset(
    {
        "radar.read",
        "quality.assess",
        "history.query",
        "trend.calculate",
        "urgent_boundary.evaluate",
        "evidence.ledger_validate",
        "evidence.ledger_commit",
        "role_artifact.render",
        "doctor_material.render",
        "confirmation.request",
        "confirmed_action.execute",
    }
)

_GOAL_ADAPTIVE_TOOLS = {
    GoalType.NIGHT_REVIEW: {
        "questionnaire.select",
        "questionnaire.capture",
        "rag.retrieve_reviewed",
    },
    GoalType.TREND_COMPARISON: {
        "history.query",
        "trend.calculate",
        "questionnaire.select",
        "questionnaire.capture",
        "rag.retrieve_reviewed",
    },
    GoalType.CHANGE_EXPLANATION: {
        "history.query",
        "trend.calculate",
        "questionnaire.select",
        "questionnaire.capture",
        "rag.retrieve_reviewed",
    },
    GoalType.DATA_QUALITY_DIAGNOSIS: {
        "questionnaire.select",
        "questionnaire.capture",
        "rag.retrieve_reviewed",
    },
    GoalType.DOCTOR_MATERIAL: {
        "questionnaire.select",
        "questionnaire.capture",
        "rag.retrieve_reviewed",
    },
    GoalType.GROUNDED_QUESTION: {"history.query", "rag.retrieve_reviewed"},
}

_GOAL_AGENTS = {
    GoalType.NIGHT_REVIEW: {
        AgentKind.EVIDENCE_ANALYSIS,
        AgentKind.CARE_PLANNING,
        AgentKind.SAFETY_REVIEW,
    },
    GoalType.TREND_COMPARISON: {
        AgentKind.EVIDENCE_ANALYSIS,
        AgentKind.CARE_PLANNING,
        AgentKind.SAFETY_REVIEW,
    },
    GoalType.CHANGE_EXPLANATION: {
        AgentKind.EVIDENCE_ANALYSIS,
        AgentKind.CARE_PLANNING,
        AgentKind.SAFETY_REVIEW,
    },
    GoalType.DATA_QUALITY_DIAGNOSIS: {
        AgentKind.EVIDENCE_ANALYSIS,
        AgentKind.SAFETY_REVIEW,
    },
    GoalType.DOCTOR_MATERIAL: {
        AgentKind.EVIDENCE_ANALYSIS,
        AgentKind.CARE_PLANNING,
        AgentKind.SAFETY_REVIEW,
    },
    GoalType.GROUNDED_QUESTION: {
        AgentKind.DIALOGUE,
        AgentKind.SAFETY_REVIEW,
    },
}


@dataclass(frozen=True)
class DegradedDecision:
    completion_status: CompletionStatus
    reason: str


DEGRADED_MATRIX = {
    GoalType.NIGHT_REVIEW: DegradedDecision(
        CompletionStatus.COMPLETE,
        "Canonical night facts can be summarized deterministically when quality gates pass.",
    ),
    GoalType.TREND_COMPARISON: DegradedDecision(
        CompletionStatus.COMPLETE,
        "Canonical trend metrics can be calculated deterministically when sample requirements pass.",
    ),
    GoalType.DATA_QUALITY_DIAGNOSIS: DegradedDecision(
        CompletionStatus.COMPLETE,
        "Device and coverage quality facts are deterministic.",
    ),
    GoalType.DOCTOR_MATERIAL: DegradedDecision(
        CompletionStatus.COMPLETE,
        "A deterministic structured packet can be prepared pending confirmation.",
    ),
    GoalType.CHANGE_EXPLANATION: DegradedDecision(
        CompletionStatus.PARTIAL,
        "The runtime can describe observed change but cannot infer a cause without a model.",
    ),
    GoalType.GROUNDED_QUESTION: DegradedDecision(
        CompletionStatus.PARTIAL,
        "Only exact allowlisted artifact facts can be returned without a model.",
    ),
}


def normalize_plan(
    proposal: PlanProposal,
    goal: UserGoal,
    *,
    safety_review_required: bool = False,
) -> list[PlanStep]:
    """Persist only judgment-bearing choices; hard gates run as middleware."""

    selected: list[tuple[PlanStepKind, str, AgentKind | None, str | None, bool, str]] = []

    def add_tool(name: str, capability: str, required: bool = True, rationale: str = "") -> None:
        if name not in ALLOWED_DYNAMIC_TOOLS:
            raise ValueError(f"dynamic plan requested non-whitelisted tool: {name}")
        key = (PlanStepKind.TOOL, name)
        if any((kind, tool) == key for kind, _, _, tool, _, _ in selected):
            return
        selected.append((PlanStepKind.TOOL, capability, None, name, required, rationale))

    conditional_agents: dict[AgentKind, str] = {}

    def add_agent(
        agent: AgentKind,
        capability: str,
        required: bool = True,
        rationale: str = "",
    ) -> None:
        if agent not in ALLOWED_CAPABILITY_AGENTS:
            raise ValueError(f"dynamic plan requested invalid capability agent: {agent.value}")
        if any(kind == PlanStepKind.AGENT and current == agent for kind, _, current, _, _, _ in selected):
            return
        selected.append((PlanStepKind.AGENT, capability, agent, None, required, rationale))

    for proposed in proposal.steps:
        if proposed.kind == "tool":
            if not proposed.tool_name:
                raise ValueError("proposed tool step has no tool name")
            if proposed.tool_name in _SAFETY_OWNED_TOOLS:
                continue
            if proposed.tool_name not in _GOAL_ADAPTIVE_TOOLS[goal.goal_type]:
                raise ValueError(
                    f"tool {proposed.tool_name} cannot expand {goal.goal_type.value} scope"
                )
            add_tool(
                proposed.tool_name,
                proposed.capability,
                proposed.required,
                proposed.rationale_summary,
            )
        else:
            if proposed.agent is None:
                raise ValueError("proposed agent step has no agent")
            if proposed.agent not in _GOAL_AGENTS[goal.goal_type]:
                raise ValueError(
                    f"agent {proposed.agent.value} cannot expand {goal.goal_type.value} scope"
                )
            if proposed.agent == AgentKind.SAFETY_REVIEW:
                conditional_agents[AgentKind.SAFETY_REVIEW] = proposed.capability
            else:
                add_agent(
                    proposed.agent,
                    proposed.capability,
                    proposed.required,
                    proposed.rationale_summary,
                )

    if goal.goal_type == GoalType.GROUNDED_QUESTION:
        add_agent(AgentKind.DIALOGUE, "answer_grounded_question")
    elif not any(item[0] == PlanStepKind.AGENT for item in selected):
        add_agent(AgentKind.EVIDENCE_ANALYSIS, "interpret_quality_gated_evidence")

    safety_always = (
        safety_review_required
        or AgentKind.SAFETY_REVIEW in conditional_agents
        or goal.goal_type == GoalType.DOCTOR_MATERIAL
        or any(item[2] == AgentKind.CARE_PLANNING for item in selected)
    )
    if safety_always:
        add_agent(
            AgentKind.SAFETY_REVIEW,
            conditional_agents.get(
                AgentKind.SAFETY_REVIEW, "review_safety_and_confirmation"
            ),
            required=True,
            rationale="Required by deterministic reviewer-trigger policy.",
        )

    steps: list[PlanStep] = []
    previous: str | None = None
    for ordinal, (kind, capability, agent, tool, required, rationale) in enumerate(selected, 1):
        step_id = f"step-{ordinal:02d}-{capability.replace('_', '-')[:48]}"
        steps.append(
            PlanStep(
                step_id=step_id,
                ordinal=ordinal,
                kind=kind,
                capability=capability,
                purpose=rationale or capability,
                agent=agent,
                tool_name=tool,
                depends_on=[previous] if previous else [],
                required=required,
                run_if=(
                    "risk_review_required"
                    if agent == AgentKind.SAFETY_REVIEW and not safety_always
                    else "always"
                ),
                rationale_summary=rationale,
            )
        )
        previous = step_id
    return steps


def degraded_steps(goal: UserGoal) -> list[PlanStep]:
    capability = f"deterministic_{goal.goal_type.value}_completion"
    return [
        PlanStep(
            step_id=f"step-01-{capability.replace('_', '-')}",
            ordinal=1,
            kind=PlanStepKind.COMPLETE,
            capability=capability,
            purpose="Complete only from deterministic safety middleware outputs.",
            rationale_summary="No capability Agent or fabricated collaboration is used.",
        )
    ]


def validate_requested_question(question_id: str | None) -> str | None:
    allowed = {
        "q-device-placement",
        "q-slept-away-from-bed",
        "q-night-bathroom",
        "q-daytime-sleepiness",
        "q-sleep-schedule-change",
    }
    if question_id is None:
        return None
    return question_id if question_id in allowed else None


def agent_allowed_for_goal(agent: AgentKind, goal_type: GoalType) -> bool:
    return agent in _GOAL_AGENTS[goal_type]


def agent_tool_scope(goal_type: GoalType) -> list[str]:
    """Tools an Agent may request; execution still belongs to the Orchestrator."""

    return sorted(_GOAL_ADAPTIVE_TOOLS[goal_type])


__all__ = [
    "ALLOWED_CAPABILITY_AGENTS",
    "ALLOWED_DYNAMIC_TOOLS",
    "DEGRADED_MATRIX",
    "DegradedDecision",
    "degraded_steps",
    "normalize_plan",
    "validate_requested_question",
    "agent_allowed_for_goal",
    "agent_tool_scope",
]
