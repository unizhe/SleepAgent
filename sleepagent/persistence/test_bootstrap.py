# 本模块负责 PostgreSQL 持久化边界与完整性校验，不提供内存或 SQLite 旁路。
"""Explicit test-only PostgreSQL role, grant, and replay seed bootstrap."""

from __future__ import annotations

import os
import sys
from typing import Any, cast

from sleepagent.persistence.migrate import (
    DEFAULT_DATABASE_URL_ENV,
    ConnectionLike,
    MigrationError,
    MigrationStateError,
    assert_migration_owner_capability,
    bootstrap_test_database_roles_from_environment,
)


def run_test_bootstrap(*, dsn: str) -> None:
    if not dsn.strip():
        raise MigrationStateError("PostgreSQL database URL is required")
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - production dependency.
        raise MigrationStateError(
            "psycopg is required to bootstrap the test database"
        ) from exc
    with psycopg.connect(
        dsn,
        autocommit=False,
        application_name="sleepagent-test-bootstrap",
    ) as raw_connection:
        connection = cast(ConnectionLike, cast(Any, raw_connection))
        assert_migration_owner_capability(connection)
        if not bootstrap_test_database_roles_from_environment(connection):
            raise MigrationStateError(
                "test database bootstrap variables are required"
            )


def main() -> int:
    dsn = os.environ.get(DEFAULT_DATABASE_URL_ENV, "")
    try:
        run_test_bootstrap(dsn=dsn)
    except MigrationError as exc:
        print(f"test database bootstrap refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # Keep credentials and SQL bodies out of logs.
        print(
            f"test database bootstrap failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 3
    print("test database bootstrap complete")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
