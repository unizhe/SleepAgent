from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import Field

from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    ContextPacket,
    EpisodePlan,
    EpisodeReceipt,
    EpisodeStatus,
    EpisodeType,
    ExecutionMode,
    FactSnapshot,
    StrictContract,
    TrustLabel,
    TrustedContextItem,
    WorkProductKind,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.governance import AcceptedWorkProduct
from sleepagent.radar_agent.product_agent.invocation import (
    AgentInvocationRecord,
    StructuredAgentModel,
)
from sleepagent.radar_agent.product_agent.registry import (
    EPISODE_DEFINITIONS,
    validate_episode_plan,
)
from sleepagent.radar_agent.product_agent.skills import (
    PromptCompiler,
    SkillRegistry,
    SkillResolver,
    default_agent_profiles,
    default_skill_packages,
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

    def __init__(
        self,
        *,
        episode_id: str,
        fact_snapshot: FactSnapshot,
        sleepcare_model: StructuredAgentModel,
        started_at: datetime | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self.episode_id = episode_id
        self.fact_snapshot = fact_snapshot
        self.sleepcare_model = sleepcare_model
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
        self.skill_registry = skill_registry or SkillRegistry(default_skill_packages())
        self.skill_resolver = SkillResolver(self.skill_registry)
        self.prompt_compiler = PromptCompiler()
        self.sleepcare_profile = default_agent_profiles()[AgentId.SLEEP_CARE]
        self._last_skill_meta: dict[str, str] = {}

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
        context = {
            "episode_id": self.episode_id,
            "episode_type": episode_type.value,
            "objective": objective,
            "fact_snapshot_id": self.fact_snapshot.fact_snapshot_id,
            "fact_snapshot_hash": self.fact_snapshot.fact_snapshot_hash,
            "source_scope": self.fact_snapshot.source_scope.model_dump(mode="json"),
            "registry_required_work_products": sorted(
                item.value for item in definition.required_work_products
            ),
            "request_required_work_products": sorted(
                item.value for item in required_work_products
            ),
            "allowed_work_products": sorted(
                item.value for item in definition.allowed_work_products
            ),
            "required_tools": sorted(definition.required_tools),
            "allowed_agents": sorted(item.value for item in definition.allowed_agents),
            "available_safety_checkpoints": sorted(
                definition.conditional_safety_checkpoints
            ),
            "request_required_safety_checkpoints": sorted(
                required_safety_checkpoints
            ),
            "exit_conditions": sorted(definition.exit_conditions),
            "budget": definition.budget.model_dump(mode="json"),
        }
        proposal = self._generate_with_one_repair(
            schema=EpisodePlanProposal,
            prompt_version="sleepcare.plan.v1",
            context=context,
            skill_id="plan_episode",
            episode_type=episode_type,
        )
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
        self._record_sleepcare_invocation(
            kind="plan",
            prompt_version="sleepcare.plan.v1",
            context=context,
            summary=f"planned {episode_type.value}",
            output=proposal,
        )
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
        context = {
            "episode_id": self.episode_id,
            "episode_state_revision": self.episode_state_revision,
            "latest_kind": latest_kind.value,
            "latest_ref": latest.work_product_ref if latest else None,
            "failure_code": failure_code,
            "accepted_work_products": sorted(
                item.value for item in self.accepted_work_products
            ),
            "required_work_products": [
                item.value for item in self.plan.required_work_products
            ],
            "remaining_agent_calls": (
                definition.budget.agent_call_limit - self.counters.agent_calls
            ),
            "remaining_replans": (
                definition.budget.replan_limit - self.counters.replans
            ),
        }
        result = self._generate_with_one_repair(
            schema=SleepCareEvaluation,
            prompt_version="sleepcare.evaluate.v1",
            context=context,
            skill_id="evaluate_work_product",
            episode_type=self.plan.episode_type,
        )
        self._record_sleepcare_invocation(
            kind="evaluate",
            prompt_version="sleepcare.evaluate.v1",
            context=context,
            summary=result.summary,
            output=result,
        )
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
        sleepcare_model: StructuredAgentModel,
        skill_registry: SkillRegistry | None = None,
    ) -> "ProductEpisodeRuntime":
        runtime = cls(
            episode_id=snapshot.episode_id,
            fact_snapshot=snapshot.fact_snapshot,
            sleepcare_model=sleepcare_model,
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

    def _generate_with_one_repair(
        self,
        *,
        schema,
        prompt_version: str,
        context,
        skill_id: str,
        episode_type,
    ):
        last_error: Exception | None = None
        for attempt in range(2):
            self._consume_sleepcare_model_call()
            try:
                invocation_id = (
                    f"sleepcare:{skill_id}:{self.episode_id}:"
                    f"{len(self.invocation_records) + 1}:{attempt}"
                )
                packet = ContextPacket(
                    context_packet_id=f"context:{invocation_id}",
                    episode_id=self.episode_id,
                    invocation_id=invocation_id,
                    agent_id=AgentId.SLEEP_CARE,
                    objective=str(context.get("objective", skill_id)),
                    fact_snapshot_id=self.fact_snapshot.fact_snapshot_id,
                    fact_snapshot_hash=self.fact_snapshot.fact_snapshot_hash,
                    episode_state_revision=self.episode_state_revision,
                    care_context_version=self.fact_snapshot.care_context_version,
                    source_scope=self.fact_snapshot.source_scope,
                    authorization_scope=(
                        self.fact_snapshot.binding.authorization_scope
                    ),
                    items=(
                        TrustedContextItem(
                            key="runtime_episode_context",
                            trust_label=TrustLabel.SYSTEM_POLICY,
                            value={
                                **context,
                                "repair_attempt": attempt,
                                "previous_error": (
                                    type(last_error).__name__
                                    if last_error
                                    else None
                                ),
                            },
                        ),
                    ),
                )
                bundle, lock = self.skill_resolver.resolve(
                    episode_id=self.episode_id,
                    episode_type=episode_type,
                    agent_id=AgentId.SLEEP_CARE,
                    mandatory_skill_ids=[skill_id],
                    subject_id=self.fact_snapshot.binding.subject_id,
                )
                package = bundle.packages[0]
                compiled = self.prompt_compiler.compile(
                    global_policy=(
                        "Runtime owns Episode state, completion and budgets.",
                        "SleepCare may propose plans but cannot skip required work.",
                    ),
                    profile=self.sleepcare_profile,
                    bundle=bundle,
                    context=packet,
                )
                self._last_skill_meta = {
                    "skill_id": skill_id,
                    "skill_version": package.version,
                    "skill_package_hash": package.package_hash,
                    "skill_lock_hash": lock.lock_hash,
                    "profile_version": self.sleepcare_profile.version,
                    "profile_hash": self.sleepcare_profile.profile_hash,
                    "prompt_bundle_hash": compiled.receipt.prompt_bundle_hash,
                    "context_packet_id": packet.context_packet_id,
                    "context_hash": stable_hash(packet),
                }
                return self.sleepcare_model.generate(
                    messages=list(compiled.messages),
                    schema=schema,
                    prompt_version=f"{prompt_version}:{package.version}",
                    context_packet_id=packet.context_packet_id,
                )
            except Exception as exc:
                last_error = exc
        raise PlanningFailed(
            f"SleepCare {prompt_version} failed after one repair"
        ) from last_error

    def _consume_sleepcare_model_call(self) -> None:
        if self.plan:
            budget = EPISODE_DEFINITIONS[self.plan.episode_type].budget
            if self.counters.model_calls >= budget.total_model_call_limit:
                raise EpisodeBudgetExceeded("model-call limit exhausted")
        self.counters = self.counters.model_copy(
            update={"model_calls": self.counters.model_calls + 1}
        )

    def _record_sleepcare_invocation(
        self,
        *,
        kind: str,
        prompt_version: str,
        context: dict[str, Any],
        summary: str,
        output: StrictContract,
    ) -> None:
        now = datetime.now(timezone.utc)
        invocation_id = (
            f"sleepcare:{kind}:{self.episode_id}:{len(self.invocation_records) + 1}"
        )
        self.invocation_records.append(
            AgentInvocationRecord(
                invocation_id=invocation_id,
                episode_id=self.episode_id,
                agent_id=AgentId.SLEEP_CARE,
                agent_version="sleepcare.v1",
                profile_version=self._last_skill_meta["profile_version"],
                profile_hash=self._last_skill_meta["profile_hash"],
                skill_id=self._last_skill_meta["skill_id"],
                skill_version=self._last_skill_meta["skill_version"],
                skill_package_hash=self._last_skill_meta["skill_package_hash"],
                skill_lock_hash=self._last_skill_meta["skill_lock_hash"],
                prompt_bundle_hash=self._last_skill_meta["prompt_bundle_hash"],
                schema_version=(
                    "EpisodePlanProposal.v1"
                    if kind == "plan"
                    else "SleepCareEvaluation.v1"
                ),
                prompt_version=prompt_version,
                policy_version="product-safety.v3",
                context_packet_id=self._last_skill_meta["context_packet_id"],
                context_hash=self._last_skill_meta["context_hash"],
                target_hash=stable_hash(
                    {
                        "episode_id": self.episode_id,
                        "kind": kind,
                        "context": context,
                        "output": output.model_dump(mode="json"),
                        "skill_lock_hash": self._last_skill_meta["skill_lock_hash"],
                    }
                ),
                provider=self.sleepcare_model.provider,
                model_id=self.sleepcare_model.model_id,
                provider_request_id=getattr(
                    self.sleepcare_model, "last_provider_request_id", None
                ),
                started_at=now,
                ended_at=now,
                latency_ms=0,
                validation_status="runtime_validated",
                safe_summary=summary[:500],
            )
        )
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
