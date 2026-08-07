from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from typing import Mapping

from sleepagent.radar_agent.persistence import (
    RadarPersistenceStore,
    connect_postgres_store,
)
from sleepagent.radar_agent.product_agent.agents import (
    ProductAgentFactory,
    ProductAgentRoster,
)
from sleepagent.radar_agent.product_agent.external_actions import (
    ConfiguredExternalActionExecutor,
)
from sleepagent.radar_agent.product_agent.governance import (
    DeterministicCommitController,
)
from sleepagent.radar_agent.product_agent.habit_persistence import (
    PersistentHabitProfileStore,
    PersistentHabitQuestionnaireStateStore,
)
from sleepagent.radar_agent.product_agent.habit_runtime import (
    HabitProfileRuntimeService,
)
from sleepagent.radar_agent.product_agent.longitudinal_memory import (
    CanonicalSourceResolver,
)
from sleepagent.radar_agent.product_agent.product_persistence import (
    PersistentCareContextStore,
    PersistentCommitJournal,
    PersistentMemoryContextStore,
    PersistentProductEpisodeResultStore,
)
from sleepagent.radar_agent.product_agent.provider import (
    OpenAICompatibleStructuredAgentModel,
)
from sleepagent.radar_agent.product_agent.runner import ProductEpisodeRunner
from sleepagent.radar_agent.product_agent.skills import (
    SkillRegistry,
    default_skill_packages,
)
from sleepagent.radar_agent.questionnaire import HabitQuestionnaireService


PRODUCT_EPISODE_API_ADAPTER_VERSION = "sleepagent-product-api-adapter.v2"
RADAR_AGENT_DATABASE_URL_ENV = "SLEEPAGENT_RADAR_AGENT_DATABASE_URL"
RADAR_AGENT_SQLITE_PATH_ENV = "SLEEPAGENT_RADAR_AGENT_SQLITE_PATH"
DEFAULT_RADAR_AGENT_SQLITE_PATH = "/tmp/sleepagent_radar_agent.sqlite3"
DEPLOYMENT_MODE_ENV = "SLEEPAGENT_DEPLOYMENT_MODE"


def build_product_episode_runner_from_env(
    *,
    persistence_store: RadarPersistenceStore | None = None,
    source_resolvers: Mapping[str, CanonicalSourceResolver] | None = None,
) -> ProductEpisodeRunner:
    """Build the one production four-role runtime from environment config."""

    persistence = persistence_store or _persistence_store_from_env()
    habit_profile_store = PersistentHabitProfileStore(persistence)
    habit_runtime = HabitProfileRuntimeService(
        questionnaire=HabitQuestionnaireService(
            state_store=PersistentHabitQuestionnaireStateStore(persistence)
        ),
        store=habit_profile_store,
    )
    commit_controller = DeterministicCommitController(
        care_store=PersistentCareContextStore(persistence),
        memory_store=PersistentMemoryContextStore(persistence),
        habit_profile_store=habit_profile_store,
        commit_journal=PersistentCommitJournal(persistence),
    )
    skill_registry = SkillRegistry(default_skill_packages())
    agent_roster = ProductAgentFactory.create(
        sleepcare_model=OpenAICompatibleStructuredAgentModel(),
        evidence_reasoning_model=OpenAICompatibleStructuredAgentModel(),
        care_strategy_model=OpenAICompatibleStructuredAgentModel(),
        safety_review_model=OpenAICompatibleStructuredAgentModel(),
        skill_registry=skill_registry,
    )
    return ProductEpisodeRunner(
        agent_roster=agent_roster,
        skill_registry=skill_registry,
        habit_runtime=habit_runtime,
        commit_controller=commit_controller,
        result_store=PersistentProductEpisodeResultStore(persistence),
        external_executor=ConfiguredExternalActionExecutor.from_env(),
        source_resolvers=source_resolvers,
    )


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
    "PRODUCT_EPISODE_API_ADAPTER_VERSION",
    "RADAR_AGENT_DATABASE_URL_ENV",
    "RADAR_AGENT_SQLITE_PATH_ENV",
    "build_product_episode_runner_from_env",
    "product_episode_runner_is_configured",
]
