from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sleepagent.services.artifact_repository import (
    ArtifactRepository,
    LocalJsonlArtifactRepository,
)
from sleepagent.services.data_management import (
    SLEEPAGENT_DATA_STORE_DIR_ENV,
    LocalJsonlSleepDataRepository,
    SleepDataRepository,
)
from sleepagent.services.memory_repository import (
    LocalJsonlMemoryRepository,
    MemoryRepository,
)
from sleepagent.services.postgres import (
    DATABASE_URL_ENV,
    PostgresArtifactRepository,
    PostgresMemoryRepository,
    PostgresSleepDataRepository,
    PostgresTaskRepository,
    PostgresUnavailableError,
)
from sleepagent.services.task_repository import (
    LocalJsonlTaskRepository,
    TaskRepository,
)


SLEEPAGENT_REPOSITORY_BACKEND_ENV = "SLEEPAGENT_REPOSITORY_BACKEND"
REPOSITORY_BACKEND_AUTO = "auto"
REPOSITORY_BACKEND_POSTGRES = "postgres"
REPOSITORY_BACKEND_LOCAL = "local"
DEFAULT_TASK_API_STORE_DIR = "/tmp/sleepagent_task_api"


@dataclass(frozen=True)
class RepositoryBundle:
    task_repository: TaskRepository
    artifact_repository: ArtifactRepository
    memory_repository: MemoryRepository
    data_repository: SleepDataRepository
    backend: str


def build_repository_bundle() -> RepositoryBundle:
    backend = os.getenv(
        SLEEPAGENT_REPOSITORY_BACKEND_ENV,
        REPOSITORY_BACKEND_AUTO,
    ).strip().lower()
    if backend not in {
        REPOSITORY_BACKEND_AUTO,
        REPOSITORY_BACKEND_POSTGRES,
        REPOSITORY_BACKEND_LOCAL,
    }:
        raise ValueError(
            f"{SLEEPAGENT_REPOSITORY_BACKEND_ENV} must be one of: "
            "auto, postgres, local."
        )

    database_url = os.getenv(DATABASE_URL_ENV)
    if backend in {REPOSITORY_BACKEND_AUTO, REPOSITORY_BACKEND_POSTGRES} and database_url:
        try:
            return RepositoryBundle(
                task_repository=PostgresTaskRepository(database_url),
                artifact_repository=PostgresArtifactRepository(database_url),
                memory_repository=PostgresMemoryRepository(database_url),
                data_repository=PostgresSleepDataRepository(database_url),
                backend=REPOSITORY_BACKEND_POSTGRES,
            )
        except Exception:
            if backend == REPOSITORY_BACKEND_POSTGRES:
                raise

    if backend == REPOSITORY_BACKEND_POSTGRES:
        raise PostgresUnavailableError(f"{DATABASE_URL_ENV} is not configured.")

    store_dir = _resolve_local_store_dir()
    return RepositoryBundle(
        task_repository=LocalJsonlTaskRepository(store_dir),
        artifact_repository=LocalJsonlArtifactRepository(store_dir),
        memory_repository=LocalJsonlMemoryRepository(store_dir),
        data_repository=LocalJsonlSleepDataRepository(store_dir),
        backend=REPOSITORY_BACKEND_LOCAL,
    )


def _resolve_local_store_dir() -> Path:
    return Path(os.getenv(SLEEPAGENT_DATA_STORE_DIR_ENV, DEFAULT_TASK_API_STORE_DIR))
