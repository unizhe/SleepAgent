from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Mapping

from sleepagent.persistence import (
    RadarPersistenceStore,
    connect_postgres_store,
)
from sleepagent.product_runtime.agents import (
    ProductAgentFactory,
    ProductAgentRoster,
)
from sleepagent.product_runtime.agent_invocation_coordinator import (
    AgentInvocationCoordinator,
    ProviderInputBudgetLedger,
)
from sleepagent.product_runtime.confirmed_action_coordinator import (
    ConfirmedActionCoordinator,
)
from sleepagent.product_runtime.contracts import (
    AgentId,
    CommunicationDraft,
    ToolEffect,
)
from sleepagent.product_runtime.external_actions import (
    ConfiguredExternalActionExecutor,
    UnconfiguredExternalActionExecutor,
)
from sleepagent.product_runtime.episode_result_finalizer import (
    EpisodeResultFinalizer,
)
from sleepagent.product_runtime.governance import (
    CareActionCatalog,
    CommitJournal,
    DeterministicCommitController,
    InMemoryCareContextStore,
    InMemoryCommitJournal,
    InMemoryMemoryContextStore,
    build_cold_start_receipt,
)
from sleepagent.product_runtime.habit_application import (
    HabitPendingChangeSetStore,
    HabitProfileApplicationService,
    InMemoryHabitPendingChangeSetStore,
    PersistentHabitPendingChangeSetStore,
)
from sleepagent.product_runtime.habit_persistence import (
    PersistentHabitProfileStore,
    PersistentHabitQuestionnaireStateStore,
)
from sleepagent.product_runtime.habit_profile import (
    HabitProfileStore,
    InMemoryHabitProfileStore,
    InMemoryObjectiveBaselineStore,
    ObjectiveBaselineStore,
)
from sleepagent.product_runtime.habit_runtime import (
    HabitProfileRuntimeService,
)
from sleepagent.product_runtime.hitl import (
    DecisionAuthorityValidator,
    HumanDecisionError,
    HumanDecisionPolicy,
    HumanDecisionRepository,
    HumanDecisionRequest,
    HumanDecisionService,
    InMemoryHumanDecisionRepository,
    PersistentHumanDecisionRepository,
)
from sleepagent.product_runtime.invocation import (
    StructuredAgentModel,
)
from sleepagent.product_runtime.longitudinal_memory import (
    CanonicalSourceResolver,
    DeterministicInductionWorker,
    InMemoryLongitudinalResultStore,
    LongitudinalMemoryService,
)
from sleepagent.product_runtime.product_persistence import (
    PersistentCareContextStore,
    PersistentCommitJournal,
    PersistentMemoryContextStore,
    PersistentProductEpisodeResultStore,
)
from sleepagent.product_runtime.publication_service import (
    PublicationService,
)
from sleepagent.product_runtime.provider import (
    OpenAICompatibleStructuredAgentModel,
)
from sleepagent.product_device.llm import (
    openai_compatible_provider_config_from_env,
)
from sleepagent.product_runtime.registry import (
    COMMIT_CONTROLLER_TOOLS,
    RUNTIME_INTERACTION_TOOLS,
    TOOL_DEFINITIONS,
)
from sleepagent.product_runtime.runner import ProductEpisodeRunner
from sleepagent.product_runtime.runtime_ports import (
    ExternalActionExecutor,
    FactSnapshotRevalidator,
    ProductEpisodeResultStore,
    PublicationPublisher,
)
from sleepagent.product_runtime.skills import (
    AgentProfile,
    PromptCompiler,
    SkillRegistry,
    SkillResolver,
    default_agent_profiles,
    default_skill_packages,
)
from sleepagent.product_runtime.services.runtime_capabilities import (
    ProductRuntimeReadService,
)
from sleepagent.product_runtime.tooling import (
    CoreProductToolService,
    ProductToolExecutor,
)
from sleepagent.product_runtime.tool_execution_coordinator import (
    ToolExecutionCoordinator,
)
from sleepagent.product_runtime.questionnaire import (
    HabitQuestionnaireService,
    InMemoryHabitQuestionnaireStateStore,
)
from sleepagent.product_runtime.questionnaire.service import (
    HabitQuestionnaireStateStore,
)


PRODUCT_EPISODE_API_ADAPTER_VERSION = "sleepagent-product-api-adapter.v3"
RADAR_AGENT_DATABASE_URL_ENV = "SLEEPAGENT_RADAR_AGENT_DATABASE_URL"
RADAR_AGENT_SQLITE_PATH_ENV = "SLEEPAGENT_RADAR_AGENT_SQLITE_PATH"
DEFAULT_RADAR_AGENT_SQLITE_PATH = "/tmp/sleepagent_product_runtime.sqlite3"
DEPLOYMENT_MODE_ENV = "SLEEPAGENT_DEPLOYMENT_MODE"
RADAR_AGENT_DEV_MODE_ENV = "SLEEPAGENT_RADAR_AGENT_DEV_MODE"


class LocalPublicationPublisher:
    """Explicit local publication boundary preserving the former no-op sink."""

    def publish(self, draft: CommunicationDraft) -> bool:
        del draft
        return True


@dataclass(frozen=True, slots=True)
class ProductRuntimeStores:
    """Concrete authorities shared by every facade in one Product runtime."""

    care_context: InMemoryCareContextStore
    memory_context: InMemoryMemoryContextStore
    habit_profile: HabitProfileStore
    habit_questionnaire_state: HabitQuestionnaireStateStore
    habit_pending_change_sets: HabitPendingChangeSetStore
    objective_baseline: ObjectiveBaselineStore
    commit_journal: CommitJournal
    episode_results: ProductEpisodeResultStore
    human_decisions: HumanDecisionRepository


@dataclass(frozen=True, slots=True)
class ProductRuntimePolicies:
    care_catalog: CareActionCatalog
    human_decision_policy: HumanDecisionPolicy
    decision_authority_validator: DecisionAuthorityValidator | None
    fact_snapshot_revalidator: FactSnapshotRevalidator | None
    source_resolvers: Mapping[str, CanonicalSourceResolver]


@dataclass(frozen=True, slots=True)
class ProductRuntimeBundle:
    """The complete, identity-checked Product Agent runtime object graph."""

    runner: ProductEpisodeRunner
    roster: ProductAgentRoster
    tool_executor: ProductToolExecutor
    skill_registry: SkillRegistry
    skill_resolver: SkillResolver
    prompt_compiler: PromptCompiler
    agent_profiles: Mapping[AgentId, AgentProfile]
    agent_invocation_coordinator: AgentInvocationCoordinator
    tool_execution_coordinator: ToolExecutionCoordinator
    confirmed_action_coordinator: ConfirmedActionCoordinator
    publication_service: PublicationService
    episode_result_finalizer: EpisodeResultFinalizer
    human_decisions: HumanDecisionService
    longitudinal_memory: LongitudinalMemoryService
    habit_runtime: HabitProfileRuntimeService
    habit_application: HabitProfileApplicationService
    stores: ProductRuntimeStores
    commit_controller: DeterministicCommitController
    induction_worker: DeterministicInductionWorker
    external_executor: ExternalActionExecutor
    publisher: PublicationPublisher
    policies: ProductRuntimePolicies
    provider_input_ledger: ProviderInputBudgetLedger
    persistence_store: RadarPersistenceStore | None
    core_tool_service: CoreProductToolService
    runtime_read_service: ProductRuntimeReadService

    def __post_init__(self) -> None:
        if type(self.roster) is not ProductAgentRoster:
            raise TypeError("Product runtime requires the exact four-Agent roster")
        if set(self.roster.as_mapping()) != set(AgentId):
            raise ValueError("Product runtime roster does not match AgentId")
        if any(
            agent.skill_registry is not self.skill_registry
            for agent in self.roster
        ):
            raise ValueError("Product runtime Agents must share one Skill registry")
        if self.skill_resolver.registry is not self.skill_registry:
            raise ValueError("Product runtime Skill resolver registry mismatch")
        if set(self.agent_profiles) != set(AgentId):
            raise ValueError("Product runtime requires one profile per Agent")
        if (
            self.roster.sleepcare.control_invoker
            is not self.agent_invocation_coordinator
        ):
            raise ValueError("SleepCare control coordinator binding mismatch")

        runner_bindings = (
            (self.runner.agent_roster, self.roster, "roster"),
            (self.runner.tool_executor, self.tool_executor, "Tool executor"),
            (self.runner.publisher, self.publisher, "publisher"),
            (self.runner.result_store, self.stores.episode_results, "result store"),
            (self.runner.care_catalog, self.policies.care_catalog, "Care catalog"),
            (self.runner.habit_runtime, self.habit_runtime, "Habit runtime"),
            (self.runner.skill_registry, self.skill_registry, "Skill registry"),
            (self.runner.skill_resolver, self.skill_resolver, "Skill resolver"),
            (self.runner.prompt_compiler, self.prompt_compiler, "prompt compiler"),
            (
                self.runner.commit_controller,
                self.commit_controller,
                "Commit Controller",
            ),
            (self.runner.human_decisions, self.human_decisions, "HDS"),
            (
                self.runner.longitudinal_memory,
                self.longitudinal_memory,
                "Memory service",
            ),
            (self.runner.induction_worker, self.induction_worker, "induction worker"),
            (
                self.runner.external_executor,
                self.external_executor,
                "external executor",
            ),
            (
                self.runner.agent_invocation_coordinator,
                self.agent_invocation_coordinator,
                "Agent invocation coordinator",
            ),
            (
                self.runner.tool_execution_coordinator,
                self.tool_execution_coordinator,
                "Tool execution coordinator",
            ),
            (
                self.runner.confirmed_action_coordinator,
                self.confirmed_action_coordinator,
                "confirmed-action coordinator",
            ),
            (
                self.runner.publication_service,
                self.publication_service,
                "publication service",
            ),
            (
                self.runner.episode_result_finalizer,
                self.episode_result_finalizer,
                "Episode result finalizer",
            ),
            (
                self.runner.provider_input_budget,
                self.provider_input_ledger,
                "provider ledger",
            ),
        )
        for actual, expected, label in runner_bindings:
            if actual is not expected:
                raise ValueError(f"Product Runner {label} binding mismatch")
        if self.runner.agent_profiles is not self.agent_profiles:
            raise ValueError("Product Runner Agent profile binding mismatch")
        if (
            self.agent_invocation_coordinator.tool_execution_coordinator
            is not self.tool_execution_coordinator
        ):
            raise ValueError("Agent invocation Tool coordinator mismatch")
        if (
            self.confirmed_action_coordinator.commit_controller
            is not self.commit_controller
            or self.confirmed_action_coordinator.human_decisions
            is not self.human_decisions
            or self.confirmed_action_coordinator.external_executor
            is not self.external_executor
        ):
            raise ValueError("confirmed-action authority graph mismatch")
        if (
            self.publication_service.publisher is not self.publisher
            or self.publication_service.result_store
            is not self.stores.episode_results
            or self.publication_service.longitudinal_memory
            is not self.longitudinal_memory
            or self.publication_service.revalidator
            is not self.policies.fact_snapshot_revalidator
        ):
            raise ValueError("publication service authority graph mismatch")
        if (
            self.episode_result_finalizer.result_store
            is not self.stores.episode_results
            or self.episode_result_finalizer.tool_execution_coordinator
            is not self.tool_execution_coordinator
            or self.episode_result_finalizer.provider_input_budget_ledger
            is not self.provider_input_ledger
        ):
            raise ValueError("Episode finalizer authority graph mismatch")
        if (
            self.runner.fact_snapshot_revalidator
            is not self.policies.fact_snapshot_revalidator
        ):
            raise ValueError("Product Runner FactSnapshot policy mismatch")

        if self.commit_controller.care_store is not self.stores.care_context:
            raise ValueError("Care store authority is not shared")
        if self.commit_controller.memory_store is not self.stores.memory_context:
            raise ValueError("Memory store authority is not shared")
        if self.commit_controller.habit_profile_store is not self.stores.habit_profile:
            raise ValueError("Habit Profile authority is not shared")
        if self.commit_controller.commit_journal is not self.stores.commit_journal:
            raise ValueError("Commit journal authority is not shared")
        if self.longitudinal_memory.memory_store is not self.stores.memory_context:
            raise ValueError("Memory service store binding mismatch")
        if self.longitudinal_memory.repository is not self.stores.episode_results:
            raise ValueError("Memory service result authority mismatch")
        if (
            self.longitudinal_memory.source_resolvers
            != dict(self.policies.source_resolvers)
        ):
            raise ValueError("Memory source-resolver policy mismatch")
        if self.habit_runtime.store is not self.stores.habit_profile:
            raise ValueError("Habit runtime Profile store binding mismatch")
        if (
            self.habit_runtime.questionnaire.state_store
            is not self.stores.habit_questionnaire_state
        ):
            raise ValueError("Habit questionnaire state authority is not shared")
        if self.habit_runtime.baseline_store is not self.stores.objective_baseline:
            raise ValueError("Habit baseline authority is not shared")
        if self.habit_application.runtime is not self.habit_runtime:
            raise ValueError("Habit application runtime binding mismatch")
        if self.habit_application.human_decisions is not self.human_decisions:
            raise ValueError("Habit application HDS binding mismatch")
        if self.habit_application.commit_controller is not self.commit_controller:
            raise ValueError("Habit application Commit Controller mismatch")
        if (
            self.habit_application.pending_store
            is not self.stores.habit_pending_change_sets
        ):
            raise ValueError("Habit pending-change authority is not shared")
        if self.human_decisions._repository is not self.stores.human_decisions:
            raise ValueError("HDS repository authority is not shared")
        if self.human_decisions.policy is not self.policies.human_decision_policy:
            raise ValueError("HDS policy binding mismatch")
        if (
            self.human_decisions.authority_validator
            is not self.policies.decision_authority_validator
        ):
            raise ValueError("HDS authority-validator binding mismatch")

        induction_repository = getattr(
            self.induction_worker.repository,
            "_InductionWorkerRepositoryView__repository",
            self.induction_worker.repository,
        )
        if induction_repository is not self.stores.episode_results:
            raise ValueError("induction worker result authority mismatch")
        self._validate_tool_handler_bindings()
        self._validate_persistent_store_bindings()

    def _validate_tool_handler_bindings(self) -> None:
        if self.tool_executor.core_service is not self.core_tool_service:
            raise ValueError("Product Tool core owner binding mismatch")
        if self.runtime_read_service.care_store is not self.stores.care_context:
            raise ValueError("Product Tool Care read authority mismatch")
        if self.runtime_read_service.care_catalog is not self.policies.care_catalog:
            raise ValueError("Product Tool Care catalog authority mismatch")

        expected_handlers = {
            **self.core_tool_service.handlers(),
            **self.habit_runtime.handlers(),
            "memory.read": self.longitudinal_memory.read,
            "memory.resolve_source": self.longitudinal_memory.resolve_source,
            "memory.review_candidates": (
                self.longitudinal_memory.review_pending_candidates
            ),
            "memory.prepare_candidate": (
                self.longitudinal_memory.prepare_pending_candidate
            ),
            "care.read_state": self.runtime_read_service.read_current_care_state,
            "care.read_catalog": self.runtime_read_service.read_care_catalog,
            "care.read_constraints": (
                self.runtime_read_service.read_care_constraints
            ),
            "device.read_delivery_policy": (
                self.runtime_read_service.read_delivery_policy
            ),
            "coordination.read_policy": (
                self.runtime_read_service.read_coordination_policy
            ),
            "policy.read": self.runtime_read_service.read_runtime_policy,
        }
        if set(self.tool_executor.handlers) != set(expected_handlers):
            raise ValueError(
                "Product Tool handlers must equal the registered executor owners"
            )
        for name, expected_handler in expected_handlers.items():
            if self.tool_executor.handlers.get(name) != expected_handler:
                raise ValueError(f"Product Tool handler binding mismatch: {name}")
        registered_reads = {
            name
            for name, definition in TOOL_DEFINITIONS.items()
            if definition.effect is ToolEffect.READ_ONLY
        }
        receipt_only = {"cold_start.evaluate"}
        if registered_reads | set(RUNTIME_INTERACTION_TOOLS) != (
            set(expected_handlers) | receipt_only
        ):
            raise ValueError(
                "registered Product executable Tools lack exact owners"
            )
        if not callable(build_cold_start_receipt):
            raise ValueError("cold-start Tool receipt owner is unavailable")
        if set(TOOL_DEFINITIONS) != (
            registered_reads
            | set(RUNTIME_INTERACTION_TOOLS)
            | set(COMMIT_CONTROLLER_TOOLS)
        ):
            raise ValueError("registered Product Tool owner inventory is incomplete")
        if any(
            TOOL_DEFINITIONS[name].owner != "questionnaire"
            or TOOL_DEFINITIONS[name].effect is not ToolEffect.STATE_WRITE
            for name in RUNTIME_INTERACTION_TOOLS
        ):
            raise ValueError(
                "runtime interaction Tools must be questionnaire state writes"
            )
        if any(
            TOOL_DEFINITIONS[name].owner != "commit_controller"
            for name in COMMIT_CONTROLLER_TOOLS
        ):
            raise ValueError("state-changing Tools must belong to Commit Controller")

    def _validate_persistent_store_bindings(self) -> None:
        if self.persistence_store is None:
            persistent = (
                self.stores.care_context,
                self.stores.memory_context,
                self.stores.habit_profile,
                self.stores.habit_questionnaire_state,
                self.stores.habit_pending_change_sets,
                self.stores.commit_journal,
                self.stores.episode_results,
                self.stores.human_decisions,
            )
            if any(
                hasattr(item, "persistence") or hasattr(item, "_store")
                for item in persistent
            ):
                raise ValueError("in-memory bundle contains a persistent adapter")
            return
        store = self.persistence_store
        bindings = (
            getattr(self.stores.care_context, "persistence", None),
            getattr(self.stores.memory_context, "persistence", None),
            getattr(self.stores.habit_profile, "persistence", None),
            getattr(self.stores.habit_questionnaire_state, "persistence", None),
            getattr(self.stores.habit_pending_change_sets, "_store", None),
            getattr(self.stores.commit_journal, "persistence", None),
            getattr(self.stores.episode_results, "persistence", None),
            getattr(self.stores.human_decisions, "_store", None),
        )
        if any(item is not store for item in bindings):
            raise ValueError("persistent Product stores must share one connection")


def build_product_runtime_bundle(
    *,
    sleepcare_model: StructuredAgentModel | None = None,
    evidence_reasoning_model: StructuredAgentModel | None = None,
    care_strategy_model: StructuredAgentModel | None = None,
    safety_review_model: StructuredAgentModel | None = None,
    sleepcare_planning_model: StructuredAgentModel | None = None,
    agent_roster: ProductAgentRoster | None = None,
    skill_registry: SkillRegistry | None = None,
    persistence_store: RadarPersistenceStore | None = None,
    stores: ProductRuntimeStores | None = None,
    source_resolvers: Mapping[str, CanonicalSourceResolver] | None = None,
    human_decisions: HumanDecisionService | None = None,
    authority_validator: DecisionAuthorityValidator | None = None,
    external_executor: ExternalActionExecutor | None = None,
    publisher: PublicationPublisher | None = None,
    fact_snapshot_revalidator: FactSnapshotRevalidator | None = None,
    care_catalog: CareActionCatalog | None = None,
) -> ProductRuntimeBundle:
    """Compose one explicit Product runtime over persistent or in-memory stores."""

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
    resolved_stores = (
        stores if stores is not None else _build_stores(persistence_store)
    )
    if human_decisions is not None and authority_validator is not None:
        raise ValueError(
            "inject human_decisions or an authority_validator, not both"
        )
    resolved_authority_validator = authority_validator
    if (
        human_decisions is None
        and resolved_authority_validator is None
        and persistence_store is not None
    ):
        resolved_authority_validator = (
            build_persistent_decision_authority_validator(persistence_store)
        )
    decision_service = human_decisions or HumanDecisionService(
        resolved_stores.human_decisions,
        authority_validator=resolved_authority_validator,
    )
    if human_decisions is not None:
        if (
            stores is not None
            and decision_service._repository is not stores.human_decisions
        ):
            raise ValueError(
                "injected HDS must use the injected ProductRuntimeStores "
                "decision repository"
            )
        resolved_stores = ProductRuntimeStores(
            care_context=resolved_stores.care_context,
            memory_context=resolved_stores.memory_context,
            habit_profile=resolved_stores.habit_profile,
            habit_questionnaire_state=(
                resolved_stores.habit_questionnaire_state
            ),
            habit_pending_change_sets=(
                resolved_stores.habit_pending_change_sets
            ),
            objective_baseline=resolved_stores.objective_baseline,
            commit_journal=resolved_stores.commit_journal,
            episode_results=resolved_stores.episode_results,
            human_decisions=decision_service._repository,
        )

    controller = DeterministicCommitController(
        care_store=resolved_stores.care_context,
        memory_store=resolved_stores.memory_context,
        habit_profile_store=resolved_stores.habit_profile,
        commit_journal=resolved_stores.commit_journal,
    )
    questionnaire = HabitQuestionnaireService(
        state_store=resolved_stores.habit_questionnaire_state
    )
    habit_runtime = HabitProfileRuntimeService(
        questionnaire=questionnaire,
        store=resolved_stores.habit_profile,
        baseline_store=resolved_stores.objective_baseline,
    )
    habit_application = HabitProfileApplicationService(
        human_decisions=decision_service,
        runtime=habit_runtime,
        commit_controller=controller,
        pending_store=resolved_stores.habit_pending_change_sets,
    )
    result_store = resolved_stores.episode_results
    memory = LongitudinalMemoryService(
        memory_store=resolved_stores.memory_context,
        repository=result_store,
        source_resolvers=dict(source_resolvers or {}),
    )
    induction_worker = DeterministicInductionWorker(result_store)
    catalog = care_catalog if care_catalog is not None else CareActionCatalog()
    core_tool_service = CoreProductToolService()
    executor = ProductToolExecutor(core_service=core_tool_service)
    runtime_read_service = ProductRuntimeReadService(
        care_store=resolved_stores.care_context,
        care_catalog=catalog,
    )
    _register_runtime_tool_handlers(
        executor=executor,
        habit_runtime=habit_runtime,
        longitudinal_memory=memory,
        runtime_read_service=runtime_read_service,
    )
    resolved_publisher = (
        publisher if publisher is not None else LocalPublicationPublisher()
    )
    resolved_external_executor = (
        external_executor
        if external_executor is not None
        else UnconfiguredExternalActionExecutor()
    )
    resolver = SkillResolver(registry)
    compiler = PromptCompiler()
    profiles: Mapping[AgentId, AgentProfile] = MappingProxyType(
        default_agent_profiles()
    )
    provider_ledger = ProviderInputBudgetLedger()
    tool_execution_coordinator = ToolExecutionCoordinator(executor)
    agent_invocation_coordinator = AgentInvocationCoordinator(
        tool_execution_coordinator=tool_execution_coordinator,
        longitudinal_memory=memory,
        skill_resolver=resolver,
        prompt_compiler=compiler,
        agent_profiles=profiles,
        provider_input_budget=provider_ledger,
    )
    roster.sleepcare.bind_control_invoker(agent_invocation_coordinator)
    confirmed_action_coordinator = ConfirmedActionCoordinator(
        commit_controller=controller,
        human_decisions=decision_service,
        external_executor=resolved_external_executor,
    )
    publication_service = PublicationService(
        publisher=resolved_publisher,
        result_store=result_store,
        longitudinal_memory=memory,
        revalidator=fact_snapshot_revalidator,
    )
    episode_result_finalizer = EpisodeResultFinalizer(
        result_store=result_store,
        tool_execution_coordinator=tool_execution_coordinator,
        provider_input_budget_ledger=provider_ledger,
    )
    policies = ProductRuntimePolicies(
        care_catalog=catalog,
        human_decision_policy=decision_service.policy,
        decision_authority_validator=decision_service.authority_validator,
        fact_snapshot_revalidator=fact_snapshot_revalidator,
        source_resolvers=MappingProxyType(dict(source_resolvers or {})),
    )
    runner = ProductEpisodeRunner(
        agent_roster=roster,
        care_catalog=catalog,
        habit_runtime=habit_runtime,
        skill_registry=registry,
        agent_invocation_coordinator=agent_invocation_coordinator,
        tool_execution_coordinator=tool_execution_coordinator,
        confirmed_action_coordinator=confirmed_action_coordinator,
        publication_service=publication_service,
        episode_result_finalizer=episode_result_finalizer,
        induction_worker=induction_worker,
    )
    return ProductRuntimeBundle(
        runner=runner,
        roster=roster,
        tool_executor=executor,
        skill_registry=registry,
        skill_resolver=resolver,
        prompt_compiler=compiler,
        agent_profiles=profiles,
        agent_invocation_coordinator=agent_invocation_coordinator,
        tool_execution_coordinator=tool_execution_coordinator,
        confirmed_action_coordinator=confirmed_action_coordinator,
        publication_service=publication_service,
        episode_result_finalizer=episode_result_finalizer,
        human_decisions=decision_service,
        longitudinal_memory=memory,
        habit_runtime=habit_runtime,
        habit_application=habit_application,
        stores=resolved_stores,
        commit_controller=controller,
        induction_worker=induction_worker,
        external_executor=resolved_external_executor,
        publisher=resolved_publisher,
        policies=policies,
        provider_input_ledger=provider_ledger,
        persistence_store=persistence_store,
        core_tool_service=core_tool_service,
        runtime_read_service=runtime_read_service,
    )


def build_product_runtime_bundle_from_env(
    *,
    persistence_store: RadarPersistenceStore | None = None,
    source_resolvers: Mapping[str, CanonicalSourceResolver] | None = None,
    human_decisions: HumanDecisionService | None = None,
    authority_validator: DecisionAuthorityValidator | None = None,
    fact_snapshot_revalidator: FactSnapshotRevalidator | None = None,
) -> ProductRuntimeBundle:
    """Build the one configured production four-role runtime."""

    if (
        persistence_store is not None
        and os.getenv(DEPLOYMENT_MODE_ENV, "development").strip().lower()
        == "production"
        and persistence_store.dialect != "postgres"
    ):
        raise RuntimeError(
            "production Product Agent runtime requires shared PostgreSQL "
            "storage"
        )
    persistence = persistence_store or _persistence_store_from_env()
    return _build_openai_compatible_product_runtime_bundle(
        persistence_store=persistence,
        source_resolvers=source_resolvers,
        human_decisions=human_decisions,
        authority_validator=authority_validator,
        fact_snapshot_revalidator=fact_snapshot_revalidator,
    )


def _build_postgres_worker_product_runtime_bundle_from_env(
) -> ProductRuntimeBundle:
    """Build live models whose durable result is owned by the Worker UoW."""

    return _build_openai_compatible_product_runtime_bundle(
        persistence_store=None,
        source_resolvers=None,
        human_decisions=None,
        authority_validator=None,
        fact_snapshot_revalidator=None,
    )


def _build_openai_compatible_product_runtime_bundle(
    *,
    persistence_store: RadarPersistenceStore | None,
    source_resolvers: Mapping[str, CanonicalSourceResolver] | None,
    human_decisions: HumanDecisionService | None,
    authority_validator: DecisionAuthorityValidator | None,
    fact_snapshot_revalidator: FactSnapshotRevalidator | None,
) -> ProductRuntimeBundle:
    provider_config = openai_compatible_provider_config_from_env()
    return build_product_runtime_bundle(
        sleepcare_model=OpenAICompatibleStructuredAgentModel(
            config=provider_config
        ),
        evidence_reasoning_model=OpenAICompatibleStructuredAgentModel(
            config=provider_config
        ),
        care_strategy_model=OpenAICompatibleStructuredAgentModel(
            config=provider_config
        ),
        safety_review_model=OpenAICompatibleStructuredAgentModel(
            config=provider_config
        ),
        persistence_store=persistence_store,
        source_resolvers=source_resolvers,
        human_decisions=human_decisions,
        authority_validator=authority_validator,
        external_executor=ConfiguredExternalActionExecutor.from_env(),
        fact_snapshot_revalidator=fact_snapshot_revalidator,
    )


def build_deterministic_product_runtime_bundle(
    *,
    model: StructuredAgentModel,
    source_resolvers: Mapping[str, CanonicalSourceResolver] | None = None,
    external_executor: ExternalActionExecutor | None = None,
    fact_snapshot_revalidator: FactSnapshotRevalidator | None = None,
) -> ProductRuntimeBundle:
    """Build the in-memory four-role graph used by deterministic replay."""

    return build_product_runtime_bundle(
        sleepcare_model=model,
        evidence_reasoning_model=model,
        care_strategy_model=model,
        safety_review_model=model,
        sleepcare_planning_model=model,
        source_resolvers=source_resolvers,
        external_executor=external_executor,
        fact_snapshot_revalidator=fact_snapshot_revalidator,
    )


def build_product_episode_runner_from_env(
    *,
    persistence_store: RadarPersistenceStore | None = None,
    source_resolvers: Mapping[str, CanonicalSourceResolver] | None = None,
    human_decisions: HumanDecisionService | None = None,
    authority_validator: DecisionAuthorityValidator | None = None,
    fact_snapshot_revalidator: FactSnapshotRevalidator | None = None,
) -> ProductEpisodeRunner:
    """Compatibility facade; all composition belongs to the bundle builder."""

    return build_product_runtime_bundle_from_env(
        persistence_store=persistence_store,
        source_resolvers=source_resolvers,
        human_decisions=human_decisions,
        authority_validator=authority_validator,
        fact_snapshot_revalidator=fact_snapshot_revalidator,
    ).runner


def product_episode_runner_is_configured(
    runner: ProductEpisodeRunner,
) -> bool:
    roster = getattr(runner, "agent_roster", None)
    if type(roster) is not ProductAgentRoster:
        return False
    models = [
        roster.sleepcare.planning_model,
        *(item.model for item in roster),
    ]
    return all(
        model is not None and bool(getattr(model, "is_configured", True))
        for model in models
    )


def build_persistent_decision_authority_validator(
    persistence_store: RadarPersistenceStore,
) -> DecisionAuthorityValidator:
    """Bind HDS decisions to persisted roles and data authorization."""

    def validate(
        request: HumanDecisionRequest,
        actor_id: str,
        actor_role: str,
        role_binding_id: str | None,
        authorization_id: str | None,
    ) -> None:
        if os.getenv(RADAR_AGENT_DEV_MODE_ENV, "false").lower() == "true":
            if actor_role not in {item.role for item in request.requirements}:
                raise HumanDecisionError(
                    "actor role is not required by this decision"
                )
            return
        if role_binding_id is None:
            raise HumanDecisionError(
                "a role binding is required for this decision"
            )
        bindings = {
            item.role_binding_id: item
            for item in persistence_store.list_role_bindings(
                request.proposal.subject_id
            )
        }
        binding = bindings.get(role_binding_id)
        if binding is None or (
            binding.user_id != actor_id
            or binding.role != actor_role
            or binding.subject_id != request.proposal.subject_id
        ):
            raise HumanDecisionError(
                "role binding does not authorize this actor"
            )
        if (
            request.proposal.action_kind == "external_action"
            and actor_role == "doctor"
        ):
            required_permission = "read_doctor_material"
            required_scope = "process_supplementary_document"
        elif request.proposal.action_kind == "external_action":
            required_permission = "export_data"
            required_scope = "export_data"
        else:
            required_permission = "process_health_data"
            required_scope = "process_radar_summary"
        if required_permission not in binding.permissions:
            raise HumanDecisionError(
                f"role binding lacks {required_permission!r} permission"
            )
        effective_authorization_id = (
            authorization_id or request.proposal.authorization_id
        )
        if effective_authorization_id is None:
            raise HumanDecisionError(
                "an active data authorization is required"
            )
        authorization = persistence_store.get_data_authorization(
            effective_authorization_id
        )
        now = datetime.now(timezone.utc)
        if (
            authorization.subject_id != request.proposal.subject_id
            or authorization.status != "active"
            or (
                authorization.expires_at is not None
                and authorization.expires_at <= now
            )
        ):
            raise HumanDecisionError(
                "data authorization is inactive or mismatched"
            )
        if required_scope not in authorization.scopes:
            raise HumanDecisionError(
                f"data authorization lacks {required_scope!r} scope"
            )

    return validate


def _registry_from_roster(
    roster: ProductAgentRoster | None,
) -> SkillRegistry:
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
            agent.skill_registry is not skill_registry
            for agent in agent_roster
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


def _build_stores(
    persistence: RadarPersistenceStore | None,
) -> ProductRuntimeStores:
    baseline_store = InMemoryObjectiveBaselineStore()
    if persistence is None:
        return ProductRuntimeStores(
            care_context=InMemoryCareContextStore(),
            memory_context=InMemoryMemoryContextStore(),
            habit_profile=InMemoryHabitProfileStore(),
            habit_questionnaire_state=InMemoryHabitQuestionnaireStateStore(),
            habit_pending_change_sets=InMemoryHabitPendingChangeSetStore(),
            objective_baseline=baseline_store,
            commit_journal=InMemoryCommitJournal(),
            episode_results=InMemoryLongitudinalResultStore(),
            human_decisions=InMemoryHumanDecisionRepository(),
        )
    return ProductRuntimeStores(
        care_context=PersistentCareContextStore(persistence),
        memory_context=PersistentMemoryContextStore(persistence),
        habit_profile=PersistentHabitProfileStore(persistence),
        habit_questionnaire_state=PersistentHabitQuestionnaireStateStore(
            persistence
        ),
        habit_pending_change_sets=PersistentHabitPendingChangeSetStore(
            persistence
        ),
        objective_baseline=baseline_store,
        commit_journal=PersistentCommitJournal(persistence),
        episode_results=PersistentProductEpisodeResultStore(persistence),
        human_decisions=PersistentHumanDecisionRepository(persistence),
    )


def _register_runtime_tool_handlers(
    *,
    executor: ProductToolExecutor,
    habit_runtime: HabitProfileRuntimeService,
    longitudinal_memory: LongitudinalMemoryService,
    runtime_read_service: ProductRuntimeReadService,
) -> None:
    for tool_name, handler in habit_runtime.handlers().items():
        executor.register_handler(tool_name, handler)
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
        "device.read_delivery_policy",
        runtime_read_service.read_delivery_policy,
    )
    executor.register_handler(
        "coordination.read_policy",
        runtime_read_service.read_coordination_policy,
    )
    executor.register_handler(
        "policy.read", runtime_read_service.read_runtime_policy
    )
    executor.register_handler("memory.read", longitudinal_memory.read)
    executor.register_handler(
        "memory.resolve_source", longitudinal_memory.resolve_source
    )
    executor.register_handler(
        "memory.review_candidates", longitudinal_memory.review_pending_candidates
    )
    executor.register_handler(
        "memory.prepare_candidate", longitudinal_memory.prepare_pending_candidate
    )


def _persistence_store_from_env() -> RadarPersistenceStore:
    database_url = os.getenv(RADAR_AGENT_DATABASE_URL_ENV)
    production = (
        os.getenv(DEPLOYMENT_MODE_ENV, "development").strip().lower()
        == "production"
    )
    if database_url:
        lowered = database_url.strip().lower()
        if production and (
            lowered.startswith("sqlite")
            or ":memory:" in lowered
            or "/tmp/" in lowered
        ):
            raise RuntimeError(
                "production Product Agent runtime requires shared PostgreSQL "
                "storage, not SQLite or /tmp"
            )
        return connect_postgres_store(database_url)
    if production:
        raise RuntimeError(
            "production Product Agent runtime requires "
            f"{RADAR_AGENT_DATABASE_URL_ENV}"
        )
    sqlite_path = Path(
        os.getenv(
            RADAR_AGENT_SQLITE_PATH_ENV,
            DEFAULT_RADAR_AGENT_SQLITE_PATH,
        )
    )
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    return RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(
            sqlite_path,
            check_same_thread=False,
            timeout=30,
        )
    )


__all__ = [
    "DEPLOYMENT_MODE_ENV",
    "DEFAULT_RADAR_AGENT_SQLITE_PATH",
    "LocalPublicationPublisher",
    "PRODUCT_EPISODE_API_ADAPTER_VERSION",
    "ProductRuntimeBundle",
    "ProductRuntimePolicies",
    "ProductRuntimeStores",
    "RADAR_AGENT_DATABASE_URL_ENV",
    "RADAR_AGENT_DEV_MODE_ENV",
    "RADAR_AGENT_SQLITE_PATH_ENV",
    "build_deterministic_product_runtime_bundle",
    "build_persistent_decision_authority_validator",
    "build_product_episode_runner_from_env",
    "build_product_runtime_bundle",
    "build_product_runtime_bundle_from_env",
    "product_episode_runner_is_configured",
]
