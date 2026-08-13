from __future__ import annotations

# 这里是 Product Agent 的唯一装配点：只构造 canonical 四 Agent、只读工具和结果发布链。
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from sleepagent.runtime.agents import ProductAgentFactory, ProductAgentRoster
from sleepagent.runtime.agent_invocation_coordinator import (
    AgentInvocationCoordinator,
    ProviderInputBudgetLedger,
)
from sleepagent.runtime.contracts import AgentId, CommunicationDraft
from sleepagent.runtime.episode_result_finalizer import EpisodeResultFinalizer
from sleepagent.runtime.governance import (
    CareActionCatalog,
    InMemoryCareContextStore,
    InMemoryMemoryContextStore,
)
from sleepagent.runtime.invocation import StructuredAgentModel
from sleepagent.runtime.memory import (
    CanonicalSourceResolver,
    InMemoryLongitudinalResultStore,
    LongitudinalMemoryService,
)
from sleepagent.runtime.provider import (
    OpenAICompatibleStructuredAgentModel,
    openai_compatible_provider_config_from_env,
)
from sleepagent.runtime.publication_service import PublicationService
from sleepagent.runtime.runner import ProductEpisodeRunner
from sleepagent.runtime.contracts import (
    FactSnapshotRevalidator,
    PublicationPublisher,
)
from sleepagent.runtime.tooling import ProductRuntimeReadService
from sleepagent.runtime.registry import (
    AgentProfile,
    PromptCompiler,
    SkillRegistry,
    SkillResolver,
    default_agent_profiles,
    default_skill_packages,
)
from sleepagent.runtime.tool_execution_coordinator import ToolExecutionCoordinator
from sleepagent.runtime.tooling import CoreProductToolService, ProductToolExecutor


@dataclass(frozen=True, slots=True)
class LocalPublicationPublisher:
    """进程内 replay 发布边界；durable Product 发布由 worker UoW 持有。"""

    def publish(self, draft: CommunicationDraft) -> bool:
        del draft
        return True


@dataclass(frozen=True, slots=True)
class ProductRuntimeStores:
    care_context: InMemoryCareContextStore
    memory_context: InMemoryMemoryContextStore
    episode_results: InMemoryLongitudinalResultStore


@dataclass(frozen=True, slots=True)
class ProductRuntimePolicies:
    care_catalog: CareActionCatalog
    fact_snapshot_revalidator: FactSnapshotRevalidator | None
    source_resolvers: Mapping[str, CanonicalSourceResolver]


@dataclass(frozen=True, slots=True)
class ProductRuntimeBundle:
    """canonical worker 和测试共同使用的最小、可核对对象图。"""

    runner: ProductEpisodeRunner
    roster: ProductAgentRoster
    tool_executor: ProductToolExecutor
    skill_registry: SkillRegistry
    skill_resolver: SkillResolver
    prompt_compiler: PromptCompiler
    agent_profiles: Mapping[AgentId, AgentProfile]
    agent_invocation_coordinator: AgentInvocationCoordinator
    tool_execution_coordinator: ToolExecutionCoordinator
    publication_service: PublicationService
    episode_result_finalizer: EpisodeResultFinalizer
    longitudinal_memory: LongitudinalMemoryService
    stores: ProductRuntimeStores
    publisher: PublicationPublisher
    policies: ProductRuntimePolicies
    provider_input_ledger: ProviderInputBudgetLedger
    core_tool_service: CoreProductToolService
    runtime_read_service: ProductRuntimeReadService

    def __post_init__(self) -> None:
        if type(self.roster) is not ProductAgentRoster:
            raise TypeError("Product runtime requires the exact four-Agent roster")
        if set(self.roster.as_mapping()) != set(AgentId):
            raise ValueError("Product runtime roster does not match AgentId")
        if self.runner.agent_roster is not self.roster:
            raise ValueError("Product runtime Runner roster binding mismatch")
        if self.runner.tool_executor is not self.tool_executor:
            raise ValueError("Product runtime Tool binding mismatch")
        if self.runner.result_store is not self.stores.episode_results:
            raise ValueError("Product runtime result authority mismatch")


def build_product_runtime_bundle(
    *,
    sleepcare_model: StructuredAgentModel | None = None,
    evidence_reasoning_model: StructuredAgentModel | None = None,
    care_strategy_model: StructuredAgentModel | None = None,
    safety_review_model: StructuredAgentModel | None = None,
    sleepcare_planning_model: StructuredAgentModel | None = None,
    agent_roster: ProductAgentRoster | None = None,
    skill_registry: SkillRegistry | None = None,
    source_resolvers: Mapping[str, CanonicalSourceResolver] | None = None,
    publisher: PublicationPublisher | None = None,
    fact_snapshot_revalidator: FactSnapshotRevalidator | None = None,
    care_catalog: CareActionCatalog | None = None,
) -> ProductRuntimeBundle:
    """装配 canonical MORNING_REVIEW 运行链，不创建第二持久化 authority。"""

    registry = skill_registry or _registry_from_roster(agent_roster)
    roster = _build_roster(
        agent_roster=agent_roster,
        skill_registry=registry,
        sleepcare_model=sleepcare_model,
        evidence_reasoning_model=evidence_reasoning_model,
        care_strategy_model=care_strategy_model,
        safety_review_model=safety_review_model,
        sleepcare_planning_model=sleepcare_planning_model,
    )
    stores = ProductRuntimeStores(
        care_context=InMemoryCareContextStore(),
        memory_context=InMemoryMemoryContextStore(),
        episode_results=InMemoryLongitudinalResultStore(),
    )
    catalog = care_catalog or CareActionCatalog()
    core_tool_service = CoreProductToolService()
    executor = ProductToolExecutor(core_service=core_tool_service)
    runtime_read_service = ProductRuntimeReadService(
        care_store=stores.care_context,
        care_catalog=catalog,
    )
    _register_read_handlers(executor, runtime_read_service)

    memory = LongitudinalMemoryService(
        memory_store=stores.memory_context,
        repository=stores.episode_results,
        source_resolvers=dict(source_resolvers or {}),
    )
    resolver = SkillResolver(registry)
    compiler = PromptCompiler()
    profiles: Mapping[AgentId, AgentProfile] = MappingProxyType(
        default_agent_profiles()
    )
    provider_ledger = ProviderInputBudgetLedger()
    tool_coordinator = ToolExecutionCoordinator(executor)
    invocation_coordinator = AgentInvocationCoordinator(
        tool_execution_coordinator=tool_coordinator,
        longitudinal_memory=memory,
        skill_resolver=resolver,
        prompt_compiler=compiler,
        agent_profiles=profiles,
        provider_input_budget=provider_ledger,
    )
    roster.sleepcare.bind_control_invoker(invocation_coordinator)
    resolved_publisher = publisher or LocalPublicationPublisher()
    publication = PublicationService(
        publisher=resolved_publisher,
        result_store=stores.episode_results,
        longitudinal_memory=memory,
        revalidator=fact_snapshot_revalidator,
    )
    finalizer = EpisodeResultFinalizer(
        result_store=stores.episode_results,
        tool_execution_coordinator=tool_coordinator,
        provider_input_budget_ledger=provider_ledger,
    )
    runner = ProductEpisodeRunner(
        agent_roster=roster,
        care_catalog=catalog,
        care_store=stores.care_context,
        skill_registry=registry,
        agent_invocation_coordinator=invocation_coordinator,
        tool_execution_coordinator=tool_coordinator,
        publication_service=publication,
        episode_result_finalizer=finalizer,
    )
    policies = ProductRuntimePolicies(
        care_catalog=catalog,
        fact_snapshot_revalidator=fact_snapshot_revalidator,
        source_resolvers=MappingProxyType(dict(source_resolvers or {})),
    )
    return ProductRuntimeBundle(
        runner=runner,
        roster=roster,
        tool_executor=executor,
        skill_registry=registry,
        skill_resolver=resolver,
        prompt_compiler=compiler,
        agent_profiles=profiles,
        agent_invocation_coordinator=invocation_coordinator,
        tool_execution_coordinator=tool_coordinator,
        publication_service=publication,
        episode_result_finalizer=finalizer,
        longitudinal_memory=memory,
        stores=stores,
        publisher=resolved_publisher,
        policies=policies,
        provider_input_ledger=provider_ledger,
        core_tool_service=core_tool_service,
        runtime_read_service=runtime_read_service,
    )


def build_product_runtime_bundle_from_env() -> ProductRuntimeBundle:
    return _build_openai_compatible_product_runtime_bundle()


def _build_postgres_worker_product_runtime_bundle_from_env() -> ProductRuntimeBundle:
    """名称强调 durable authority 在 PostgreSQL worker，而不在 Agent runtime。"""

    return _build_openai_compatible_product_runtime_bundle()


def _build_openai_compatible_product_runtime_bundle() -> ProductRuntimeBundle:
    provider_config = openai_compatible_provider_config_from_env()
    model = lambda: OpenAICompatibleStructuredAgentModel(config=provider_config)
    return build_product_runtime_bundle(
        sleepcare_model=model(),
        evidence_reasoning_model=model(),
        care_strategy_model=model(),
        safety_review_model=model(),
    )


def build_deterministic_product_runtime_bundle(
    *, model: StructuredAgentModel
) -> ProductRuntimeBundle:
    return build_product_runtime_bundle(
        sleepcare_model=model,
        evidence_reasoning_model=model,
        care_strategy_model=model,
        safety_review_model=model,
        sleepcare_planning_model=model,
    )


def build_product_episode_runner_from_env() -> ProductEpisodeRunner:
    return build_product_runtime_bundle_from_env().runner


def product_episode_runner_is_configured(runner: ProductEpisodeRunner) -> bool:
    roster = getattr(runner, "agent_roster", None)
    if type(roster) is not ProductAgentRoster:
        return False
    models = [roster.sleepcare.planning_model, *(item.model for item in roster)]
    return all(
        model is not None and bool(getattr(model, "is_configured", True))
        for model in models
    )


def _registry_from_roster(roster: ProductAgentRoster | None) -> SkillRegistry:
    if roster is None:
        return SkillRegistry(default_skill_packages())
    registries = [agent.skill_registry for agent in roster]
    registry = registries[0]
    if any(item is not registry for item in registries[1:]):
        raise ValueError("injected Agent roster must share one Skill registry")
    return registry


def _build_roster(
    *,
    agent_roster: ProductAgentRoster | None,
    skill_registry: SkillRegistry,
    sleepcare_model: StructuredAgentModel | None,
    evidence_reasoning_model: StructuredAgentModel | None,
    care_strategy_model: StructuredAgentModel | None,
    safety_review_model: StructuredAgentModel | None,
    sleepcare_planning_model: StructuredAgentModel | None,
) -> ProductAgentRoster:
    models = (
        sleepcare_model,
        evidence_reasoning_model,
        care_strategy_model,
        safety_review_model,
    )
    if agent_roster is not None:
        if any(item is not None for item in (*models, sleepcare_planning_model)):
            raise ValueError("inject an Agent roster or model bindings, not both")
        if any(
            agent.skill_registry is not skill_registry for agent in agent_roster
        ):
            raise ValueError("injected roster and Skill registry differ")
        return agent_roster
    if any(item is None for item in models):
        raise TypeError("Product runtime requires one model for each Agent role")
    assert sleepcare_model is not None
    assert evidence_reasoning_model is not None
    assert care_strategy_model is not None
    assert safety_review_model is not None
    return ProductAgentFactory.create(
        sleepcare_model=sleepcare_model,
        evidence_reasoning_model=evidence_reasoning_model,
        care_strategy_model=care_strategy_model,
        safety_review_model=safety_review_model,
        sleepcare_planning_model=sleepcare_planning_model,
        skill_registry=skill_registry,
    )


def _register_read_handlers(
    executor: ProductToolExecutor,
    runtime_read_service: ProductRuntimeReadService,
) -> None:
    executor.register_handler(
        "care.read_state", runtime_read_service.read_current_care_state
    )
    executor.register_handler(
        "care.read_catalog", runtime_read_service.read_care_catalog
    )
    executor.register_handler(
        "care.read_constraints", runtime_read_service.read_care_constraints
    )
    executor.register_handler(
        "device.read_delivery_policy", runtime_read_service.read_delivery_policy
    )
    executor.register_handler(
        "coordination.read_policy", runtime_read_service.read_coordination_policy
    )
    executor.register_handler("policy.read", runtime_read_service.read_runtime_policy)


__all__ = [
    "LocalPublicationPublisher",
    "ProductRuntimeBundle",
    "ProductRuntimePolicies",
    "ProductRuntimeStores",
    "build_deterministic_product_runtime_bundle",
    "build_product_episode_runner_from_env",
    "build_product_runtime_bundle",
    "build_product_runtime_bundle_from_env",
    "product_episode_runner_is_configured",
]
