from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from sleepagent.radar_agent.agents.risk import RiskSignalDecision, urgent_text_match
from sleepagent.radar_agent.agents.trend import RadarTrendResult
from sleepagent.radar_agent.dynamic.contracts import (
    AgentInvocation,
    AgentInvocationStatus,
    AgentKind,
    CompletionReceipt,
    CompletionStatus,
    DynamicStepRecord,
    EvaluationDecision,
    EvaluationOutcome,
    ExecutionMode,
    ExecutionPlan,
    GoalType,
    InvocationOutcome,
    ModelInvocation,
    PlanStep,
    PlanStepKind,
    RuntimeBudget,
    RuntimeBudgetExceeded,
    RuntimeCheckpoint,
    ToolInvocation,
    UserGoal,
    UserInputRequest,
    stable_input_sha256,
)
from sleepagent.radar_agent.provider import RadarProvider
from sleepagent.radar_agent.llm import (
    CloudLLMInvalidJSONError,
    CloudLLMSchemaError,
)
from sleepagent.radar_agent.questionnaire.defaults import (
    DEFAULT_QUESTIONNAIRE_BANK,
    DEFAULT_SKILL_QUESTION_PACK,
)
from sleepagent.radar_agent.runtime import (
    RadarAgentTask,
    RadarArtifactVersion,
    RadarTaskStatus,
    TaskService,
)
from sleepagent.radar_agent.schemas import (
    A2AMessage,
    EvidenceLedger,
    RadarDevice,
    RadarNightSummary,
    RadarVitalSnapshot,
    RiskLevel,
    RoleReportArtifact,
)

from .model import (
    AGENT_OUTPUT_SCHEMAS,
    AgentWorkProduct,
    EvaluationProposal,
    PlanProposal,
    StructuredModel,
    agent_messages,
    capability_agent_manifest,
    evaluation_messages,
    planner_messages,
)
from .policies import (
    ALLOWED_CAPABILITY_AGENTS,
    ALLOWED_DYNAMIC_TOOLS,
    DEGRADED_MATRIX,
    agent_allowed_for_goal,
    agent_tool_scope,
    degraded_steps,
    normalize_plan,
    validate_requested_question,
)
from .toolbox import DynamicToolbox, json_safe
from .tool_registry import dynamic_tool_manifest


SchemaT = TypeVar("SchemaT", bound=BaseModel)


class DynamicOrchestratorRuntime:
    """Persisted bounded Plan -> Execute -> Evaluate -> Replan controller."""

    def __init__(
        self,
        *,
        service: TaskService,
        provider: RadarProvider,
        model: StructuredModel | None,
        id_factory=None,
    ) -> None:
        self.service = service
        self.store = service.store
        self.model = model
        self.toolbox = DynamicToolbox(provider=provider, service=service)
        self._id_factory = id_factory or (lambda: uuid4().hex)

    def run(self, task_id: str) -> CompletionReceipt | None:
        task = self.service.get_task(task_id)
        if task.runtime_kind != "dynamic_goal":
            raise ValueError("dynamic runtime cannot execute a legacy task")
        if task.status == RadarTaskStatus.COMPLETED:
            return self.store.get_completion_receipt(task_id)
        if task.status == RadarTaskStatus.WAITING_FOR_CONFIRMATION:
            return self.store.get_completion_receipt(task_id)
        goal = self.store.get_task_goal(task_id)
        if task.goal_payload != goal.model_dump(mode="json"):
            raise ValueError("persisted task goal does not match the immutable dynamic goal")
        self._validate_goal_binding(task, goal)
        if task.status == RadarTaskStatus.RUNNING:
            resumed_receipt = self._resume_confirmation_action(task, goal)
            if resumed_receipt is not None:
                return resumed_receipt

        state = self._restore_state(task_id)
        budget = self._load_or_create_budget(goal)
        if task.status == RadarTaskStatus.WAITING_FOR_USER_INPUT:
            state = self._resume_user_input(task, state)
            budget = budget.resume_clock()
            self.store.save_runtime_budget(budget)
            task = self.service.transition_task(
                task_id,
                RadarTaskStatus.RUNNING,
                message="User input recorded; resuming the same execution plan.",
            )
            if state.get("_user_input_declined"):
                return self._finalize(
                    task=task,
                    goal=goal,
                    mode=ExecutionMode(
                        task.execution_mode or ExecutionMode.SAFE_DEGRADED.value
                    ),
                    completion_status=CompletionStatus.BLOCKED,
                    state=state,
                    failures=["user_input_declined"],
                )
        elif task.status == RadarTaskStatus.CREATED:
            task = self.service.transition_task(task_id, RadarTaskStatus.RUNNING)
        elif task.status != RadarTaskStatus.RUNNING:
            raise ValueError(f"task in {task.status.value!r} cannot run dynamically")

        plans = self.store.list_execution_plans(task_id)
        try:
            budget.assert_time_available()
        except RuntimeBudgetExceeded as exc:
            if plans:
                plan = plans[-1]
                mode = ExecutionMode(
                    task.execution_mode or ExecutionMode.SAFE_DEGRADED.value
                )
            else:
                mode = ExecutionMode.SAFE_DEGRADED
                plan = self._create_degraded_plan(goal, budget, revision=1)
            task = self._update_task(
                task_id,
                execution_mode=mode.value,
                current_plan_id=plan.plan_id,
            )
            if mode == ExecutionMode.SAFE_DEGRADED:
                self._emit_degraded(task_id, f"budget_exhausted_{exc.budget_name}")
            self.service.emit_event(
                task_id,
                event_type="goal.accepted",
                message="Explicit goal accepted for bounded execution.",
                payload={
                    "goal_id": goal.goal_id,
                    "goal_type": goal.goal_type.value,
                    "execution_mode": mode.value,
                },
            )
            self.service.emit_event(
                task_id,
                event_type="execution.budget_exhausted",
                message="Dynamic execution stopped before scheduling new work because its persisted safety budget expired.",
                payload={"budget": exc.budget_name},
            )
            return self._finalize(
                task=task,
                goal=goal,
                mode=mode,
                completion_status=CompletionStatus.PARTIAL,
                state=state,
                failures=[f"budget_exhausted:{exc.budget_name}"],
            )
        urgent_preflight = None
        preflight_failure: Exception | None = None
        explicit_text = [value for value in (goal.focus, goal.question) if value]
        if not plans and urgent_text_match(explicit_text) is not None:
            state, budget = self._run_explicit_urgent_preflight(
                task=task,
                goal=goal,
                state=state,
                budget=budget,
            )
            urgent_preflight = state["urgent_boundary.evaluate"]["risk_decision"]
        if not plans and urgent_preflight is None:
            try:
                state, budget = self._run_deterministic_preflight(
                    task=task,
                    goal=goal,
                    state=state,
                    budget=budget,
                )
            except RuntimeBudgetExceeded as exc:
                mode = ExecutionMode.SAFE_DEGRADED
                plan = self._create_degraded_plan(goal, budget, revision=1)
                task = self._update_task(
                    task_id,
                    execution_mode=mode.value,
                    current_plan_id=plan.plan_id,
                )
                self._emit_degraded(
                    task_id, f"preflight_budget_exhausted_{exc.budget_name}"
                )
                self.service.emit_event(
                    task_id,
                    event_type="goal.accepted",
                    message="Explicit goal accepted for bounded execution.",
                    payload={
                        "goal_id": goal.goal_id,
                        "goal_type": goal.goal_type.value,
                        "execution_mode": mode.value,
                    },
                )
                self.service.emit_event(
                    task_id,
                    event_type="execution.budget_exhausted",
                    message="Dynamic execution stopped during deterministic preflight at its persisted safety budget.",
                    payload={"budget": exc.budget_name},
                )
                return self._finalize(
                    task=task,
                    goal=goal,
                    mode=mode,
                    completion_status=CompletionStatus.PARTIAL,
                    state=state,
                    failures=[f"budget_exhausted:{exc.budget_name}"],
                )
            except Exception as exc:
                preflight_failure = exc
                state["_preflight_failure"] = exc.__class__.__name__
        mode = ExecutionMode.INTELLIGENT
        if plans:
            plan = plans[-1]
            mode = ExecutionMode(task.execution_mode or ExecutionMode.SAFE_DEGRADED.value)
        elif (
            urgent_preflight is not None
            and urgent_preflight.risk_level == RiskLevel.URGENT_BOUNDARY
        ):
            mode = ExecutionMode.SAFE_DEGRADED
            plan = self._create_degraded_plan(goal, budget, revision=1)
            budget = self.store.get_runtime_budget(task_id)
            self._emit_degraded(task_id, "urgent_text_preflight")
            state["_urgent_interrupted"] = True
            self.service.emit_event(
                task_id,
                event_type="execution.interrupted",
                message="Explicit urgent text activated the deterministic safety boundary before any model call.",
                payload={"reason_code": "urgent_text_preflight"},
            )
        elif preflight_failure is not None:
            mode = ExecutionMode.SAFE_DEGRADED
            plan = self._create_degraded_plan(goal, budget, revision=1)
            budget = self.store.get_runtime_budget(task_id)
            self._emit_degraded(
                task_id, f"preflight_{preflight_failure.__class__.__name__}"
            )
        elif self.model is None or not self.model.is_configured:
            mode = ExecutionMode.SAFE_DEGRADED
            plan = self._create_degraded_plan(goal, budget, revision=1)
            budget = self.store.get_runtime_budget(task_id)
            self._emit_degraded(task_id, "model_not_configured")
        else:
            try:
                plan, budget = self._create_intelligent_plan(
                    goal,
                    budget,
                    revision=1,
                    prior_state=state,
                )
            except RuntimeBudgetExceeded as exc:
                mode = ExecutionMode.SAFE_DEGRADED
                plan = self._create_degraded_plan(goal, budget, revision=1)
                budget = self.store.get_runtime_budget(task_id)
                state["_planning_budget_failure"] = exc.budget_name
                self._emit_degraded(
                    task_id, f"planning_budget_exhausted_{exc.budget_name}"
                )
            except Exception as exc:
                mode = ExecutionMode.SAFE_DEGRADED
                plan = self._create_degraded_plan(goal, budget, revision=1)
                budget = self.store.get_runtime_budget(task_id)
                self._emit_degraded(task_id, exc.__class__.__name__)

        task = self._update_task(
            task_id,
            execution_mode=mode.value,
            current_plan_id=plan.plan_id,
        )
        self.service.emit_event(
            task_id,
            event_type="goal.accepted",
            message="Explicit goal accepted for bounded execution.",
            payload={
                "goal_id": goal.goal_id,
                "goal_type": goal.goal_type.value,
                "execution_mode": mode.value,
            },
        )

        while True:
            try:
                budget.assert_time_available()
                state, budget, paused, failures, checkpoint_evaluation = self._execute_plan(
                    task=task,
                    goal=goal,
                    plan=plan,
                    budget=budget,
                    state=state,
                    mode=mode,
                )
            except RuntimeBudgetExceeded as exc:
                self.service.emit_event(
                    task_id,
                    event_type="execution.budget_exhausted",
                    message="Dynamic execution stopped at its persisted safety budget.",
                    payload={"budget": exc.budget_name},
                )
                return self._finalize(
                    task=task,
                    goal=goal,
                    mode=mode,
                    completion_status=CompletionStatus.PARTIAL,
                    state=state,
                    failures=[f"budget_exhausted:{exc.budget_name}"],
                )
            if paused:
                return None
            if mode == ExecutionMode.INTELLIGENT and state.get("_model_failure"):
                reason = str(state.pop("_model_failure"))
                self._emit_degraded(task_id, reason)
                mode = ExecutionMode.SAFE_DEGRADED
                plan = self._create_degraded_plan(
                    goal,
                    budget,
                    revision=plan.revision + 1,
                    supersedes=plan.plan_id,
                )
                budget = self.store.get_runtime_budget(task_id)
                self._invalidate_downstream_for_replan(state)
                task = self._update_task(
                    task_id,
                    execution_mode=mode.value,
                    current_plan_id=plan.plan_id,
                )
                continue
            if mode == ExecutionMode.SAFE_DEGRADED:
                status = self._degraded_completion_status(goal, state, failures)
                if status != CompletionStatus.BLOCKED:
                    try:
                        state, budget, postflight_failures = self._run_postflight(
                            task=self.service.get_task(task_id),
                            goal=goal,
                            plan=plan,
                            state=state,
                            budget=budget,
                        )
                        failures.extend(postflight_failures)
                        if postflight_failures:
                            status = CompletionStatus.PARTIAL
                    except RuntimeBudgetExceeded as exc:
                        failures.append(f"budget_exhausted:{exc.budget_name}")
                        status = CompletionStatus.PARTIAL
                    except Exception as exc:
                        failures.append(f"postflight:{exc.__class__.__name__}")
                        status = CompletionStatus.BLOCKED
                return self._finalize(
                    task=task,
                    goal=goal,
                    mode=mode,
                    completion_status=status,
                    state=state,
                    failures=failures,
                )

            try:
                evaluation = checkpoint_evaluation
                if evaluation is None:
                    evaluation, budget = self._evaluate(
                        goal=goal,
                        plan=plan,
                        budget=budget,
                        state=state,
                        failures=failures,
                        checkpoint_kind="goal_checkpoint",
                    )
            except Exception as exc:
                self._emit_degraded(task_id, f"evaluation_{exc.__class__.__name__}")
                task = self._update_task(
                    task_id, execution_mode=ExecutionMode.SAFE_DEGRADED.value
                )
                try:
                    state, budget, postflight_failures = self._run_postflight(
                        task=self.service.get_task(task_id),
                        goal=goal,
                        plan=plan,
                        state=state,
                        budget=budget,
                    )
                    failures.extend(postflight_failures)
                except Exception as postflight_exc:
                    failures.append(
                        f"postflight:{postflight_exc.__class__.__name__}"
                    )
                return self._finalize(
                    task=task,
                    goal=goal,
                    mode=ExecutionMode.SAFE_DEGRADED,
                    completion_status=(
                        CompletionStatus.PARTIAL if failures else CompletionStatus.COMPLETE
                    ),
                    state=state,
                    failures=failures,
                )

            if evaluation.decision == EvaluationDecision.REPLAN:
                if budget.replan_count >= budget.replan_limit:
                    return self._finalize(
                        task=task,
                        goal=goal,
                        mode=mode,
                        completion_status=CompletionStatus.PARTIAL,
                        state=state,
                        failures=[*failures, "replan_budget_exhausted"],
                    )
                try:
                    plan, budget = self._create_intelligent_plan(
                        goal,
                        budget,
                        revision=plan.revision + 1,
                        supersedes=plan.plan_id,
                        prior_state=state,
                    )
                    state.pop("_requires_replan_after_user_input", None)
                    self._invalidate_downstream_for_replan(state)
                    task = self._update_task(task_id, current_plan_id=plan.plan_id)
                    continue
                except Exception as exc:
                    self._emit_degraded(task_id, f"replan_{exc.__class__.__name__}")
                    return self._finalize(
                        task=task,
                        goal=goal,
                        mode=ExecutionMode.SAFE_DEGRADED,
                        completion_status=CompletionStatus.PARTIAL,
                        state=state,
                        failures=[*failures, "replan_failed"],
                    )
            if evaluation.decision == EvaluationDecision.BLOCK:
                failures.extend(evaluation.unresolved_gaps or ["orchestrator_blocked"])
                status = CompletionStatus.BLOCKED
            elif evaluation.decision in {
                EvaluationDecision.ASK_USER,
                EvaluationDecision.REQUEST_CONFIRMATION,
                EvaluationDecision.CONTINUE,
            }:
                failures.append(f"unresolved_evaluation:{evaluation.decision.value}")
                status = CompletionStatus.PARTIAL
            else:
                status = (
                    CompletionStatus.PARTIAL
                    if evaluation.decision == EvaluationDecision.COMPLETE_PARTIAL
                    or failures
                    else CompletionStatus.COMPLETE
                )
            if status != CompletionStatus.BLOCKED:
                try:
                    state, budget, postflight_failures = self._run_postflight(
                        task=self.service.get_task(task_id),
                        goal=goal,
                        plan=plan,
                        state=state,
                        budget=budget,
                    )
                    failures.extend(postflight_failures)
                    if postflight_failures:
                        status = CompletionStatus.PARTIAL
                except RuntimeBudgetExceeded as exc:
                    failures.append(f"budget_exhausted:{exc.budget_name}")
                    status = CompletionStatus.PARTIAL
                except Exception as exc:
                    failures.append(f"postflight:{exc.__class__.__name__}")
                    status = CompletionStatus.BLOCKED
            return self._finalize(
                task=task,
                goal=goal,
                mode=mode,
                completion_status=status,
                state=state,
                failures=failures,
            )

    def _run_deterministic_preflight(
        self,
        *,
        task: RadarAgentTask,
        goal: UserGoal,
        state: dict[str, Any],
        budget: RuntimeBudget,
    ) -> tuple[dict[str, Any], RuntimeBudget]:
        specs: list[tuple[str, str]] = []
        if goal.goal_type == GoalType.GROUNDED_QUESTION:
            specs.append(("history.query", "read_explicit_source_artifact"))
        else:
            specs.extend(
                [
                    ("radar.read", "read_exact_scope"),
                    ("quality.assess", "quality_gate"),
                ]
            )
            if goal.goal_type in {
                GoalType.TREND_COMPARISON,
                GoalType.CHANGE_EXPLANATION,
            }:
                specs.extend(
                    [
                        ("history.query", "read_exact_history_range"),
                        ("trend.calculate", "calculate_canonical_trend"),
                    ]
                )
            specs.append(
                ("urgent_boundary.evaluate", "apply_urgent_text_and_risk_boundary")
            )
        steps = [
            PlanStep(
                step_id=f"preflight-{index:02d}",
                ordinal=index,
                kind=PlanStepKind.TOOL,
                capability=capability,
                tool_name=tool_name,
                depends_on=[f"preflight-{index - 1:02d}"] if index > 1 else [],
                rationale_summary="Deterministic safety preflight before model planning.",
            )
            for index, (tool_name, capability) in enumerate(specs, 1)
        ]
        preflight = ExecutionPlan(
            plan_id=f"preflight:{task.task_id}",
            task_id=task.task_id,
            goal_id=goal.goal_id,
            revision=1,
            objective="Gather the minimum canonical evidence required for safe planning.",
            assumptions=["This fixed middleware cannot publish an Agent conclusion."],
            steps=steps,
            completion_criteria=["Canonical evidence availability and safety boundaries are known."],
            change_summary="Deterministic pre-model safety middleware.",
        )
        completed = set(state.get("_completed_capabilities", []))
        for step in steps:
            if step.capability in completed and step.tool_name in state:
                continue
            output, budget = self._invoke_tool(
                task=task,
                goal=goal,
                plan=preflight,
                step=step,
                state=state,
                budget=budget,
            )
            state[step.tool_name] = output
            completed.add(step.capability)
            state["_completed_capabilities"] = sorted(completed)
            self._checkpoint(task.task_id, state)
        self.service.emit_event(
            task.task_id,
            event_type="execution.preflight_completed",
            message="Deterministic evidence and safety preflight completed before model planning.",
            payload={"capabilities": [step.capability for step in steps]},
        )
        return state, budget

    def _run_explicit_urgent_preflight(
        self,
        *,
        task: RadarAgentTask,
        goal: UserGoal,
        state: dict[str, Any],
        budget: RuntimeBudget,
    ) -> tuple[dict[str, Any], RuntimeBudget]:
        step = PlanStep(
            step_id="preflight-urgent-text",
            ordinal=1,
            kind=PlanStepKind.TOOL,
            capability="apply_explicit_urgent_text_boundary",
            tool_name="urgent_boundary.evaluate",
            purpose="Check explicit user text before any model invocation.",
        )
        middleware = ExecutionPlan(
            plan_id=f"preflight-urgent:{task.task_id}",
            task_id=task.task_id,
            goal_id=goal.goal_id,
            revision=1,
            objective="Apply the deterministic urgent-text boundary.",
            assumptions=["This middleware cannot publish an Agent conclusion."],
            steps=[step],
            completion_criteria=["Explicit urgent text boundary is known."],
        )
        output, budget = self._invoke_tool(
            task=task,
            goal=goal,
            plan=middleware,
            step=step,
            state=state,
            budget=budget,
        )
        state["urgent_boundary.evaluate"] = output
        self._checkpoint(task.task_id, state)
        return state, budget

    def _run_postflight(
        self,
        *,
        task: RadarAgentTask,
        goal: UserGoal,
        plan: ExecutionPlan,
        state: dict[str, Any],
        budget: RuntimeBudget,
    ) -> tuple[dict[str, Any], RuntimeBudget, list[str]]:
        """Run non-negotiable publication gates outside the visible Agent plan."""

        specs: list[tuple[str, str]] = []
        if state.get("user_input_responses"):
            specs.append(("questionnaire.capture", "capture_reviewed_user_context"))
        specs.extend(
            [
                ("evidence.ledger_validate", "validate_claim_evidence_integrity"),
                ("evidence.ledger_commit", "commit_fact_snapshot"),
                ("role_artifact.render", "render_requested_role_artifact"),
            ]
        )
        if goal.goal_type == GoalType.DOCTOR_MATERIAL:
            specs.extend(
                [
                    ("doctor_material.render", "render_requested_doctor_material"),
                    ("confirmation.request", "request_export_confirmation"),
                ]
            )
        completed: list[str] = []
        for index, (tool_name, capability) in enumerate(specs, 1):
            if tool_name in state:
                completed.append(capability)
                continue
            step = PlanStep(
                step_id=f"postflight-{index:02d}-{capability}",
                ordinal=index,
                kind=PlanStepKind.TOOL,
                capability=capability,
                tool_name=tool_name,
                purpose="Mandatory deterministic publication and action-safety gate.",
                rationale_summary="Hard safety middleware; not an Agent judgment step.",
            )
            output, budget = self._invoke_tool(
                task=task,
                goal=goal,
                plan=plan,
                step=step,
                state=state,
                budget=budget,
            )
            state[tool_name] = output
            completed.append(capability)
            self._checkpoint(task.task_id, state)
        self.service.emit_event(
            task.task_id,
            event_type="execution.postflight_completed",
            message="Deterministic evidence, publication, and confirmation gates completed.",
            payload={"capabilities": completed},
        )
        return state, budget, []

    def _create_intelligent_plan(
        self,
        goal: UserGoal,
        budget: RuntimeBudget,
        *,
        revision: int,
        supersedes: str | None = None,
        prior_state: dict[str, Any] | None = None,
    ) -> tuple[ExecutionPlan, RuntimeBudget]:
        budget = budget.consume("initial_plan" if revision == 1 else "replan")
        self.store.save_runtime_budget(budget)
        evidence_summary: dict[str, Any] = {
            "status": "authorized_canonical_read_required",
            "exact_date_scope_present": bool(
                goal.target_date
                or (goal.range_start and goal.range_end)
                or goal.source_date
            ),
            "explicit_source_selected": bool(goal.source_artifact_id),
        }
        if prior_state:
            evidence_summary["completed_capability_keys"] = sorted(
                key for key in prior_state if not key.startswith("_")
            )
            quality = prior_state.get("quality.assess", {}).get("night_summary")
            if quality is not None:
                evidence_summary.update(
                    {
                        "status": "canonical_preflight_complete",
                        "data_quality_status": quality.data_quality_status.value,
                        "data_coverage_ratio": quality.data_coverage_ratio,
                        "health_conclusion_allowed": quality.health_conclusion_allowed,
                    }
                )
            risk = prior_state.get("urgent_boundary.evaluate", {}).get(
                "risk_decision"
            )
            if risk is not None:
                evidence_summary["deterministic_risk_level"] = risk.risk_level.value
            trend = prior_state.get("trend.calculate", {}).get("trend_result")
            if trend is not None:
                evidence_summary["trend_claim_count"] = len(trend.claims)
                evidence_summary["trend_uncertainties"] = trend.uncertainties
        messages = planner_messages(
            goal=_safe_goal_context(goal),
            capability_manifest={
                "agents": capability_agent_manifest(),
                "tools": dynamic_tool_manifest(),
                "hard_safety_middleware": [
                    "identity_and_role_binding",
                    "canonical_evidence_and_quality_gate",
                    "deterministic_risk_boundary",
                    "ledger_validation_and_fact_snapshot",
                    "confirmation_before_external_action",
                ],
                "runtime_budget": budget.model_dump(mode="json"),
                "role_permissions": {
                    "requested_role": goal.requested_role,
                    "allowed_action_scope": goal.allowed_action_scope,
                },
            },
            evidence_summary=evidence_summary,
        )
        logical_key = f"plan:{revision}"
        while True:
            proposal, budget, invocation_id = self._invoke_model(
                task_id=goal.task_id,
                purpose="plan",
                agent=AgentKind.ORCHESTRATOR,
                schema=PlanProposal,
                messages=messages,
                prompt_version="dynamic-orchestrator-plan.v1",
                logical_key=logical_key,
                budget=budget,
            )
            try:
                if proposal.goal_type != goal.goal_type:
                    raise ValueError("planner changed the accepted goal type")
                plan = ExecutionPlan(
                    plan_id=f"plan-{self._id_factory()}",
                    task_id=goal.task_id,
                    goal_id=goal.goal_id,
                    revision=revision,
                    objective=proposal.plan_summary,
                    assumptions=["Only authorized canonical evidence is in scope."],
                    steps=normalize_plan(
                        proposal,
                        goal,
                        safety_review_required=self._safety_review_needed(
                            prior_state or {}
                        ),
                    ),
                    completion_criteria=[
                        "Requested output is backed by a validated Evidence Ledger.",
                        "All deterministic safety and role gates pass.",
                    ],
                    created_by_invocation_id=invocation_id,
                    supersedes_plan_id=supersedes,
                    change_summary=proposal.plan_summary,
                )
                break
            except (ValidationError, ValueError) as exc:
                attempts = [
                    item
                    for item in self.store.list_model_invocations(goal.task_id)
                    if item.client_idempotency_key
                    == f"{goal.task_id}:model:{logical_key}"
                ]
                self.service.emit_event(
                    goal.task_id,
                    event_type="plan.rejected",
                    message="Planner output was rejected by deterministic policy validation.",
                    payload={
                        "revision": revision,
                        "attempt": len(attempts),
                        "error_code": exc.__class__.__name__,
                    },
                )
                if len(attempts) >= 2:
                    raise
        self.store.save_execution_plan(plan)
        self.service.emit_event(
            goal.task_id,
            event_type="plan.created" if revision == 1 else "plan.revised",
            message="Orchestrator created a bounded execution plan.",
            payload={
                "plan_id": plan.plan_id,
                "revision": revision,
                "capabilities": [step.capability for step in plan.steps],
                "supersedes_plan_id": supersedes,
            },
        )
        return plan, budget

    def _create_degraded_plan(
        self,
        goal: UserGoal,
        budget: RuntimeBudget,
        *,
        revision: int,
        supersedes: str | None = None,
    ) -> ExecutionPlan:
        if revision == 1 and budget.initial_plan_count == 0:
            budget = budget.consume("initial_plan")
        elif revision > 1:
            budget = budget.consume("replan")
        self.store.save_runtime_budget(budget)
        plan = ExecutionPlan(
            plan_id=f"plan-{self._id_factory()}",
            task_id=goal.task_id,
            goal_id=goal.goal_id,
            revision=revision,
            objective=f"Complete {goal.goal_type.value} with deterministic safe capabilities.",
            assumptions=["No successful structured model call is available."],
            steps=degraded_steps(goal),
            completion_criteria=[
                "Return only outputs supported by deterministic evidence and policy."
            ],
            supersedes_plan_id=supersedes,
            change_summary="Safe degraded deterministic capability plan.",
        )
        self.store.save_execution_plan(plan)
        self.service.emit_event(
            goal.task_id,
            event_type="plan.created" if revision == 1 else "plan.revised",
            message="Created a deterministic safe-degraded plan.",
            payload={
                "plan_id": plan.plan_id,
                "revision": revision,
                "supersedes_plan_id": supersedes,
                "capabilities": [step.capability for step in plan.steps],
            },
        )
        return plan

    def _execute_plan(
        self,
        *,
        task: RadarAgentTask,
        goal: UserGoal,
        plan: ExecutionPlan,
        budget: RuntimeBudget,
        state: dict[str, Any],
        mode: ExecutionMode,
    ) -> tuple[
        dict[str, Any],
        RuntimeBudget,
        bool,
        list[str],
        EvaluationOutcome | None,
    ]:
        records = {item.step.step_id: item for item in self.store.list_dynamic_steps(plan.plan_id)}
        failures: list[str] = []
        if state.get("_preflight_failure"):
            failures.append(f"preflight:{state['_preflight_failure']}")
        if state.get("_urgent_interrupted"):
            failures.append("urgent_boundary_interrupted")
        for step in plan.steps:
            budget.assert_time_available()
            record = records.get(step.step_id)
            if record is not None and record.status == "succeeded":
                continue
            if step.capability in set(state.get("_completed_capabilities", [])):
                reused = DynamicStepRecord(
                    task_id=task.task_id,
                    plan_id=plan.plan_id,
                    step=step,
                    status="succeeded",
                    output={"reused_from_checkpoint": True},
                    finished_at=datetime.now(timezone.utc),
                )
                self.store.save_dynamic_step(reused)
                records[step.step_id] = reused
                self.service.emit_event(
                    task.task_id,
                    event_type="plan.step_reused",
                    message=f"Reused completed capability after replan: {step.capability}.",
                    payload={
                        "plan_id": plan.plan_id,
                        "capability": step.capability,
                        "reused_from_plan_id": plan.supersedes_plan_id,
                    },
                )
                continue
            if step.run_if == "risk_review_required" and not self._safety_review_needed(state):
                skipped = DynamicStepRecord(
                    task_id=task.task_id,
                    plan_id=plan.plan_id,
                    step=step,
                    status="skipped",
                    output={"reason": "conditional_safety_review_not_required"},
                    finished_at=datetime.now(timezone.utc),
                )
                self.store.save_dynamic_step(skipped)
                records[step.step_id] = skipped
                self.service.emit_event(
                    task.task_id,
                    event_type="plan.step_skipped",
                    message=f"Skipped capability because its reviewer trigger was absent: {step.capability}.",
                    payload={
                        "plan_id": plan.plan_id,
                        "capability": step.capability,
                        "skip_reason": "conditional_safety_review_not_required",
                    },
                )
                continue
            if any(
                records.get(dep) is not None and records[dep].status == "failed"
                for dep in step.depends_on
            ):
                skipped = DynamicStepRecord(
                    task_id=task.task_id,
                    plan_id=plan.plan_id,
                    step=step,
                    status="skipped",
                    output={"reason": "dependency_failed"},
                    finished_at=datetime.now(timezone.utc),
                )
                self.store.save_dynamic_step(skipped)
                records[step.step_id] = skipped
                failures.append(f"{step.capability}:dependency_failed")
                self.service.emit_event(
                    task.task_id,
                    event_type="plan.step_skipped",
                    message=f"Skipped capability because a declared dependency failed: {step.capability}.",
                    payload={
                        "plan_id": plan.plan_id,
                        "capability": step.capability,
                        "skip_reason": "dependency_failed",
                    },
                )
                continue
            running = DynamicStepRecord(
                task_id=task.task_id,
                plan_id=plan.plan_id,
                step=step,
                status="running",
                started_at=datetime.now(timezone.utc),
            )
            self.store.save_dynamic_step(running)
            try:
                if step.kind == PlanStepKind.TOOL:
                    output, budget = self._invoke_tool(
                        task=task,
                        goal=goal,
                        plan=plan,
                        step=step,
                        state=state,
                        budget=budget,
                    )
                    state[step.tool_name] = output
                elif step.kind == PlanStepKind.AGENT:
                    if mode != ExecutionMode.INTELLIGENT:
                        raise RuntimeError("capability Agent cannot run in degraded mode")
                    product, budget, invocation = self._invoke_agent(
                        task=task,
                        goal=goal,
                        plan=plan,
                        step=step,
                        state=state,
                        budget=budget,
                    )
                    state.setdefault("agent_products", []).append(product)
                    state[f"agent:{step.agent.value}"] = product
                    output = product.model_dump(mode="json")
                    paused, budget = self._maybe_request_user_input(
                        task=task,
                        goal=goal,
                        plan=plan,
                        source_step=step,
                        source_invocation=invocation,
                        product=product,
                        state=state,
                        budget=budget,
                    )
                    if paused:
                        completed = running.model_copy(
                            update={
                                "status": "succeeded",
                                "output": output,
                                "finished_at": datetime.now(timezone.utc),
                            }
                        )
                        self.store.save_dynamic_step(completed)
                        records[step.step_id] = completed
                        state.setdefault("_completed_capabilities", []).append(
                            step.capability
                        )
                        state["_completed_capabilities"] = list(
                            dict.fromkeys(state["_completed_capabilities"])
                        )
                        self._checkpoint(task.task_id, state)
                        return state, budget, True, failures, None
                else:
                    output = {}
                completed = running.model_copy(
                    update={
                        "status": "succeeded",
                        "output": json_safe(output),
                        "finished_at": datetime.now(timezone.utc),
                    }
                )
                self.store.save_dynamic_step(completed)
                records[step.step_id] = completed
                state.setdefault("_completed_capabilities", []).append(step.capability)
                state["_completed_capabilities"] = list(
                    dict.fromkeys(state["_completed_capabilities"])
                )
                self._checkpoint(task.task_id, state)
                if mode == ExecutionMode.INTELLIGENT and step.kind == PlanStepKind.AGENT:
                    pending_a2a = state.pop("_pending_a2a_evaluation", None)
                    if pending_a2a is not None:
                        return state, budget, False, failures, pending_a2a
                    evaluation, budget = self._evaluate(
                        goal=goal,
                        plan=plan,
                        budget=budget,
                        state=state,
                        failures=failures,
                        checkpoint_kind=f"agent:{step.step_id}",
                    )
                    if evaluation.decision != EvaluationDecision.CONTINUE:
                        return state, budget, False, failures, evaluation
            except Exception as exc:
                failed = running.model_copy(
                    update={
                        "status": "failed",
                        "output": {"error_code": exc.__class__.__name__},
                        "finished_at": datetime.now(timezone.utc),
                    }
                )
                self.store.save_dynamic_step(failed)
                records[step.step_id] = failed
                failures.append(f"{step.capability}:{exc.__class__.__name__}")
                if mode == ExecutionMode.INTELLIGENT and not state.get("_model_failure"):
                    try:
                        evaluation, budget = self._evaluate(
                            goal=goal,
                            plan=plan,
                            budget=budget,
                            state=state,
                            failures=failures,
                            checkpoint_kind=f"error:{step.step_id}",
                        )
                        if evaluation.decision != EvaluationDecision.CONTINUE:
                            return state, budget, False, failures, evaluation
                    except Exception as evaluation_exc:
                        state["_model_failure"] = evaluation_exc.__class__.__name__
                if step.required and mode == ExecutionMode.SAFE_DEGRADED:
                    break
        return state, budget, False, failures, None

    def _invoke_tool(
        self,
        *,
        task: RadarAgentTask,
        goal: UserGoal,
        plan: ExecutionPlan,
        step: PlanStep,
        state: dict[str, Any],
        budget: RuntimeBudget,
    ) -> tuple[dict[str, Any], RuntimeBudget]:
        assert step.tool_name is not None
        definition = self.toolbox.definition(step.tool_name)
        if getattr(self.service, "_require_authorization", False):
            self.service.assert_task_access(
                task.task_id,
                actor_id=goal.requesting_actor_id,
                actor_role=goal.requested_role,
                permission=definition.required_permission,
            )
        budget.assert_time_available()
        budget = budget.consume("deterministic_tool_call")
        self.store.save_runtime_budget(budget)
        idempotency_key = f"{task.task_id}:tool:{step.capability}"
        prior_attempts = [
            item.attempt
            for item in self.store.list_tool_invocations(task.task_id)
            if item.client_idempotency_key == idempotency_key
        ]
        invocation = ToolInvocation(
            invocation_id=f"tool-inv-{self._id_factory()}",
            task_id=task.task_id,
            plan_id=plan.plan_id,
            step_id=step.step_id,
            tool_name=step.tool_name,
            tool_schema_version=definition.version,
            input_schema=definition.input_schema,
            output_schema=definition.output_schema,
            read_write_class=definition.read_write_class,
            required_permission=definition.required_permission,
            confirmation_policy=definition.confirmation_policy,
            timeout_seconds=definition.timeout_seconds,
            idempotency_behavior=definition.idempotency_behavior,
            audit_policy=definition.audit_policy,
            parent_invocation_id=plan.created_by_invocation_id,
            context_packet_id=f"dynamic:{task.task_id}:tool:{step.capability}",
            client_idempotency_key=idempotency_key,
            attempt=max(prior_attempts, default=0) + 1,
            outcome=InvocationOutcome.PENDING,
        )
        self.store.save_tool_invocation(invocation)
        self.service.emit_event(
            task.task_id,
            event_type="tool.invocation_started",
            message=f"Started deterministic capability: {step.capability}.",
            payload={
                "invocation_id": invocation.invocation_id,
                "tool_name": step.tool_name,
                "capability": step.capability,
            },
        )
        self._checkpoint(task.task_id, state)
        try:
            output = self.toolbox.execute(
                step.tool_name, task=task, goal=goal, state=state
            )
        except BaseException as exc:
            outcome = (
                InvocationOutcome.FAILED
                if isinstance(exc, Exception)
                else InvocationOutcome.UNKNOWN_OUTCOME
            )
            finished_at = datetime.now(timezone.utc)
            self.store.save_tool_invocation(
                invocation.model_copy(
                    update={
                        "outcome": outcome,
                        "error_code": exc.__class__.__name__,
                        "validation_result": "invalid",
                        "finished_at": finished_at,
                        "latency_ms": max(
                            0,
                            int((finished_at - invocation.started_at).total_seconds() * 1000),
                        ),
                    }
                )
            )
            self.service.emit_event(
                task.task_id,
                event_type="tool.invocation_failed",
                message=f"Deterministic capability failed: {step.capability}.",
                payload={"invocation_id": invocation.invocation_id, "error_code": exc.__class__.__name__},
            )
            raise
        finished_at = datetime.now(timezone.utc)
        self.store.save_tool_invocation(
            invocation.model_copy(
                update={
                    "outcome": InvocationOutcome.SUCCEEDED,
                    "output_summary": {"output_keys": sorted(output)},
                    "safe_summary": f"Completed {step.capability} with schema-valid output.",
                    "evidence_refs": self._available_evidence_refs(
                        {**state, step.tool_name: output}
                    ),
                    "validation_result": "valid",
                    "finished_at": finished_at,
                    "latency_ms": max(
                        0,
                        int((finished_at - invocation.started_at).total_seconds() * 1000),
                    ),
                }
            )
        )
        self.service.emit_event(
            task.task_id,
            event_type="tool.invocation_completed",
            message=f"Completed deterministic capability: {step.capability}.",
            payload={
                "invocation_id": invocation.invocation_id,
                "tool_name": step.tool_name,
                "capability": step.capability,
                "output_keys": sorted(output),
            },
        )
        return output, budget

    def _invoke_agent(
        self,
        *,
        task: RadarAgentTask,
        goal: UserGoal,
        plan: ExecutionPlan,
        step: PlanStep,
        state: dict[str, Any],
        budget: RuntimeBudget,
        caused_by: A2AMessage | None = None,
        allow_a2a: bool = True,
    ) -> tuple[AgentWorkProduct, RuntimeBudget, AgentInvocation]:
        assert step.agent is not None
        if step.agent not in ALLOWED_CAPABILITY_AGENTS:
            raise PermissionError("invalid dynamic capability Agent")
        budget.assert_time_available()
        budget = budget.consume("capability_agent_call")
        self.store.save_runtime_budget(budget)
        logical_key = (
            f"agent:{step.step_id}"
            if caused_by is None
            else f"a2a:{caused_by.message_id}:{step.agent.value}"
        )
        output_schema = AGENT_OUTPUT_SCHEMAS[step.agent]
        invocation = AgentInvocation(
            invocation_id=f"agent-inv-{self._id_factory()}",
            task_id=task.task_id,
            plan_id=plan.plan_id,
            step_id=step.step_id,
            agent=step.agent,
            agent_schema_version=f"{output_schema.__name__}.v1",
            context_packet_id=f"dynamic:{task.task_id}:{logical_key}",
            call_reason=step.capability,
            allowed_tool_scope=agent_tool_scope(goal.goal_type),
            status=AgentInvocationStatus.RUNNING,
            caused_by_a2a_message_id=caused_by.message_id if caused_by else None,
            parent_invocation_id=(
                caused_by.source_agent_invocation_id
                if caused_by
                else plan.created_by_invocation_id
            ),
        )
        self.store.save_agent_invocation(invocation)
        self.service.emit_event(
            task.task_id,
            event_type="agent.invocation_started",
            message=f"{step.agent.value} Agent started capability work.",
            payload={
                "invocation_id": invocation.invocation_id,
                "agent": step.agent.value,
                "capability": step.capability,
                "caused_by_a2a_message_id": invocation.caused_by_a2a_message_id,
            },
        )
        evidence = self._agent_evidence(state)
        messages = agent_messages(
            agent=step.agent,
            goal=_safe_goal_context(goal),
            evidence=evidence,
            accepted_a2a=(caused_by.model_dump(mode="json") if caused_by else None),
        )
        self._checkpoint(task.task_id, state)
        try:
            product, budget, model_invocation_id = self._invoke_model(
                task_id=task.task_id,
                purpose="agent",
                agent=step.agent,
                schema=output_schema,
                messages=messages,
                prompt_version=f"dynamic-agent-{step.agent.value}.v1",
                logical_key=logical_key,
                budget=budget,
            )
            allowed_refs = set(self._available_evidence_refs(state))
            product = product.model_copy(
                update={
                    "evidence_refs": [ref for ref in product.evidence_refs if ref in allowed_refs]
                }
            )
            finished_at = datetime.now(timezone.utc)
            model_invocation = next(
                item
                for item in self.store.list_model_invocations(task.task_id)
                if item.invocation_id == model_invocation_id
            )
            completed = invocation.model_copy(
                update={
                    "status": AgentInvocationStatus.SUCCEEDED,
                    "model_invocation_id": model_invocation_id,
                    "model_provider": model_invocation.model_provider,
                    "model_id": model_invocation.model_id,
                    "provider_request_id": model_invocation.provider_request_id,
                    "accepted_a2a_message_id": caused_by.message_id if caused_by else None,
                    "evidence_refs": product.evidence_refs,
                    "validation_result": "valid",
                    "safe_summary": product.summary,
                    "finished_at": finished_at,
                    "latency_ms": max(
                        0,
                        int((finished_at - invocation.started_at).total_seconds() * 1000),
                    ),
                }
            )
            self.store.save_agent_invocation(completed)
            self.service.emit_event(
                task.task_id,
                event_type="agent.invocation_completed",
                message=f"{step.agent.value} Agent completed capability work.",
                payload={
                    "invocation_id": invocation.invocation_id,
                    "agent": step.agent.value,
                    "capability": step.capability,
                    "confidence": product.confidence,
                    "uncertainty_count": len(product.uncertainties),
                },
            )
            if allow_a2a:
                product, budget = self._handle_a2a_requests(
                    task=task,
                    goal=goal,
                    plan=plan,
                    source_step=step,
                    source_invocation=completed,
                    source_product=product,
                    state=state,
                    budget=budget,
                )
            return product, budget, completed
        except BaseException as exc:
            state["_model_failure"] = exc.__class__.__name__
            status = (
                AgentInvocationStatus.FAILED
                if isinstance(exc, Exception)
                else AgentInvocationStatus.UNKNOWN_OUTCOME
            )
            finished_at = datetime.now(timezone.utc)
            self.store.save_agent_invocation(
                invocation.model_copy(
                    update={
                        "status": status,
                        "error_summary": exc.__class__.__name__,
                        "validation_result": "invalid",
                        "finished_at": finished_at,
                        "latency_ms": max(
                            0,
                            int((finished_at - invocation.started_at).total_seconds() * 1000),
                        ),
                    }
                )
            )
            self.service.emit_event(
                task.task_id,
                event_type="agent.invocation_failed",
                message=f"{step.agent.value} Agent capability failed.",
                payload={"invocation_id": invocation.invocation_id, "error_code": exc.__class__.__name__},
            )
            raise

    def _handle_a2a_requests(
        self,
        *,
        task: RadarAgentTask,
        goal: UserGoal,
        plan: ExecutionPlan,
        source_step: PlanStep,
        source_invocation: AgentInvocation,
        source_product: AgentWorkProduct,
        state: dict[str, Any],
        budget: RuntimeBudget,
    ) -> tuple[AgentWorkProduct, RuntimeBudget]:
        combined = source_product
        for index, request in enumerate(source_product.a2a_requests[:2], 1):
            if (
                request.receiver not in ALLOWED_CAPABILITY_AGENTS
                or not agent_allowed_for_goal(request.receiver, goal.goal_type)
                or request.receiver == source_step.agent
                or budget.capability_agent_call_count >= budget.capability_agent_call_limit
            ):
                self.service.emit_event(
                    task.task_id,
                    event_type="a2a.rejected",
                    message="Peer capability request was rejected by deterministic policy.",
                    payload={"reason_code": "scope_or_budget_rejected"},
                )
                continue
            allowed_refs = set(self._available_evidence_refs(state))
            refs = [ref for ref in request.evidence_refs if ref in allowed_refs]
            duplicate = next(
                (
                    item
                    for item in self.store.list_a2a_messages(task.task_id)
                    if item.sender == source_step.agent.value
                    and item.receiver == request.receiver.value
                    and item.request_type == request.request_type
                    and sorted(item.evidence_refs) == sorted(refs)
                ),
                None,
            )
            if duplicate is not None:
                self.service.emit_event(
                    task.task_id,
                    event_type="a2a.rejected",
                    message="Duplicate peer capability request was not executed again.",
                    payload={"message_id": duplicate.message_id, "reason_code": "duplicate"},
                )
                continue
            message = A2AMessage(
                message_id=f"a2a-{self._id_factory()}",
                sender=source_step.agent.value,
                receiver=request.receiver.value,
                task_id=task.task_id,
                intent=request.intent,
                evidence_refs=refs,
                confidence=source_product.confidence,
                requested_action=request.requested_capability,
                request_type=request.request_type,
                risk_level=RiskLevel.INFO,
                collaboration_round=1,
                payload={
                    "source_agent_invocation_id": source_invocation.invocation_id,
                    "requested_capability": request.requested_capability,
                },
                source_agent_invocation_id=source_invocation.invocation_id,
                caused_by_invocation_id=source_invocation.invocation_id,
                expected_output_schema=AGENT_OUTPUT_SCHEMAS[
                    request.receiver
                ].__name__,
            )
            self.service.emit_event(
                task.task_id,
                event_type="a2a.requested",
                message="A capability Agent requested bounded peer review.",
                payload={
                    "message_id": message.message_id,
                    "sender": message.sender,
                    "receiver": message.receiver,
                    "intent": message.intent,
                    "request_type": message.request_type,
                },
            )
            forwarded = self.service.forward_a2a_message(
                task.task_id, message, approved_by_orchestrator=True
            )
            forwarded = forwarded.model_copy(
                update={"accepted_by_orchestrator_at": datetime.now(timezone.utc)}
            )
            self.store.save_a2a_message(forwarded)
            self.service.emit_event(
                task.task_id,
                event_type="a2a.accepted",
                message="Orchestrator accepted the peer capability request.",
                payload={
                    "message_id": forwarded.message_id,
                    "source_agent_invocation_id": source_invocation.invocation_id,
                },
            )
            target_step = PlanStep(
                step_id=f"{source_step.step_id}:a2a:{index}",
                ordinal=source_step.ordinal,
                kind=PlanStepKind.AGENT,
                capability=request.requested_capability,
                agent=request.receiver,
                depends_on=[source_step.step_id],
            )
            try:
                target_product, budget, target_invocation = self._invoke_agent(
                    task=task,
                    goal=goal,
                    plan=plan,
                    step=target_step,
                    state=state,
                    budget=budget,
                    caused_by=forwarded,
                    allow_a2a=False,
                )
            except Exception:
                self.store.save_a2a_message(
                    forwarded.model_copy(
                        update={
                            "message_status": "rejected",
                            "resolution_status": "rejected",
                            "resolution_summary": "Target capability invocation failed safely.",
                        }
                    )
                )
                self.service.emit_event(
                    task.task_id,
                    event_type="a2a.rejected",
                    message="Accepted peer request could not complete its target invocation.",
                    payload={"message_id": forwarded.message_id, "reason_code": "target_failed"},
                )
                continue
            handled = forwarded.model_copy(
                update={
                    "message_status": "handled",
                    "target_agent_invocation_id": target_invocation.invocation_id,
                    "target_invocation_id": target_invocation.invocation_id,
                    "resolution_status": target_product.a2a_disposition,
                    "resolution_summary": target_product.a2a_resolution_summary
                    or "Target Agent returned a schema-valid bounded result.",
                    "handled_at": datetime.now(timezone.utc),
                }
            )
            self.store.save_a2a_message(handled)
            changed_fields = _a2a_changed_fields(source_product, target_product)
            state.setdefault("a2a_outcomes", []).append(
                {
                    "message_id": handled.message_id,
                    "resolution_status": handled.resolution_status,
                    "changed_fields": changed_fields,
                    "source_invocation_id": source_invocation.invocation_id,
                    "target_invocation_id": target_invocation.invocation_id,
                }
            )
            self.service.emit_event(
                task.task_id,
                event_type="a2a.resolved",
                message="Target capability Agent handled the accepted peer request.",
                payload={
                    "message_id": handled.message_id,
                    "target_agent_invocation_id": target_invocation.invocation_id,
                    "causal_source_agent_invocation_id": source_invocation.invocation_id,
                    "sender": handled.sender,
                    "receiver": handled.receiver,
                    "resolution_status": handled.resolution_status,
                    "changed_fields": changed_fields,
                },
            )
            state.setdefault("agent_products", []).append(target_product)
            target_evaluation, budget = self._evaluate(
                goal=goal,
                plan=plan,
                budget=budget,
                state=state,
                failures=[],
                checkpoint_kind=f"a2a:{target_invocation.invocation_id}",
            )
            if target_evaluation.decision != EvaluationDecision.CONTINUE:
                state["_pending_a2a_evaluation"] = target_evaluation
            revision = next(
                (
                    item
                    for item in target_product.a2a_requests
                    if item.request_type == "revision"
                    and item.receiver == source_step.agent
                ),
                None,
            )
            if revision is not None:
                if budget.capability_agent_call_count >= budget.capability_agent_call_limit:
                    self.service.emit_event(
                        task.task_id,
                        event_type="a2a.rejected",
                        message="Requested analyst revision was rejected at the Agent-call budget.",
                        payload={"reason_code": "capability_agent_budget_exhausted"},
                    )
                else:
                    revision_refs = [
                        ref
                        for ref in revision.evidence_refs
                        if ref in set(self._available_evidence_refs(state))
                    ]
                    revision_message = A2AMessage(
                        message_id=f"a2a-{self._id_factory()}",
                        sender=request.receiver.value,
                        receiver=source_step.agent.value,
                        task_id=task.task_id,
                        intent=revision.intent,
                        evidence_refs=revision_refs,
                        confidence=target_product.confidence,
                        requested_action=revision.requested_capability,
                        request_type="revision",
                        risk_level=RiskLevel.INFO,
                        collaboration_round=2,
                        payload={
                            "source_agent_invocation_id": target_invocation.invocation_id,
                            "requested_capability": revision.requested_capability,
                        },
                        source_agent_invocation_id=target_invocation.invocation_id,
                        caused_by_invocation_id=target_invocation.invocation_id,
                        expected_output_schema=AGENT_OUTPUT_SCHEMAS[
                            source_step.agent
                        ].__name__,
                    )
                    revision_message = self.service.forward_a2a_message(
                        task.task_id,
                        revision_message,
                        approved_by_orchestrator=True,
                    ).model_copy(
                        update={
                            "accepted_by_orchestrator_at": datetime.now(
                                timezone.utc
                            )
                        }
                    )
                    self.store.save_a2a_message(revision_message)
                    self.service.emit_event(
                        task.task_id,
                        event_type="a2a.accepted",
                        message="Orchestrator accepted a reviewer-requested analyst revision.",
                        payload={
                            "message_id": revision_message.message_id,
                            "source_agent_invocation_id": target_invocation.invocation_id,
                            "request_type": "revision",
                        },
                    )
                    revision_step = PlanStep(
                        step_id=f"{source_step.step_id}:revision:{index}",
                        ordinal=source_step.ordinal,
                        kind=PlanStepKind.AGENT,
                        capability=revision.requested_capability,
                        agent=source_step.agent,
                        depends_on=[target_step.step_id],
                    )
                    revised_product, budget, revised_invocation = self._invoke_agent(
                        task=task,
                        goal=goal,
                        plan=plan,
                        step=revision_step,
                        state=state,
                        budget=budget,
                        caused_by=revision_message,
                        allow_a2a=False,
                    )
                    resolved_revision = revision_message.model_copy(
                        update={
                            "message_status": "handled",
                            "target_agent_invocation_id": revised_invocation.invocation_id,
                            "target_invocation_id": revised_invocation.invocation_id,
                            "resolution_status": revised_product.a2a_disposition,
                            "resolution_summary": revised_product.a2a_resolution_summary
                            or "The analyst returned a schema-valid revision.",
                            "handled_at": datetime.now(timezone.utc),
                        }
                    )
                    self.store.save_a2a_message(resolved_revision)
                    revision_changes = _a2a_changed_fields(
                        source_product, revised_product
                    )
                    state.setdefault("a2a_outcomes", []).append(
                        {
                            "message_id": resolved_revision.message_id,
                            "resolution_status": resolved_revision.resolution_status,
                            "changed_fields": revision_changes,
                            "source_invocation_id": target_invocation.invocation_id,
                            "target_invocation_id": revised_invocation.invocation_id,
                        }
                    )
                    state.setdefault("agent_products", []).append(revised_product)
                    self.service.emit_event(
                        task.task_id,
                        event_type="a2a.resolved",
                        message="Reviewer-requested analyst revision completed.",
                        payload={
                            "message_id": resolved_revision.message_id,
                            "target_agent_invocation_id": revised_invocation.invocation_id,
                            "causal_source_agent_invocation_id": target_invocation.invocation_id,
                            "sender": resolved_revision.sender,
                            "receiver": resolved_revision.receiver,
                            "resolution_status": resolved_revision.resolution_status,
                            "changed_fields": revision_changes,
                        },
                    )
                    revision_evaluation, budget = self._evaluate(
                        goal=goal,
                        plan=plan,
                        budget=budget,
                        state=state,
                        failures=[],
                        checkpoint_kind=f"a2a:{revised_invocation.invocation_id}",
                    )
                    if revision_evaluation.decision != EvaluationDecision.CONTINUE:
                        state["_pending_a2a_evaluation"] = revision_evaluation
                    combined = revised_product
            combined = combined.model_copy(
                update={
                    "findings": list(dict.fromkeys([*combined.findings, *target_product.findings])),
                    "uncertainties": list(
                        dict.fromkeys([*combined.uncertainties, *target_product.uncertainties])
                    ),
                    "candidate_actions": list(
                        dict.fromkeys([*combined.candidate_actions, *target_product.candidate_actions])
                    ),
                    "confidence": min(combined.confidence, target_product.confidence),
                }
            )
        return combined, budget

    def _maybe_request_user_input(
        self,
        *,
        task: RadarAgentTask,
        goal: UserGoal,
        plan: ExecutionPlan,
        source_step: PlanStep,
        source_invocation: AgentInvocation,
        product: AgentWorkProduct,
        state: dict[str, Any],
        budget: RuntimeBudget,
    ) -> tuple[bool, RuntimeBudget]:
        question_id = validate_requested_question(product.requested_question_id)
        if not product.needs_user_input or question_id is None:
            return False, budget
        questions = {
            **DEFAULT_QUESTIONNAIRE_BANK.questions,
            **DEFAULT_SKILL_QUESTION_PACK.questions,
        }
        question = questions.get(question_id)
        if question is None or task.role not in question.applicable_roles:
            return False, budget
        # Questionnaire selection is itself an audited deterministic tool call.
        state["requested_question_id"] = question_id
        synthetic = PlanStep(
            step_id=f"{source_step.step_id}:question",
            ordinal=source_step.ordinal,
            kind=PlanStepKind.TOOL,
            capability="select_reviewed_user_question",
            tool_name="questionnaire.select",
            depends_on=[source_step.step_id],
        )
        selected, budget = self._invoke_tool(
            task=task,
            goal=goal,
            plan=plan,
            step=synthetic,
            state=state,
            budget=budget,
        )
        if selected.get("question") is None:
            return False, budget
        question_type = (
            "symptom_self_report"
            if question_id == "q-daytime-sleepiness"
            else "simple_context"
            if question_id == "q-sleep-schedule-change"
            else "observable_fact"
        )
        request = UserInputRequest(
            request_id=f"user-input-{self._id_factory()}",
            task_id=task.task_id,
            question_id=question_id,
            question_version=(
                DEFAULT_SKILL_QUESTION_PACK.version
                if question_id in DEFAULT_SKILL_QUESTION_PACK.questions
                else DEFAULT_QUESTIONNAIRE_BANK.version
            ),
            question_text=question.role_text.get(task.role, question.text),
            question_type=question_type,
            target_role=task.role,
            answer_options=list(question.options),
            why_needed="用于解释当前证据缺口；不会要求专业测量或医学判断。",
            decision_scope="仅影响当前任务的证据解释、风险复核或是否继续观察。",
            blocks_task=True,
            asked_by_agent_invocation_id=source_invocation.invocation_id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            cooldown_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        self.store.save_user_input_request(request)
        self._update_task(
            task.task_id, pending_user_input_request_id=request.request_id
        )
        self.service.emit_event(
            task.task_id,
            event_type="user_input.requested",
            message="A reviewed observable-fact question is waiting for an answer.",
            payload={
                "request_id": request.request_id,
                "question_id": request.question_id,
                "question_type": request.question_type,
                "why_needed": request.why_needed,
            },
        )
        budget = budget.pause_clock()
        self.store.save_runtime_budget(budget)
        self.service.transition_task(
            task.task_id,
            RadarTaskStatus.WAITING_FOR_USER_INPUT,
            message="Execution paused for one reviewed user question.",
        )
        state["pending_user_input_request_id"] = request.request_id
        return True, budget

    def _evaluate(
        self,
        *,
        goal: UserGoal,
        plan: ExecutionPlan,
        budget: RuntimeBudget,
        state: dict[str, Any],
        failures: list[str],
        checkpoint_kind: str,
    ) -> tuple[EvaluationOutcome, RuntimeBudget]:
        result_rows = [
            {
                "step_id": record.step.step_id,
                "capability": record.step.capability,
                "status": record.status,
                "output_keys": sorted(record.output),
            }
            for record in self.store.list_dynamic_steps(plan.plan_id)
        ]
        self._checkpoint(goal.task_id, state)
        proposal, budget, invocation_id = self._invoke_model(
            task_id=goal.task_id,
            purpose="evaluate",
            agent=AgentKind.ORCHESTRATOR,
            schema=EvaluationProposal,
            messages=evaluation_messages(
                goal=_safe_goal_context(goal),
                step_results=result_rows,
                safe_state_summary=self._safe_state_summary(state),
                budget=budget.model_dump(mode="json"),
            ),
            prompt_version="dynamic-orchestrator-evaluate.v1",
            logical_key=(
                f"evaluate:{plan.revision}:{checkpoint_kind}:"
                f"{len(state.get('evaluations', [])) + 1}"
            ),
            budget=budget,
        )
        decision = EvaluationDecision(proposal.decision)
        if (
            checkpoint_kind == "goal_checkpoint"
            and state.get("_requires_replan_after_user_input")
        ):
            decision = EvaluationDecision.REPLAN
            proposal = proposal.model_copy(
                update={
                    "decision_summary": (
                        "A reviewed user answer added evidence, so the remaining "
                        "capabilities must be selected again."
                    ),
                    "missing_capabilities": ["adapt_to_reviewed_user_context"],
                    "replan_trigger": "new_evidence",
                }
            )
        has_safety_step = any(
            step.agent == AgentKind.SAFETY_REVIEW for step in plan.steps
        )
        if (
            checkpoint_kind.startswith("agent:")
            and self._safety_review_needed(state)
            and not has_safety_step
        ):
            decision = EvaluationDecision.REPLAN
            proposal = proposal.model_copy(
                update={
                    "decision_summary": (
                        "A deterministic reviewer trigger requires a SafetyReview "
                        "capability before publication."
                    ),
                    "missing_capabilities": ["review_safety_and_confirmation"],
                    "replan_trigger": "new_evidence",
                    "safety_flags": [
                        *proposal.safety_flags,
                        "deterministic_safety_review_trigger",
                    ],
                }
            )
        elif checkpoint_kind != "goal_checkpoint" and decision in {
            EvaluationDecision.COMPLETE,
            EvaluationDecision.COMPLETE_PARTIAL,
        }:
            decision = EvaluationDecision.CONTINUE
        if decision == EvaluationDecision.WAIT_FOR_USER_INPUT:
            # The model cannot invent a pause; only a persisted reviewed request can.
            decision = (
                EvaluationDecision.ASK_USER
                if state.get("pending_user_input_request_id")
                else EvaluationDecision.CONTINUE
            )
        if decision == EvaluationDecision.REQUEST_CONFIRMATION and not any(
            item.status == "pending"
            for item in self.service.list_confirmations(goal.task_id)
        ):
            decision = EvaluationDecision.CONTINUE
        if decision == EvaluationDecision.REPLAN and not self._accept_replan_trigger(
            proposal.replan_trigger,
            plan=plan,
            state=state,
        ):
            decision = EvaluationDecision.COMPLETE_PARTIAL
            proposal = proposal.model_copy(
                update={
                    "decision_summary": (
                        "Replan request was denied because no new evidence, conflict, "
                        "failure, changed goal, or policy rejection was present."
                    ),
                    "safety_flags": [*proposal.safety_flags, "invalid_replan_trigger"],
                }
            )
        if failures and decision == EvaluationDecision.COMPLETE:
            decision = EvaluationDecision.COMPLETE_PARTIAL
        outcome = EvaluationOutcome(
            evaluation_id=f"evaluation-{self._id_factory()}",
            task_id=goal.task_id,
            plan_id=plan.plan_id,
            decision=decision,
            decision_summary=proposal.decision_summary,
            reason_codes=[proposal.replan_trigger] if proposal.replan_trigger else [],
            evidence_refs=self._available_evidence_refs(state),
            unresolved_gaps=[*proposal.missing_capabilities, *failures],
            next_authorized_action=decision.value,
            missing_capabilities=proposal.missing_capabilities,
            safety_flags=proposal.safety_flags,
            created_by_invocation_id=invocation_id,
        )
        self.service.emit_event(
            goal.task_id,
            event_type="plan.evaluated",
            message="Orchestrator evaluated goal completion.",
            payload={
                "evaluation_id": outcome.evaluation_id,
                "plan_id": plan.plan_id,
                "decision": outcome.decision.value,
                "checkpoint_kind": checkpoint_kind,
                "decision_summary": outcome.decision_summary,
                "missing_capabilities": outcome.missing_capabilities,
            },
        )
        state.setdefault("evaluations", []).append(outcome)
        self._checkpoint(goal.task_id, state)
        return outcome, budget

    def _accept_replan_trigger(
        self,
        trigger: str | None,
        *,
        plan: ExecutionPlan,
        state: dict[str, Any],
    ) -> bool:
        if trigger == "new_evidence":
            evidence = {
                key: json_safe(state[key])
                for key in (
                    "quality.assess",
                    "history.query",
                    "trend.calculate",
                    "user_input_responses",
                )
                if key in state
            }
            if not evidence:
                return False
            fingerprint = stable_input_sha256(evidence)
            if fingerprint == state.get("_last_replan_evidence_sha256"):
                return False
            state["_last_replan_evidence_sha256"] = fingerprint
            return True
        if trigger == "conflict":
            return bool(self.store.list_a2a_messages(plan.task_id))
        if trigger in {"tool_failure", "agent_failure"}:
            expected_kind = (
                PlanStepKind.TOOL if trigger == "tool_failure" else PlanStepKind.AGENT
            )
            return any(
                record.status == "failed" and record.step.kind == expected_kind
                for record in self.store.list_dynamic_steps(plan.plan_id)
            )
        if trigger == "policy_rejection":
            safety = state.get(f"agent:{AgentKind.SAFETY_REVIEW.value}")
            return getattr(safety, "safety_decision", None) in {"revise", "block"}
        # Accepted goals are immutable inside a task, so changed_goal requires a
        # new task instead of an in-place scope mutation.
        return False

    def _invoke_model(
        self,
        *,
        task_id: str,
        purpose: str,
        agent: AgentKind,
        schema: type[SchemaT],
        messages: list[dict[str, str]],
        prompt_version: str,
        logical_key: str,
        budget: RuntimeBudget,
    ) -> tuple[SchemaT, RuntimeBudget, str]:
        if self.model is None or not self.model.is_configured:
            raise RuntimeError("structured model is not configured")
        budget.assert_time_available()
        idempotency_key = f"{task_id}:model:{logical_key}"
        prior_attempts = [
            item.attempt
            for item in self.store.list_model_invocations(task_id)
            if item.client_idempotency_key == idempotency_key
        ]
        attempt = max(prior_attempts, default=0) + 1
        budget = budget.consume(
            "schema_repair"
            if purpose == "schema_repair"
            else ("model_call" if attempt == 1 else "retry")
        )
        self.store.save_runtime_budget(budget)
        invocation = ModelInvocation(
            invocation_id=f"model-inv-{self._id_factory()}",
            task_id=task_id,
            purpose=purpose,
            agent=agent,
            model_provider=self.model.provider,
            model_id=self.model.model_id,
            prompt_version=prompt_version,
            context_packet_id=f"dynamic:{task_id}:{logical_key}",
            client_idempotency_key=idempotency_key,
            attempt=attempt,
            input_sha256=stable_input_sha256(messages),
            output_schema=schema.__name__,
            outcome=InvocationOutcome.PENDING,
        )
        # Attempt is authoritative before any network call.
        self.store.save_model_invocation(invocation)
        try:
            value = self.model.generate(
                messages=messages,
                schema=schema,
                prompt_version=prompt_version,
                context_packet_id=invocation.context_packet_id,
            )
        except BaseException as exc:
            outcome = (
                InvocationOutcome.FAILED
                if isinstance(exc, Exception)
                else InvocationOutcome.UNKNOWN_OUTCOME
            )
            finished_at = datetime.now(timezone.utc)
            self.store.save_model_invocation(
                invocation.model_copy(
                    update={
                        "outcome": outcome,
                        "error_code": exc.__class__.__name__,
                        "error_summary": exc.__class__.__name__,
                        "finished_at": finished_at,
                        "latency_ms": max(
                            0,
                            int(
                                (finished_at - invocation.started_at).total_seconds()
                                * 1000
                            ),
                        ),
                    }
                )
            )
            if (
                purpose != "schema_repair"
                and attempt < 2
                and isinstance(
                    exc,
                    (CloudLLMInvalidJSONError, CloudLLMSchemaError, ValidationError),
                )
            ):
                event_type = "plan.rejected" if purpose == "plan" else "model.output_rejected"
                self.service.emit_event(
                    task_id,
                    event_type=event_type,
                    message="Invalid structured output was rejected before a bounded repair call.",
                    payload={"attempt": attempt, "error_code": exc.__class__.__name__},
                )
                return self._invoke_model(
                    task_id=task_id,
                    purpose="schema_repair",
                    agent=agent,
                    schema=schema,
                    messages=messages,
                    prompt_version=prompt_version,
                    logical_key=logical_key,
                    budget=budget,
                )
            raise
        finished_at = datetime.now(timezone.utc)
        self.store.save_model_invocation(
            invocation.model_copy(
                update={
                    "outcome": InvocationOutcome.SUCCEEDED,
                    "provider_request_id": getattr(
                        self.model, "last_provider_request_id", None
                    ),
                    "input_token_count": getattr(
                        self.model, "last_input_token_count", None
                    ),
                    "output_token_count": getattr(
                        self.model, "last_output_token_count", None
                    ),
                    "finished_at": finished_at,
                    "latency_ms": max(
                        0,
                        int(
                            (finished_at - invocation.started_at).total_seconds()
                            * 1000
                        ),
                    ),
                }
            )
        )
        return value, budget, invocation.invocation_id

    def _finalize(
        self,
        *,
        task: RadarAgentTask,
        goal: UserGoal,
        mode: ExecutionMode,
        completion_status: CompletionStatus,
        state: dict[str, Any],
        failures: list[str],
    ) -> CompletionReceipt:
        artifact_refs: list[str] = []
        for name in ("role_artifact.render", "doctor_material.render"):
            value = state.get(name)
            if isinstance(value, dict) and value.get("artifact_version_id"):
                artifact_refs.append(value["artifact_version_id"])
        snapshot = state.get("evidence.ledger_commit", {}).get("fact_snapshot")
        snapshot_id = getattr(snapshot, "snapshot_id", None)
        receipt = CompletionReceipt(
            receipt_id=f"receipt-{self._id_factory()}",
            task_id=task.task_id,
            goal_id=goal.goal_id,
            execution_mode=mode,
            completion_status=completion_status,
            achieved_goal=completion_status == CompletionStatus.COMPLETE,
            accepted_claim_refs=[
                claim.claim_id
                for claim in getattr(
                    state.get("evidence.ledger_commit", {}).get("ledger"),
                    "claims",
                    [],
                )
            ],
            requested_artifact_refs=artifact_refs,
            missing_outputs=failures,
            unresolved_gaps=failures,
            actions_proposed=[
                action
                for product in state.get("agent_products", [])
                for action in product.candidate_actions
            ],
            actions_executed=[],
            trace_refs=[task.trace_id, plan_ref]
            if (plan_ref := self.service.get_task(task.task_id).current_plan_id)
            else [task.trace_id],
            caveats=(
                [DEGRADED_MATRIX[goal.goal_type].reason]
                if mode == ExecutionMode.SAFE_DEGRADED
                else []
            ),
            safe_next_step=self._safe_next_step(state, failures),
            fact_snapshot_id=snapshot_id,
        )
        self.store.save_completion_receipt(receipt)
        self._update_task(
            task.task_id,
            execution_mode=mode.value,
            completion_status=completion_status.value,
            pending_user_input_request_id=None,
        )
        confirmation = state.get("confirmation.request", {}).get("confirmation")
        if confirmation is not None and confirmation.status == "pending":
            budget = self.store.get_runtime_budget(task.task_id).pause_clock()
            self.store.save_runtime_budget(budget)
            if self.service.get_task(task.task_id).status == RadarTaskStatus.RUNNING:
                self.service.transition_task(
                    task.task_id,
                    RadarTaskStatus.WAITING_FOR_CONFIRMATION,
                    message="Requested output is ready; external export remains confirmation-gated.",
                )
        else:
            current = self.service.get_task(task.task_id)
            if current.status == RadarTaskStatus.RUNNING:
                self.service.transition_task(task.task_id, RadarTaskStatus.COMPLETED)
        if completion_status != CompletionStatus.COMPLETE:
            self.service.emit_event(
                task.task_id,
                event_type=f"task.{completion_status.value}",
                message="Task completed without claiming every requested output.",
                payload={
                    "completion_status": completion_status.value,
                    "unresolved_gap_count": len(failures),
                },
            )
        self.service.emit_event(
            task.task_id,
            event_type="task.receipt_created",
            message="Goal execution receipt created.",
            payload={
                "receipt_id": receipt.receipt_id,
                "execution_mode": mode.value,
                "completion_status": completion_status.value,
                "artifact_refs": artifact_refs,
            },
        )
        return receipt

    def _degraded_completion_status(
        self,
        goal: UserGoal,
        state: dict[str, Any],
        failures: list[str],
    ) -> CompletionStatus:
        if failures:
            return CompletionStatus.BLOCKED if not state.get("evidence.ledger_commit") else CompletionStatus.PARTIAL
        base = DEGRADED_MATRIX[goal.goal_type].completion_status
        summary = state.get("quality.assess", {}).get("night_summary")
        if summary is not None and not summary.health_conclusion_allowed:
            return CompletionStatus.PARTIAL
        return base

    @staticmethod
    def _safe_next_step(state: dict[str, Any], failures: list[str]) -> str | None:
        if state.get("_urgent_interrupted"):
            return "请优先处理当前明显不适；如症状严重或持续，请联系当地急救或专业医疗人员。"
        if any(item.startswith("budget_exhausted:") for item in failures):
            return "保留当前已完成证据，稍后从同一任务检查点继续，或缩小日期/目标范围。"
        if any(item.startswith("preflight:") for item in failures):
            return "请先检查设备连接、数据授权和所选日期，再重新启动分析。"
        if "user_input_declined" in failures:
            return "本次分析已按你的选择停止；需要时可重新启动任务，或在愿意补充可观察事实后再继续。"
        if failures:
            return "请查看未解决项并补充允许的证据；系统不会把不完整结果标记为完成。"
        return None

    def _load_or_create_budget(self, goal: UserGoal) -> RuntimeBudget:
        try:
            return self.store.get_runtime_budget(goal.task_id)
        except KeyError:
            value = RuntimeBudget.for_goal(goal.task_id, goal.goal_type)
            return self.store.save_runtime_budget(value)

    def _validate_goal_binding(self, task: RadarAgentTask, goal: UserGoal) -> None:
        """Fail before any model/tool call when immutable identity scope diverges."""

        device = self.store.get_device(task.radar_device_id)
        mismatches: list[str] = []
        if goal.task_id != task.task_id:
            mismatches.append("task_id")
        if goal.subject_id != task.subject_id:
            mismatches.append("subject_id")
        if goal.requesting_actor_id != (task.requested_by_user_id or "system"):
            mismatches.append("requesting_actor_id")
        if goal.requested_role != task.role:
            mismatches.append("requested_role")
        if device.bound_subject_id != task.subject_id:
            mismatches.append("device_subject_binding")
        if goal.resolved_timezone_name != device.timezone_name:
            mismatches.append("resolved_timezone_name")
        if mismatches:
            raise PermissionError(
                "dynamic goal binding validation failed: " + ",".join(mismatches)
            )
        if getattr(self.service, "_require_authorization", False):
            self.service.assert_task_access(
                task.task_id,
                actor_id=goal.requesting_actor_id,
                actor_role=goal.requested_role,
            )

    def _resume_confirmation_action(
        self, task: RadarAgentTask, goal: UserGoal
    ) -> CompletionReceipt | None:
        try:
            receipt = self.store.get_completion_receipt(task.task_id)
        except KeyError:
            return None
        confirmations = self.service.list_confirmations(task.task_id)
        if any(item.status == "pending" for item in confirmations):
            return receipt
        approved = next(
            (
                item
                for item in reversed(confirmations)
                if item.status == "approved"
                and item.execution_status != "completed"
            ),
            None,
        )
        if approved is None:
            self.service.transition_task(
                task.task_id,
                RadarTaskStatus.COMPLETED,
                message="Confirmation was resolved without authorizing an external action.",
            )
            return receipt

        plans = self.store.list_execution_plans(task.task_id)
        if not plans:
            raise ValueError("confirmed action has no persisted execution plan")
        state = self._restore_state(task.task_id)
        state["approved_confirmation_id"] = approved.confirmation_id
        budget = self._load_or_create_budget(goal).resume_clock()
        self.store.save_runtime_budget(budget)
        step = PlanStep(
            step_id="confirmed-action-execute",
            ordinal=1,
            kind=PlanStepKind.TOOL,
            capability=f"execute_confirmed_{approved.action_type}",
            tool_name="confirmed_action.execute",
            purpose="Execute only the exact approved external action.",
        )
        output, budget = self._invoke_tool(
            task=task,
            goal=goal,
            plan=plans[-1],
            step=step,
            state=state,
            budget=budget,
        )
        state["confirmed_action.execute"] = output
        self._checkpoint(task.task_id, state)
        artifact_ref = output["artifact_version_id"]
        updated = receipt.model_copy(
            update={
                "requested_artifact_refs": list(
                    dict.fromkeys([*receipt.requested_artifact_refs, artifact_ref])
                ),
                "actions_executed": list(
                    dict.fromkeys(
                        [*receipt.actions_executed, output["action_type"]]
                    )
                ),
            }
        )
        self.store.save_completion_receipt(updated)
        self.service.emit_event(
            task.task_id,
            event_type="confirmation.action_completed",
            message="The exact approved external action completed through the audited tool boundary.",
            payload={
                "confirmation_id": approved.confirmation_id,
                "action_type": output["action_type"],
                "artifact_version_id": artifact_ref,
            },
        )
        self.service.transition_task(
            task.task_id,
            RadarTaskStatus.COMPLETED,
            message="Approved external action completed and was reconciled.",
        )
        return updated

    @staticmethod
    def _safe_state_summary(state: dict[str, Any]) -> dict[str, Any]:
        products = state.get("agent_products", [])
        return {
            "available_evidence_refs": DynamicOrchestratorRuntime._available_evidence_refs(
                state
            ),
            "data_quality_status": getattr(
                state.get("quality.assess", {}).get("night_summary"),
                "data_quality_status",
                None,
            ),
            "risk_level": getattr(
                state.get("urgent_boundary.evaluate", {}).get("risk_decision"),
                "risk_level",
                None,
            ),
            "agent_results": [
                {
                    "summary": item.summary,
                    "confidence": item.confidence,
                    "uncertainty_count": len(item.uncertainties),
                    "safety_decision": item.safety_decision,
                }
                for item in products
            ],
            "a2a_outcomes": state.get("a2a_outcomes", []),
            "user_input_response_count": len(
                state.get("user_input_responses", [])
            ),
        }

    def _emit_degraded(self, task_id: str, reason_code: str) -> None:
        self.service.emit_event(
            task_id,
            event_type="execution.degraded",
            message="Intelligent execution is unavailable; using explicit safe-degraded behavior.",
            payload={"execution_mode": "safe_degraded", "reason_code": reason_code},
        )

    @staticmethod
    def _invalidate_downstream_for_replan(state: dict[str, Any]) -> None:
        downstream_tools = {
            "evidence.ledger_validate",
            "evidence.ledger_commit",
            "role_artifact.render",
            "doctor_material.render",
            "confirmation.request",
        }
        for key in downstream_tools:
            state.pop(key, None)
        completed = set(state.get("_completed_capabilities", []))
        completed -= {
            "validate_claim_evidence_integrity",
            "commit_fact_snapshot",
            "render_requested_role_artifact",
            "render_requested_doctor_material",
            "request_export_confirmation",
            "review_safety_and_confirmation",
        }
        state["_completed_capabilities"] = sorted(completed)

    def _update_task(self, task_id: str, **updates: Any) -> RadarAgentTask:
        task = self.service.get_task(task_id)
        updated = task.model_copy(
            update={
                **updates,
                "task_version": task.task_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.store.save_task_if_version(updated, expected_version=task.task_version)
        return updated

    def _checkpoint(self, task_id: str, state: dict[str, Any]) -> None:
        previous = self.store.latest_runtime_checkpoint(task_id)
        sequence = 1 if previous is None else previous.sequence + 1
        checkpoint = RuntimeCheckpoint(
            checkpoint_id=f"checkpoint-{self._id_factory()}",
            task_id=task_id,
            sequence=sequence,
            state=json_safe(state),
        )
        self.store.save_runtime_checkpoint(checkpoint)

    def _restore_state(self, task_id: str) -> dict[str, Any]:
        checkpoint = self.store.latest_runtime_checkpoint(task_id)
        if checkpoint is None:
            return {}
        return _rehydrate_state(checkpoint.state)

    def _resume_user_input(self, task: RadarAgentTask, state: dict[str, Any]) -> dict[str, Any]:
        request_id = task.pending_user_input_request_id or state.get(
            "pending_user_input_request_id"
        )
        if not request_id:
            raise ValueError("waiting task has no persisted user input request")
        request = self.store.get_user_input_request(request_id)
        if request.status == "declined":
            state["_user_input_declined"] = True
            state.pop("pending_user_input_request_id", None)
            self._update_task(task.task_id, pending_user_input_request_id=None)
            self.service.emit_event(
                task.task_id,
                event_type="user_input.declined",
                message="User declined the reviewed input; the task will stop safely.",
                payload={
                    "request_id": request_id,
                    "question_id": request.question_id,
                },
            )
            self._checkpoint(task.task_id, state)
            return state
        response = self.store.get_user_input_response(request_id)
        if response is None:
            raise ValueError("user input has not been answered")
        if request.status != "answered":
            raise ValueError("user input request has not reached answered state")
        if request.target_role != response.answered_by_role:
            raise ValueError("answering role does not match the reviewed request")
        if request.expires_at is not None and response.created_at > request.expires_at:
            raise ValueError("user input request has expired")
        if request.answer_options and response.answer not in request.answer_options:
            raise ValueError("answer is not one of the reviewed options")
        state.setdefault("user_input_responses", []).append(response)
        state["_requires_replan_after_user_input"] = True
        state.pop("pending_user_input_request_id", None)
        self._update_task(task.task_id, pending_user_input_request_id=None)
        self.service.emit_event(
            task.task_id,
            event_type="user_input.received",
            message="Reviewed user input was recorded as self-reported context.",
            payload={
                "request_id": request_id,
                "question_id": request.question_id,
                "answer_source": "self_report",
            },
        )
        self._checkpoint(task.task_id, state)
        return state

    @staticmethod
    def _agent_evidence(state: dict[str, Any]) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        summary = state.get("quality.assess", {}).get("night_summary")
        if summary is not None:
            evidence["quality"] = _safe_night_summary(summary)
        history = state.get("history.query", {})
        if history.get("summaries") is not None:
            evidence["history"] = {
                "summaries": [
                    _safe_night_summary(item) for item in history["summaries"]
                ]
            }
        source_ledger = history.get("source_ledger")
        if source_ledger is not None:
            evidence["explicit_source"] = {
                "source_date": history.get("source_date"),
                "ledger": _safe_ledger(source_ledger),
            }
        trend = state.get("trend.calculate", {}).get("trend_result")
        if trend is not None:
            evidence["trend"] = {
                "claims": [claim.model_dump(mode="json") for claim in trend.claims],
                "evidence_refs": trend.evidence_refs,
                "latest_night": trend.latest_night,
                "windows": trend.windows,
                "uncertainties": trend.uncertainties,
                "caveats": trend.caveats,
            }
        risk = state.get("urgent_boundary.evaluate", {}).get("risk_decision")
        if risk is not None:
            evidence["risk_boundary"] = {
                "risk_level": risk.risk_level.value,
                "claims": [claim.model_dump(mode="json") for claim in risk.claims],
                "evidence_refs": risk.evidence_refs,
            }
        rag_context = state.get("rag.retrieve_reviewed", {}).get("rag_context")
        if rag_context is not None:
            evidence["reviewed_rag"] = {
                "citation_ids": rag_context.get("citation_ids", []),
                "snippets": rag_context.get("snippets", []),
                "caveats": rag_context.get("caveats", []),
                "source_metadata": rag_context.get("source_metadata", []),
            }
        ledger = state.get("evidence.ledger_validate", {}).get("ledger")
        if ledger is not None:
            evidence["validated_ledger"] = _safe_ledger(ledger)
        responses = state.get("user_input_responses", [])
        if responses:
            evidence["self_reports"] = [
                {
                    "request_id": item.request_id,
                    "answer": item.answer,
                    "answered_by_role": item.answered_by_role,
                    "created_at": item.created_at,
                }
                for item in responses
            ]
        products = state.get("agent_products", [])
        if products:
            evidence["prior_agent_products"] = [
                {
                    "summary": item.summary,
                    "findings": item.findings,
                    "evidence_refs": item.evidence_refs,
                    "confidence": item.confidence,
                    "uncertainties": item.uncertainties,
                    "candidate_actions": item.candidate_actions,
                    "safety_decision": item.safety_decision,
                }
                for item in products
            ]
        return json_safe(evidence)

    @staticmethod
    def _available_evidence_refs(state: dict[str, Any]) -> list[str]:
        refs: list[str] = []
        summary = state.get("quality.assess", {}).get("night_summary")
        if summary is not None:
            refs.extend(summary.source_raw_event_ids)
            if summary.source_report_ref:
                refs.append(summary.source_report_ref)
            refs.append(f"night-summary:{summary.radar_device_id}:{summary.night_of.isoformat()}")
        trend = state.get("trend.calculate", {}).get("trend_result")
        if trend is not None:
            refs.extend(trend.evidence_refs)
        ledger = state.get("evidence.ledger_validate", {}).get("ledger")
        if ledger is not None:
            refs.extend(ledger.canonical_evidence_refs)
        rag_context = state.get("rag.retrieve_reviewed", {}).get("rag_context")
        if rag_context is not None:
            refs.extend(rag_context.get("citation_ids", []))
        return list(dict.fromkeys(refs))

    @staticmethod
    def _safety_review_needed(state: dict[str, Any]) -> bool:
        risk = state.get("urgent_boundary.evaluate", {}).get("risk_decision")
        risk_level = getattr(risk, "risk_level", RiskLevel.INFO)
        if risk_level in {
            RiskLevel.WATCH,
            RiskLevel.ESCALATE,
            RiskLevel.URGENT_BOUNDARY,
            RiskLevel.UNCERTAIN,
        }:
            return True
        for product in state.get("agent_products", []):
            if product.confidence < 0.6 or product.uncertainties or product.candidate_actions:
                return True
        return bool(state.get("conflicts"))


def _rehydrate_state(raw: dict[str, Any]) -> dict[str, Any]:
    state = dict(raw)
    if isinstance(state.get("radar.read"), dict):
        value = state["radar.read"]
        value["device"] = RadarDevice.model_validate(value["device"])
        value["snapshots"] = [RadarVitalSnapshot.model_validate(item) for item in value.get("snapshots", [])]
        if value.get("provider_night_summary") is not None:
            value["provider_night_summary"] = RadarNightSummary.model_validate(value["provider_night_summary"])
    if isinstance(state.get("quality.assess"), dict) and state["quality.assess"].get("night_summary"):
        state["quality.assess"]["night_summary"] = RadarNightSummary.model_validate(
            state["quality.assess"]["night_summary"]
        )
    if isinstance(state.get("history.query"), dict):
        value = state["history.query"]
        if "summaries" in value:
            value["summaries"] = [RadarNightSummary.model_validate(item) for item in value["summaries"]]
        if "artifacts" in value:
            value["artifacts"] = [RadarArtifactVersion.model_validate(item) for item in value["artifacts"]]
        if value.get("source_ledger") is not None:
            value["source_ledger"] = EvidenceLedger.model_validate(value["source_ledger"])
    if isinstance(state.get("trend.calculate"), dict) and state["trend.calculate"].get("trend_result"):
        state["trend.calculate"]["trend_result"] = RadarTrendResult.model_validate(
            state["trend.calculate"]["trend_result"]
        )
    if isinstance(state.get("urgent_boundary.evaluate"), dict) and state["urgent_boundary.evaluate"].get("risk_decision"):
        state["urgent_boundary.evaluate"]["risk_decision"] = RiskSignalDecision.model_validate(
            state["urgent_boundary.evaluate"]["risk_decision"]
        )
    for name in ("evidence.ledger_validate", "evidence.ledger_commit"):
        if isinstance(state.get(name), dict) and state[name].get("ledger"):
            state[name]["ledger"] = EvidenceLedger.model_validate(state[name]["ledger"])
    for name in ("role_artifact.render", "doctor_material.render"):
        if isinstance(state.get(name), dict) and state[name].get("artifact"):
            state[name]["artifact"] = RoleReportArtifact.model_validate(state[name]["artifact"])
    products: list[AgentWorkProduct] = []
    for item in state.get("agent_products", []):
        schema = AgentWorkProduct
        if isinstance(item, dict):
            if "evidence_selection_reason_codes" in item:
                schema = AGENT_OUTPUT_SCHEMAS[AgentKind.EVIDENCE_ANALYSIS]
            elif "role_fit_notes" in item:
                schema = AGENT_OUTPUT_SCHEMAS[AgentKind.CARE_PLANNING]
            elif "policy_reason_codes" in item:
                schema = AGENT_OUTPUT_SCHEMAS[AgentKind.SAFETY_REVIEW]
            elif "answer_evidence_refs" in item:
                schema = AGENT_OUTPUT_SCHEMAS[AgentKind.DIALOGUE]
        products.append(schema.model_validate(item))
    state["agent_products"] = products
    return state


def _safe_goal_context(goal: UserGoal) -> dict[str, Any]:
    """Minimum planner/Agent goal projection; identity stays inside policy code."""

    return {
        "goal_type": goal.goal_type.value,
        "target_date": goal.target_date,
        "range_start": goal.range_start,
        "range_end": goal.range_end,
        "question": goal.question,
        "source_date": goal.source_date,
        "focus": goal.focus,
        "requested_role": goal.requested_role,
        "requested_outputs": goal.requested_outputs,
        "allowed_action_scope": goal.allowed_action_scope,
    }


def _safe_night_summary(summary: RadarNightSummary) -> dict[str, Any]:
    return {
        "night_of": summary.night_of,
        "total_sleep_minutes": summary.total_sleep_minutes,
        "sleep_score": summary.sleep_score,
        "out_of_bed_count": summary.out_of_bed_count,
        "movement_count": summary.movement_count,
        "data_coverage_ratio": summary.data_coverage_ratio,
        "data_quality_status": summary.data_quality_status.value,
        "health_conclusion_allowed": summary.health_conclusion_allowed,
        "explainable_metrics": summary.explainable_metrics,
        "caveats": summary.caveats,
        "blocked_reasons": summary.blocked_reasons,
        "evidence_refs": [
            *summary.source_raw_event_ids,
            *([summary.source_report_ref] if summary.source_report_ref else []),
        ],
    }


def _safe_ledger(ledger: EvidenceLedger) -> dict[str, Any]:
    return {
        "ledger_id": ledger.ledger_id,
        "canonical_evidence_refs": ledger.canonical_evidence_refs,
        "derived_metrics": ledger.derived_metrics,
        "claims": [claim.model_dump(mode="json") for claim in ledger.claims],
        "questionnaire_entries": [
            {
                "question_id": entry.question_id,
                "answer": entry.answer,
                "role": entry.role,
                "collected_at": entry.collected_at,
                "evidence_ref": entry.evidence_ref,
            }
            for entry in ledger.questionnaire_entries
        ],
        "caveats": ledger.caveats,
        "uncertainties": ledger.uncertainties,
        "confidence": ledger.confidence,
        "review_status": ledger.review_status.value,
    }


def _a2a_changed_fields(
    source: AgentWorkProduct, target: AgentWorkProduct
) -> list[str]:
    changed: list[str] = []
    comparisons = {
        "claims": source.findings != target.findings,
        "confidence": source.confidence != target.confidence,
        "caveats": source.uncertainties != target.uncertainties,
        "actions": source.candidate_actions != target.candidate_actions,
        "safety_decision": source.safety_decision != target.safety_decision,
    }
    for field, differs in comparisons.items():
        if differs:
            changed.append(field)
    return changed


__all__ = ["DynamicOrchestratorRuntime"]
