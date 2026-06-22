from __future__ import annotations

import pytest

from sleepagent.services import repository_factory
from sleepagent.services.postgres import (
    DATABASE_URL_ENV,
    POSTGRES_SCHEMA_SQL,
    PostgresUnavailableError,
    split_postgres_schema_statements,
)
from sleepagent.services.repository_factory import (
    REPOSITORY_BACKEND_LOCAL,
    REPOSITORY_BACKEND_POSTGRES,
    SLEEPAGENT_REPOSITORY_BACKEND_ENV,
    build_repository_bundle,
)


def test_phase4_postgres_schema_contains_repeatable_core_tables() -> None:
    statements = split_postgres_schema_statements()
    required_tables = {
        "subjects",
        "sleep_records",
        "tasks",
        "task_events",
        "analysis_results",
        "reports",
        "artifacts",
        "artifact_versions",
        "memory_summaries",
        "alert_events",
        "external_contexts",
    }

    assert statements
    assert all("CREATE TABLE IF NOT EXISTS" in statement for statement in statements)
    for table in required_tables:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in POSTGRES_SCHEMA_SQL

    assert "UNIQUE(artifact_id, version_number)" in POSTGRES_SCHEMA_SQL
    assert "record_json JSONB NOT NULL" in POSTGRES_SCHEMA_SQL
    assert "task_json JSONB NOT NULL" in POSTGRES_SCHEMA_SQL
    assert "artifact_json JSONB NOT NULL" in POSTGRES_SCHEMA_SQL
    assert "version_json JSONB NOT NULL" in POSTGRES_SCHEMA_SQL


def test_repository_factory_auto_falls_back_to_local_when_postgres_fails(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://invalid")
    monkeypatch.setenv(SLEEPAGENT_REPOSITORY_BACKEND_ENV, "auto")
    monkeypatch.setenv("SLEEPAGENT_DATA_STORE_DIR", str(tmp_path))
    monkeypatch.setattr(
        repository_factory,
        "PostgresTaskRepository",
        _raising_postgres_repository,
    )

    bundle = build_repository_bundle()

    assert bundle.backend == REPOSITORY_BACKEND_LOCAL


def test_repository_factory_postgres_mode_stays_strict(monkeypatch) -> None:
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://invalid")
    monkeypatch.setenv(SLEEPAGENT_REPOSITORY_BACKEND_ENV, REPOSITORY_BACKEND_POSTGRES)
    monkeypatch.setattr(
        repository_factory,
        "PostgresTaskRepository",
        _raising_postgres_repository,
    )

    with pytest.raises(PostgresUnavailableError):
        build_repository_bundle()


def _raising_postgres_repository(*_: object, **__: object) -> object:
    raise PostgresUnavailableError("postgres unavailable in unit test")
