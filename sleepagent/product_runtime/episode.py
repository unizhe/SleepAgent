from __future__ import annotations

from datetime import datetime, timezone

from pydantic import Field

from sleepagent.product_runtime.agents.sleepcare import (
    EpisodePlanProposal,
    EvaluationDecision,
    SleepCareAgent,
    SleepCareControlInvocationPort,
    SleepCareEvaluation,
    SleepCareEvaluationContext,
    SleepCareEvaluationInput,
    SleepCareEvaluationOutput,
    SleepCarePlanContext,
    SleepCarePlanInput,
    SleepCarePlanOutput,
)

from sleepagent.product_runtime.contracts import (
    AgentId,
    EpisodePlan,
    EpisodeReceipt,
    EpisodeStatus,
    EpisodeType,
    ExecutionMode,
    FactSnapshot,
    StrictContract,
    WorkProductKind,
)
from sleepagent.product_runtime.governance import AcceptedWorkProduct
from sleepagent.product_runtime.invocation import (
    AgentInvocationRecord,
)
from sleepagent.product_runtime.policies.workflow import (
    CANONICAL_WORKFLOW_POLICY,
    CanonicalWorkflowPolicy,
)
from sleepagent.product_runtime.registry import (
    EPISODE_DEFINITIONS,
    validate_episode_plan,
)
from sleepagent.product_runtime.skills import (
    SkillRegistry,
)


EPISODE_RUNTIME_VERSION = "sleepagent-product-episode-runtime.v17"


class EpisodeRuntimeError(RuntimeError):
    pass


class EpisodeBudgetExceeded(EpisodeRuntimeError):
    pass


class EpisodeDeadlineExceeded(EpisodeRuntimeError):
    pass


class EpisodeStateConflict(EpisodeRuntimeError):
    pass


class ResumeRequiresReplan(EpisodeRuntimeError):
    pass


class PlanningFailed(EpisodeRuntimeError):
    pass


class RuntimeCounters(StrictContract):
    agent_calls: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    replans: int = Field(default=0, ge=0)
    safety_revisions: int = Field(default=0, ge=0)


class EpisodeRuntimeSnapshot(StrictContract):
    episode_id: str
    snapshot_version: int = Field(..., ge=1)
    fact_snapshot: FactSnapshot
    plan: EpisodePlan | None = None
    episode_state_revision: int = Field(default=0, ge=0)
    counters: RuntimeCounters = Field(default_factory=RuntimeCounters)
    invocation_records: list[AgentInvocationRecord] = Field(default_factory=list)
    accepted_work_products: dict[str, AcceptedWorkProduct] = Field(
        default_factory=dict
    )
    safety_rounds_by_target: dict[str, int] = Field(default_factory=dict)
    status: EpisodeStatus | None = None
    execution_mode: ExecutionMode = ExecutionMode.INTELLIGENT
    failure_codes: list[str] = Field(default_factory=list)
    started_at: datetime
    active_elapsed_seconds: float = Field(default=0, ge=0)


class ProductEpisodeRuntime:
    """Runtime-owned lifecycle for the four-Agent topology."""

    workflow_policy: CanonicalWorkflowPolicy = CANONICAL_WORKFLOW_POLICY

    def __init__(
        self,
        *,
        episode_id: str,
        fact_snapshot: FactSnapshot,
        sleepcare_agent: SleepCareAgent,
        sleepcare_control_invoker: SleepCareControlInvocationPort | None = None,
        started_at: datetime | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        if type(sleepcare_agent) is not SleepCareAgent:
            raise TypeError("ProductEpisodeRuntime requires SleepCareAgent")
        resolved_registry = skill_registry or sleepcare_agent.skill_registry
        if (
            skill_registry is not None
            and sleepcare_agent.skill_registry.snapshot()
            != skill_registry.snapshot()
        ):
            raise ValueError("SleepCareAgent and Runtime Skill registries differ")
        if sleepcare_control_invoker is not None:
            sleepcare_agent.bind_control_invoker(sleepcare_control_invoker)
        if sleepcare_agent.control_invoker is None:
            raise TypeError(
                "ProductEpisodeRuntime requires a coordinator-bound SleepCareAgent"
            )
        self.episode_id = episode_id
        self.fact_snapshot = fact_snapshot
        self.sleepcare_agent = sleepcare_agent
        self.started_at = started_at or datetime.now(timezone.utc)
        self.plan: EpisodePlan | None = None
        self.episode_state_revision = 0
        self.counters = RuntimeCounters()
        self.invocation_records: list[AgentInvocationRecord] = []
        self.accepted_work_products: dict[WorkProductKind, AcceptedWorkProduct] = {}
        self.safety_rounds_by_target: dict[str, int] = {}
        self.status: EpisodeStatus | None = None
        self.execution_mode = ExecutionMode.INTELLIGENT
        self.failure_codes: list[str] = []
        self._paused_at: datetime | None = None
        self._paused_seconds = 0.0
        self.skill_registry = resolved_registry

    def create_plan(
        self,
        *,
        episode_type,
        objective: str,
        required_work_products: set[WorkProductKind] | None = None,
        required_safety_checkpoints: set[str] | None = None,
    ) -> EpisodePlan:
        required_work_products = required_work_products or set()
        required_safety_checkpoints = required_safety_checkpoints or set()
        definition = EPISODE_DEFINITIONS[episode_type]
        self._check_deadline(definition.budget.soft_deadline_seconds)
        context = SleepCarePlanContext(
            episode_id=self.episode_id,
            episode_type=episode_type,
            objective=objective,
            fact_snapshot_id=self.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=self.fact_snapshot.fact_snapshot_hash,
            source_scope=self.fact_snapshot.source_scope,
            registry_required_work_products=tuple(
                sorted(
                    definition.required_work_products,
                    key=lambda item: item.value,
                )
            ),
            request_required_work_products=tuple(
                sorted(required_work_products, key=lambda item: item.value)
            ),
            allowed_work_products=tuple(
                sorted(
                    definition.allowed_work_products,
                    key=lambda item: item.value,
                )
            ),
            required_tools=tuple(sorted(definition.required_tools)),
            allowed_agents=tuple(
                sorted(definition.allowed_agents, key=lambda item: item.value)
            ),
            available_safety_checkpoints=tuple(
                sorted(definition.conditional_safety_checkpoints)
            ),
            request_required_safety_checkpoints=tuple(
                sorted(required_safety_checkpoints)
            ),
            exit_conditions=tuple(sorted(definition.exit_conditions)),
            budget=definition.budget,
        )
        role_output = self._plan_with_one_repair(
            episode_type=episode_type,
            context=context,
        )
        proposal = role_output.proposal
        plan = EpisodePlan(
            plan_id=f"plan:{self.episode_id}:{self.counters.replans + 1}",
            episode_id=self.episode_id,
            episode_type=episode_type,
            objective=proposal.objective,
            required_work_products=proposal.required_work_products,
            conditional_work_products=proposal.conditional_work_products,
            allowed_agents=sorted(
                definition.allowed_agents, key=lambda item: item.value
            ),
            allowed_tools=sorted(definition.allowed_tools),
            safety_checkpoints=proposal.safety_checkpoints,
            exit_conditions=proposal.exit_conditions,
            expected_agent_calls=proposal.expected_agent_calls,
            expected_tool_calls=proposal.expected_tool_calls,
        )
        validate_episode_plan(
            plan,
            required_work_products=required_work_products,
            required_safety_checkpoints=required_safety_checkpoints,
        )
        self.plan = plan
        self.episode_state_revision += 1
        self._record_sleepcare_result(role_output.record)
        return plan

    def replan(self, *, reason: str) -> EpisodePlan:
        if self.plan is None:
            raise PlanningFailed("cannot replan before initial plan")
        definition = EPISODE_DEFINITIONS[self.plan.episode_type]
        if self.counters.replans >= definition.budget.replan_limit:
            raise EpisodeBudgetExceeded("SleepCare replan limit exhausted")
        self.counters = self.counters.model_copy(
            update={"replans": self.counters.replans + 1}
        )
        required = set(self.plan.required_work_products)
        checkpoints = set(self.plan.safety_checkpoints)
        return self.create_plan(
            episode_type=self.plan.episode_type,
            objective=f"{self.plan.objective}; replan_reason={reason}",
            required_work_products=required,
            required_safety_checkpoints=checkpoints,
        )

    def evaluate(
        self,
        *,
        latest_kind: WorkProductKind,
        latest: AcceptedWorkProduct | None,
        failure_code: str | None = None,
    ) -> SleepCareEvaluation:
        if self.plan is None:
            raise PlanningFailed("cannot evaluate before plan")
        definition = EPISODE_DEFINITIONS[self.plan.episode_type]
        self._check_deadline(definition.budget.soft_deadline_seconds)
        context = SleepCareEvaluationContext(
            episode_id=self.episode_id,
            episode_state_revision=self.episode_state_revision,
            latest_kind=latest_kind,
            latest_ref=latest.work_product_ref if latest else None,
            failure_code=failure_code,
            accepted_work_products=tuple(
                sorted(self.accepted_work_products, key=lambda item: item.value)
            ),
            required_work_products=tuple(self.plan.required_work_products),
            remaining_agent_calls=(
                definition.budget.agent_call_limit - self.counters.agent_calls
            ),
            remaining_replans=(
                definition.budget.replan_limit - self.counters.replans
            ),
        )
        role_output = self._evaluate_with_one_repair(
            episode_type=self.plan.episode_type,
            context=context,
        )
        result = role_output.evaluation
        self._record_sleepcare_result(role_output.record)
        if result.decision == EvaluationDecision.FINISH:
            missing = set(self.plan.required_work_products) - set(
                self.accepted_work_products
            )
            missing.discard(WorkProductKind.SLEEPCARE_PLAN)
            if missing:
                raise EpisodeStateConflict(
                    "SleepCare cannot finish while required work is missing"
                )
        if result.decision == EvaluationDecision.REPLAN and not result.replan_reason:
            raise EpisodeStateConflict("replan decision requires reason")
        return result

    def accept(
        self, kind: WorkProductKind, work_product: AcceptedWorkProduct
    ) -> None:
        if self.status is not None:
            raise EpisodeStateConflict("terminal/waiting Episode cannot accept work")
        if work_product.fact_snapshot_hash != self.fact_snapshot.fact_snapshot_hash:
            raise EpisodeStateConflict("stale accepted work product")
        self.accepted_work_products[kind] = work_product
        self.episode_state_revision += 1

    def record_agent_invocation(self, record: AgentInvocationRecord) -> None:
        definition = EPISODE_DEFINITIONS[self.plan.episode_type] if self.plan else None
        if definition is None:
            raise PlanningFailed("Agent invocation requires a validated plan")
        if self.counters.agent_calls >= definition.budget.agent_call_limit:
            raise EpisodeBudgetExceeded("Agent-call limit exhausted")
        if self.counters.model_calls >= definition.budget.total_model_call_limit:
            raise EpisodeBudgetExceeded("model-call limit exhausted")
        if record.episode_id != self.episode_id:
            raise EpisodeStateConflict("cross-Episode invocation record")
        if any(item.invocation_id == record.invocation_id for item in self.invocation_records):
            raise EpisodeStateConflict("duplicate invocation id")
        self.invocation_records.append(record)
        self.counters = self.counters.model_copy(
            update={
                "agent_calls": self.counters.agent_calls + 1,
                "model_calls": self.counters.model_calls + 1,
            }
        )

    def record_tool_call(self) -> None:
        if self.plan is None:
            raise PlanningFailed("tool call requires plan")
        budget = EPISODE_DEFINITIONS[self.plan.episode_type].budget
        if self.counters.tool_calls >= budget.tool_call_limit:
            raise EpisodeBudgetExceeded("Tool-call limit exhausted")
        self.counters = self.counters.model_copy(
            update={"tool_calls": self.counters.tool_calls + 1}
        )

    def record_safety_revision(self, target_hash: str) -> None:
        if self.plan is None:
            raise PlanningFailed("Safety revision requires plan")
        budget = EPISODE_DEFINITIONS[self.plan.episode_type].budget
        rounds = self.safety_rounds_by_target.get(target_hash, 0) + 1
        if rounds > budget.safety_revision_limit:
            raise EpisodeBudgetExceeded("Safety revision limit exhausted")
        self.safety_rounds_by_target[target_hash] = rounds

    def checkpoint(
        self,
        status: EpisodeStatus,
        *,
        failure_code: str | None = None,
    ) -> None:
        if status not in {
            EpisodeStatus.WAITING_USER,
            EpisodeStatus.WAITING_CONFIRMATION,
        }:
            raise EpisodeStateConflict("checkpoint status must be waiting_*")
        self.status = status
        self._paused_at = datetime.now(timezone.utc)
        if failure_code:
            self.failure_codes.append(failure_code)

    def resume(self, *, snapshot: FactSnapshot) -> None:
        if self.status not in {
            EpisodeStatus.WAITING_USER,
            EpisodeStatus.WAITING_CONFIRMATION,
        }:
            raise ResumeRequiresReplan("Episode is not waiting")
        if snapshot.binding != self.fact_snapshot.binding:
            raise ResumeRequiresReplan("authenticated binding changed")
        if snapshot.fact_snapshot_hash != self.fact_snapshot.fact_snapshot_hash:
            raise ResumeRequiresReplan("FactSnapshot changed")
        if self._paused_at:
            self._paused_seconds += (
                datetime.now(timezone.utc) - self._paused_at
            ).total_seconds()
        self._paused_at = None
        self.status = None

    def finish(
        self,
        *,
        status: EpisodeStatus,
        execution_mode: ExecutionMode | None = None,
        failure_codes: list[str] | None = None,
        tool_receipt_ids: list[str] | None = None,
    ) -> EpisodeReceipt:
        if status in {
            EpisodeStatus.WAITING_USER,
            EpisodeStatus.WAITING_CONFIRMATION,
        }:
            self.checkpoint(status)
            terminal = False
        else:
            self.status = status
            terminal = True
        mode = execution_mode or self.execution_mode
        codes = [*self.failure_codes, *(failure_codes or [])]
        return EpisodeReceipt(
            episode_id=self.episode_id,
            episode_type=(
                self.plan.episode_type
                if self.plan
                else EpisodeType.URGENT_BOUNDARY
            ),
            receipt_revision=self.episode_state_revision + 1,
            terminal=terminal,
            execution_mode=mode,
            status=status,
            goal_achieved=status == EpisodeStatus.COMPLETE,
            fact_snapshot_id=self.fact_snapshot.fact_snapshot_id,
            fact_snapshot_hash=self.fact_snapshot.fact_snapshot_hash,
            source_scope=self.fact_snapshot.source_scope,
            final_episode_state_revision=self.episode_state_revision,
            agent_invocation_ids=[
                item.invocation_id for item in self.invocation_records
            ],
            tool_receipt_ids=list(dict.fromkeys(tool_receipt_ids or [])),
            accepted_work_product_refs=[
                item.work_product_ref
                for item in self.accepted_work_products.values()
            ],
            safety_decision_refs=[
                item.work_product_ref
                for kind, item in self.accepted_work_products.items()
                if kind == WorkProductKind.SAFETY_DECISION
            ],
            failure_codes=codes,
            trace_ref=f"trace:{self.episode_id}:{self.episode_state_revision}",
        )

    def snapshot(self) -> EpisodeRuntimeSnapshot:
        return EpisodeRuntimeSnapshot(
            episode_id=self.episode_id,
            snapshot_version=1,
            fact_snapshot=self.fact_snapshot,
            plan=self.plan,
            episode_state_revision=self.episode_state_revision,
            counters=self.counters,
            invocation_records=self.invocation_records,
            accepted_work_products={
                key.value: value for key, value in self.accepted_work_products.items()
            },
            safety_rounds_by_target=self.safety_rounds_by_target,
            status=self.status,
            execution_mode=self.execution_mode,
            failure_codes=self.failure_codes,
            started_at=self.started_at,
            active_elapsed_seconds=self._active_elapsed(),
        )

    @classmethod
    def restore(
        cls,
        snapshot: EpisodeRuntimeSnapshot,
        *,
        sleepcare_agent: SleepCareAgent,
        sleepcare_control_invoker: SleepCareControlInvocationPort | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> "ProductEpisodeRuntime":
        runtime = cls(
            episode_id=snapshot.episode_id,
            fact_snapshot=snapshot.fact_snapshot,
            sleepcare_agent=sleepcare_agent,
            sleepcare_control_invoker=sleepcare_control_invoker,
            started_at=snapshot.started_at,
            skill_registry=skill_registry,
        )
        runtime.plan = snapshot.plan
        runtime.episode_state_revision = snapshot.episode_state_revision
        runtime.counters = snapshot.counters
        runtime.invocation_records = list(snapshot.invocation_records)
        runtime.accepted_work_products = {
            WorkProductKind(key): value
            for key, value in snapshot.accepted_work_products.items()
        }
        runtime.safety_rounds_by_target = dict(snapshot.safety_rounds_by_target)
        runtime.status = snapshot.status
        runtime.execution_mode = snapshot.execution_mode
        runtime.failure_codes = list(snapshot.failure_codes)
        runtime._paused_seconds = max(
            0,
            (datetime.now(timezone.utc) - snapshot.started_at).total_seconds()
            - snapshot.active_elapsed_seconds,
        )
        if runtime.status in {
            EpisodeStatus.WAITING_USER,
            EpisodeStatus.WAITING_CONFIRMATION,
        }:
            runtime._paused_at = datetime.now(timezone.utc)
        runtime._validate_restored_state()
        return runtime

    def _validate_restored_state(self) -> None:
        if self.plan:
            validate_episode_plan(self.plan)
        if self.counters.agent_calls != len(self.invocation_records):
            raise EpisodeStateConflict("serialized Agent counters do not match records")
        if any(
            item.fact_snapshot_hash != self.fact_snapshot.fact_snapshot_hash
            for item in self.accepted_work_products.values()
        ):
            raise EpisodeStateConflict("serialized work product is stale")
        if len({item.invocation_id for item in self.invocation_records}) != len(
            self.invocation_records
        ):
            raise EpisodeStateConflict("serialized invocation ids are not unique")

    def _plan_with_one_repair(
        self,
        *,
        episode_type: EpisodeType,
        context: SleepCarePlanContext,
    ) -> SleepCarePlanOutput:
        last_error: Exception | None = None
        for attempt in range(2):
            self._consume_sleepcare_model_call()
            try:
                return self.sleepcare_agent.plan(
                    SleepCarePlanInput(
                        episode_id=self.episode_id,
                        episode_type=episode_type,
                        fact_snapshot=self.fact_snapshot,
                        episode_state_revision=self.episode_state_revision,
                        runtime_context=context,
                        invocation_ordinal=len(self.invocation_records) + 1,
                        repair_attempt=attempt,
                        previous_error_type=(
                            type(last_error).__name__ if last_error else None
                        ),
                    )
                )
            except Exception as exc:
                last_error = exc
        raise PlanningFailed(
            "SleepCare sleepcare.plan.v1 failed after one repair"
        ) from last_error

    def _evaluate_with_one_repair(
        self,
        *,
        episode_type: EpisodeType,
        context: SleepCareEvaluationContext,
    ) -> SleepCareEvaluationOutput:
        last_error: Exception | None = None
        for attempt in range(2):
            self._consume_sleepcare_model_call()
            try:
                return self.sleepcare_agent.evaluate(
                    SleepCareEvaluationInput(
                        episode_id=self.episode_id,
                        episode_type=episode_type,
                        fact_snapshot=self.fact_snapshot,
                        episode_state_revision=self.episode_state_revision,
                        runtime_context=context,
                        invocation_ordinal=len(self.invocation_records) + 1,
                        repair_attempt=attempt,
                        previous_error_type=(
                            type(last_error).__name__ if last_error else None
                        ),
                    )
                )
            except Exception as exc:
                last_error = exc
        raise PlanningFailed(
            "SleepCare sleepcare.evaluate.v1 failed after one repair"
        ) from last_error

    def _consume_sleepcare_model_call(self) -> None:
        if self.plan:
            budget = EPISODE_DEFINITIONS[self.plan.episode_type].budget
            if self.counters.model_calls >= budget.total_model_call_limit:
                raise EpisodeBudgetExceeded("model-call limit exhausted")
        self.counters = self.counters.model_copy(
            update={"model_calls": self.counters.model_calls + 1}
        )

    def _record_sleepcare_result(self, record: AgentInvocationRecord) -> None:
        if record.episode_id != self.episode_id:
            raise EpisodeStateConflict("cross-Episode SleepCare result")
        if record.agent_id is not AgentId.SLEEP_CARE:
            raise EpisodeStateConflict("SleepCare port returned another Agent identity")
        if record.skill_id not in {"plan_episode", "evaluate_work_product"}:
            raise EpisodeStateConflict("SleepCare port returned an invalid control Skill")
        if any(
            item.invocation_id == record.invocation_id
            for item in self.invocation_records
        ):
            raise EpisodeStateConflict("duplicate SleepCare invocation id")
        self.invocation_records.append(record)
        self.counters = self.counters.model_copy(
            update={"agent_calls": self.counters.agent_calls + 1}
        )

    def _active_elapsed(self) -> float:
        end = self._paused_at or datetime.now(timezone.utc)
        return max(
            0,
            (end - self.started_at).total_seconds() - self._paused_seconds,
        )

    def _check_deadline(self, limit: int) -> None:
        if self._active_elapsed() > limit:
            raise EpisodeDeadlineExceeded("Episode active-time deadline exceeded")


class InMemoryEpisodeCheckpointStore:
    def __init__(self) -> None:
        self._items: dict[str, list[EpisodeRuntimeSnapshot]] = {}

    def append(self, snapshot: EpisodeRuntimeSnapshot) -> None:
        revisions = self._items.setdefault(snapshot.episode_id, [])
        if revisions and snapshot.snapshot_version <= revisions[-1].snapshot_version:
            snapshot = snapshot.model_copy(
                update={"snapshot_version": revisions[-1].snapshot_version + 1}
            )
        revisions.append(snapshot.model_copy(deep=True))

    def latest(self, episode_id: str) -> EpisodeRuntimeSnapshot:
        try:
            return self._items[episode_id][-1].model_copy(deep=True)
        except (KeyError, IndexError) as exc:
            raise KeyError(f"unknown Episode checkpoint: {episode_id}") from exc


__all__ = [
    "EPISODE_RUNTIME_VERSION",
    "EpisodeBudgetExceeded",
    "EpisodeDeadlineExceeded",
    "EpisodePlanProposal",
    "EpisodeRuntimeError",
    "EpisodeRuntimeSnapshot",
    "EpisodeStateConflict",
    "EvaluationDecision",
    "InMemoryEpisodeCheckpointStore",
    "PlanningFailed",
    "ProductEpisodeRuntime",
    "ResumeRequiresReplan",
    "RuntimeCounters",
    "SleepCareEvaluation",
]
