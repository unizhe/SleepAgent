"""Fail-closed PostgreSQL schema migration runner.

The application and worker runtimes must never import a side effect that runs
DDL.  This module is therefore an explicit command boundary: it discovers the
immutable SQL release, serializes runners with a session advisory lock, and
records checksums in the v2 ledger introduced by migration 021.

Migration 021 is special.  Its checksum is pinned in this runner so a database
cannot bootstrap the checksum ledger from an untrusted copy of the ledger
definition itself.  Existing 001-020 installations are attested once from the
legacy ``(version, applied_at)`` ledger.  Any partial legacy history, checksum
drift, failed row, or crash-left ``started`` row requires operator recovery.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import re
import socket
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal, Protocol, Sequence

from sleepagent.radar_agent.persistence.migrations import split_sql_statements


LEGACY_LAST_VERSION = 20
LEDGER_BOOTSTRAP_VERSION = 21
LATEST_SCHEMA_VERSION = 25
RELEASE_021_SHA256 = (
    "6e1270dece670e94157619af7a40300bc856f8e9386e15fb7c5975dbed4d4d68"
)
MIGRATION_ADVISORY_LOCK_ID = 7_216_457_676_974_471_169
DEFAULT_DATABASE_URL_ENV = "SLEEPAGENT_BACKEND_DATABASE_DSN"
BOOTSTRAP_API_ROLE_ENV = "SLEEPAGENT_BOOTSTRAP_API_DATABASE_ROLE"
BOOTSTRAP_API_PASSWORD_ENV = "SLEEPAGENT_BOOTSTRAP_API_DATABASE_PASSWORD"
BOOTSTRAP_WORKER_ROLE_ENV = "SLEEPAGENT_BOOTSTRAP_WORKER_DATABASE_ROLE"
BOOTSTRAP_WORKER_PASSWORD_ENV = "SLEEPAGENT_BOOTSTRAP_WORKER_DATABASE_PASSWORD"

_MIGRATION_FILE_PATTERN = re.compile(
    r"^(?P<version>[0-9]{3})_(?P<slug>[a-z0-9_]+)\.sql$"
)
_TRANSACTION_MARKER = re.compile(
    r"^--\s*sleepagent:transactional=(?P<value>true|false)\s*$",
    re.MULTILINE,
)
_DEFAULT_MIGRATION_DIRECTORY = Path(__file__).parent / "migrations"


class MigrationError(RuntimeError):
    """Base error for migration validation and execution."""


class MigrationReleaseError(MigrationError):
    """The SQL files do not form the expected immutable release."""


class MigrationStateError(MigrationError):
    """The database ledger is incomplete, corrupt, or needs recovery."""


class MigrationExecutionError(MigrationError):
    """A migration failed after its durable ``started`` record was written."""


class CursorLike(Protocol):
    rowcount: int

    def execute(self, query: str, params: Any = None) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> Any: ...

    def close(self) -> None: ...


class ConnectionLike(Protocol):
    def cursor(self) -> CursorLike: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@dataclass(frozen=True, slots=True)
class MigrationFile:
    version: int
    name: str
    path: Path
    sql: str
    sql_sha256: str
    transactional: bool


@dataclass(frozen=True, slots=True)
class MigrationLedgerRow:
    version: int
    migration_name: str
    sql_sha256: str
    status: str
    transactional: bool
    started_at: Any
    finished_at: Any
    applied_by: str
    error_code: str | None


def discover_migrations(
    directory: Path = _DEFAULT_MIGRATION_DIRECTORY,
) -> tuple[MigrationFile, ...]:
    """Load and validate the exact, contiguous SQL migration release."""

    migrations: list[MigrationFile] = []
    for path in sorted(directory.glob("[0-9][0-9][0-9]_*.sql")):
        match = _MIGRATION_FILE_PATTERN.fullmatch(path.name)
        if match is None:
            raise MigrationReleaseError(f"invalid migration filename: {path.name}")
        raw_sql = path.read_bytes()
        try:
            sql = raw_sql.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationReleaseError(
                f"migration is not UTF-8: {path.name}"
            ) from exc
        version = int(match.group("version"))
        marker = _TRANSACTION_MARKER.search(sql)
        if version >= LEDGER_BOOTSTRAP_VERSION and marker is None:
            raise MigrationReleaseError(
                f"migration {path.name} must declare transactional strategy"
            )
        transactional = marker is None or marker.group("value") == "true"
        migrations.append(
            MigrationFile(
                version=version,
                name=path.stem,
                path=path,
                sql=sql,
                sql_sha256=hashlib.sha256(raw_sql).hexdigest(),
                transactional=transactional,
            )
        )

    expected_versions = list(range(1, LATEST_SCHEMA_VERSION + 1))
    actual_versions = [migration.version for migration in migrations]
    if actual_versions != expected_versions:
        raise MigrationReleaseError(
            "migration release must contain exactly versions 001-025; "
            f"found {actual_versions!r}"
        )
    migration_021 = migrations[LEDGER_BOOTSTRAP_VERSION - 1]
    if migration_021.sql_sha256 != RELEASE_021_SHA256:
        raise MigrationReleaseError(
            "021_migration_ledger_v2.sql does not match the release-pinned "
            "checksum"
        )
    return tuple(migrations)


def validate_ledger_rows(
    migrations: Sequence[MigrationFile],
    rows: Sequence[MigrationLedgerRow],
    *,
    require_complete: bool,
) -> None:
    """Validate immutable identities and a contiguous terminal ledger prefix."""

    expected = {migration.version: migration for migration in migrations}
    seen: dict[int, MigrationLedgerRow] = {}
    for row in rows:
        if row.version in seen:
            raise MigrationStateError(
                f"duplicate migration ledger version {row.version:03d}"
            )
        seen[row.version] = row
        migration = expected.get(row.version)
        if migration is None:
            raise MigrationStateError(
                f"database schema version {row.version:03d} is not supported "
                "by this release"
            )
        if row.migration_name != migration.name:
            raise MigrationStateError(
                f"migration {row.version:03d} name changed from "
                f"{row.migration_name!r} to {migration.name!r}"
            )
        if row.sql_sha256 != migration.sql_sha256:
            raise MigrationStateError(
                f"migration {row.version:03d} SQL checksum drift detected"
            )
        if row.transactional is not migration.transactional:
            raise MigrationStateError(
                f"migration {row.version:03d} transaction strategy changed"
            )
        expected_status = (
            "legacy_attested"
            if row.version <= LEGACY_LAST_VERSION
            else "applied"
        )
        if row.status != expected_status:
            if row.status == "started" and row.finished_at is None:
                detail = "was started but never finished"
            elif row.status == "failed":
                detail = f"is failed ({row.error_code or 'unknown error'})"
            else:
                detail = (
                    f"has status {row.status!r}; expected {expected_status!r}"
                )
            raise MigrationStateError(
                f"migration {row.version:03d} {detail}; operator recovery is "
                "required"
            )
        if row.started_at is None or row.finished_at is None:
            raise MigrationStateError(
                f"migration {row.version:03d} has incomplete timestamps"
            )
        if not row.applied_by.strip():
            raise MigrationStateError(
                f"migration {row.version:03d} has no applied_by identity"
            )

    versions = sorted(seen)
    if versions:
        contiguous = list(range(1, versions[-1] + 1))
        if versions != contiguous:
            raise MigrationStateError(
                f"migration ledger has a version gap: found {versions!r}"
            )
    if not versions or versions[-1] < LEDGER_BOOTSTRAP_VERSION:
        raise MigrationStateError(
            "v2 migration ledger is not bootstrapped through version 021"
        )
    if require_complete and versions != sorted(expected):
        missing = sorted(set(expected).difference(versions))
        raise MigrationStateError(
            f"schema is not at release version 025; missing {missing!r}"
        )


class PostgresMigrationRunner:
    """Apply or check one immutable migration release on one connection."""

    def __init__(
        self,
        connection: ConnectionLike,
        *,
        migrations: Sequence[MigrationFile] | None = None,
        applied_by: str,
    ) -> None:
        if not applied_by.strip():
            raise ValueError("applied_by is required")
        self.connection = connection
        self.migrations = tuple(migrations or discover_migrations())
        self.applied_by = applied_by.strip()
        # Validate injected migration sets with the same release invariants.
        if [item.version for item in self.migrations] != list(
            range(1, LATEST_SCHEMA_VERSION + 1)
        ):
            raise MigrationReleaseError(
                "runner requires the complete contiguous 001-025 release"
            )
        migration_021 = self.migrations[LEDGER_BOOTSTRAP_VERSION - 1]
        if migration_021.sql_sha256 != RELEASE_021_SHA256:
            raise MigrationReleaseError("runner received an untrusted 021 migration")

    def apply(self) -> int:
        """Apply all missing migrations and return the resulting version."""

        with self._advisory_lock():
            self._ensure_legacy_history()
            if not self._table_exists("radar_agent_schema_migrations_v2"):
                self._bootstrap_v2_ledger()
            rows = self._load_v2_rows()
            validate_ledger_rows(
                self.migrations,
                rows,
                require_complete=False,
            )
            applied_versions = {row.version for row in rows}
            for migration in self.migrations:
                if migration.version <= LEDGER_BOOTSTRAP_VERSION:
                    continue
                if migration.version in applied_versions:
                    continue
                self._apply_one(migration)
                applied_versions.add(migration.version)
            final_rows = self._load_v2_rows()
            validate_ledger_rows(
                self.migrations,
                final_rows,
                require_complete=True,
            )
            return final_rows[-1].version

    def check(self) -> int:
        """Read-only verification of legacy and v2 ledger integrity."""

        with self._advisory_lock():
            self._validate_legacy_history()
            if not self._table_exists("radar_agent_schema_migrations_v2"):
                raise MigrationStateError("v2 migration ledger does not exist")
            rows = self._load_v2_rows()
            validate_ledger_rows(
                self.migrations,
                rows,
                require_complete=True,
            )
            return rows[-1].version

    @contextmanager
    def _advisory_lock(self) -> Iterator[None]:
        self.connection.rollback()
        self._fetchone(
            "SELECT pg_advisory_lock(%s)",
            (MIGRATION_ADVISORY_LOCK_ID,),
        )
        # Migration DDL intentionally creates unqualified application objects
        # in the trusted public schema.  Keep pg_catalog second so built-ins are
        # still resolved without accidentally targeting pg_catalog itself.
        self._execute("SET search_path = public, pg_catalog")
        # The lock is session scoped and survives this transaction boundary.
        self.connection.commit()
        try:
            yield
        finally:
            self.connection.rollback()
            row = self._fetchone(
                "SELECT pg_advisory_unlock(%s)",
                (MIGRATION_ADVISORY_LOCK_ID,),
            )
            self.connection.commit()
            if row is not None and row[0] is False:
                raise MigrationStateError("migration advisory lock was not held")

    def _ensure_legacy_history(self) -> None:
        if not self._table_exists("radar_agent_schema_migrations"):
            if self._database_contains_sleepagent_schema():
                raise MigrationStateError(
                    "SleepAgent tables exist without the legacy migration ledger"
                )
            self._apply_pristine_legacy_release()
        self._validate_legacy_history()

    def _apply_pristine_legacy_release(self) -> None:
        self.connection.rollback()
        try:
            for migration in self.migrations[:LEGACY_LAST_VERSION]:
                for statement in split_sql_statements(migration.sql):
                    self._execute(statement)
            self.connection.commit()
        except Exception as exc:
            self.connection.rollback()
            raise MigrationExecutionError(
                "failed to install pristine legacy migrations 001-020"
            ) from exc

    def _validate_legacy_history(self) -> None:
        if not self._table_exists("radar_agent_schema_migrations"):
            raise MigrationStateError("legacy migration ledger does not exist")
        rows = self._fetchall(
            "SELECT version, applied_at "
            "FROM radar_agent_schema_migrations ORDER BY version"
        )
        actual = [str(row[0]) for row in rows]
        expected = [
            migration.name for migration in self.migrations[:LEGACY_LAST_VERSION]
        ]
        if actual != expected:
            raise MigrationStateError(
                "legacy migration ledger must contain exactly contiguous "
                f"001-020; found {actual!r}"
            )

    def _bootstrap_v2_ledger(self) -> None:
        migration_021 = self.migrations[LEDGER_BOOTSTRAP_VERSION - 1]
        if migration_021.sql_sha256 != RELEASE_021_SHA256:
            raise MigrationReleaseError(
                "refusing to bootstrap from a modified migration 021"
            )
        legacy_rows = self._fetchall(
            "SELECT version, applied_at "
            "FROM radar_agent_schema_migrations ORDER BY version"
        )
        applied_at_by_name = {str(row[0]): row[1] for row in legacy_rows}
        self.connection.rollback()
        try:
            for statement in split_sql_statements(migration_021.sql):
                self._execute(statement)
            for migration in self.migrations[:LEGACY_LAST_VERSION]:
                applied_at = applied_at_by_name[migration.name]
                self._execute(
                    "INSERT INTO radar_agent_schema_migrations_v2 ("
                    "version, migration_name, sql_sha256, status, "
                    "transactional, started_at, finished_at, applied_by, "
                    "error_code) VALUES (%s, %s, %s, 'legacy_attested', %s, "
                    "%s, %s, %s, NULL)",
                    (
                        migration.version,
                        migration.name,
                        migration.sql_sha256,
                        migration.transactional,
                        applied_at,
                        applied_at,
                        self.applied_by,
                    ),
                )
            self._execute(
                "INSERT INTO radar_agent_schema_migrations_v2 ("
                "version, migration_name, sql_sha256, status, transactional, "
                "started_at, finished_at, applied_by, error_code) VALUES "
                "(%s, %s, %s, 'applied', %s, clock_timestamp(), "
                "clock_timestamp(), %s, NULL)",
                (
                    migration_021.version,
                    migration_021.name,
                    migration_021.sql_sha256,
                    migration_021.transactional,
                    self.applied_by,
                ),
            )
            self.connection.commit()
        except Exception as exc:
            self.connection.rollback()
            raise MigrationExecutionError(
                "failed to bootstrap checksum migration ledger v2"
            ) from exc

    def _apply_one(self, migration: MigrationFile) -> None:
        self._record_started(migration)
        try:
            if migration.transactional:
                self._apply_transactional(migration)
            else:
                self._apply_non_transactional(migration)
        except Exception as exc:
            self.connection.rollback()
            try:
                self._finish_row(
                    migration,
                    status="failed",
                    error_code=_database_error_code(exc),
                )
            except Exception as ledger_exc:
                self.connection.rollback()
                raise MigrationExecutionError(
                    f"migration {migration.version:03d} failed and its ledger "
                    "row could not be finalized; operator recovery is required"
                ) from ledger_exc
            raise MigrationExecutionError(
                f"migration {migration.version:03d} failed; operator recovery "
                "is required"
            ) from exc

    def _record_started(self, migration: MigrationFile) -> None:
        self.connection.rollback()
        self._execute(
            "INSERT INTO radar_agent_schema_migrations_v2 ("
            "version, migration_name, sql_sha256, status, transactional, "
            "started_at, finished_at, applied_by, error_code) VALUES "
            "(%s, %s, %s, 'started', %s, clock_timestamp(), NULL, %s, NULL)",
            (
                migration.version,
                migration.name,
                migration.sql_sha256,
                migration.transactional,
                self.applied_by,
            ),
        )
        self.connection.commit()

    def _apply_transactional(self, migration: MigrationFile) -> None:
        self.connection.rollback()
        for statement in split_sql_statements(migration.sql):
            self._execute(statement)
        self._finish_row(
            migration,
            status="applied",
            error_code=None,
            commit=False,
        )
        self.connection.commit()

    def _apply_non_transactional(self, migration: MigrationFile) -> None:
        if not hasattr(self.connection, "autocommit"):
            raise MigrationExecutionError(
                "connection does not expose autocommit for a non-transactional "
                "migration"
            )
        self.connection.rollback()
        previous_autocommit = bool(getattr(self.connection, "autocommit"))
        setattr(self.connection, "autocommit", True)
        try:
            for statement in split_sql_statements(migration.sql):
                self._execute(statement)
        finally:
            setattr(self.connection, "autocommit", previous_autocommit)
        self._finish_row(migration, status="applied", error_code=None)

    def _finish_row(
        self,
        migration: MigrationFile,
        *,
        status: Literal["applied", "failed"],
        error_code: str | None,
        commit: bool = True,
    ) -> None:
        changed = self._execute(
            "UPDATE radar_agent_schema_migrations_v2 "
            "SET status = %s, finished_at = clock_timestamp(), error_code = %s "
            "WHERE version = %s AND status = 'started'",
            (status, error_code, migration.version),
        )
        if changed != 1:
            raise MigrationStateError(
                f"migration {migration.version:03d} ledger fence changed "
                f"{changed} rows"
            )
        if commit:
            self.connection.commit()

    def _load_v2_rows(self) -> list[MigrationLedgerRow]:
        rows = self._fetchall(
            "SELECT version, migration_name, sql_sha256, status, "
            "transactional, started_at, finished_at, applied_by, error_code "
            "FROM radar_agent_schema_migrations_v2 ORDER BY version"
        )
        return [
            MigrationLedgerRow(
                version=int(row[0]),
                migration_name=str(row[1]),
                sql_sha256=str(row[2]),
                status=str(row[3]),
                transactional=bool(row[4]),
                started_at=row[5],
                finished_at=row[6],
                applied_by=str(row[7]),
                error_code=None if row[8] is None else str(row[8]),
            )
            for row in rows
        ]

    def _database_contains_sleepagent_schema(self) -> bool:
        for table_name in (
            "radar_subjects",
            "sleep_domain_operations",
            "backend_namespaces",
            "radar_agent_schema_migrations_v2",
        ):
            if self._table_exists(table_name):
                return True
        return False

    def _table_exists(self, table_name: str) -> bool:
        row = self._fetchone("SELECT to_regclass(%s)", (table_name,))
        return row is not None and row[0] is not None

    def _execute(self, query: str, params: Any = None) -> int:
        cursor = self.connection.cursor()
        try:
            cursor.execute(query, params)
            return int(cursor.rowcount)
        finally:
            cursor.close()

    def _fetchone(self, query: str, params: Any = None) -> Any:
        cursor = self.connection.cursor()
        try:
            cursor.execute(query, params)
            return cursor.fetchone()
        finally:
            cursor.close()

    def _fetchall(self, query: str, params: Any = None) -> list[Any]:
        cursor = self.connection.cursor()
        try:
            cursor.execute(query, params)
            return list(cursor.fetchall())
        finally:
            cursor.close()


def run_migration_command(
    action: Literal["apply", "check"],
    *,
    dsn: str,
    applied_by: str,
) -> int:
    """Open the migration-owner connection and execute one CLI action."""

    if not dsn.strip():
        raise MigrationStateError("PostgreSQL database URL is required")
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - optional runtime dependency.
        raise MigrationStateError(
            "psycopg is required to run PostgreSQL migrations"
        ) from exc
    with psycopg.connect(
        dsn,
        autocommit=False,
        application_name="sleepagent-migrate",
    ) as connection:
        assert_migration_owner_capability(connection)
        runner = PostgresMigrationRunner(connection, applied_by=applied_by)
        version = runner.apply() if action == "apply" else runner.check()
        if action == "apply":
            bootstrap_test_database_roles_from_environment(connection)
        return version


def assert_migration_owner_capability(connection: ConnectionLike) -> None:
    """Require the sole DDL owner to bypass the FORCE-RLS application policy."""

    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT rolbypassrls OR rolsuper "
            "FROM pg_catalog.pg_roles WHERE rolname = current_user"
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
        connection.rollback()
    if row is None or row[0] is not True:
        raise MigrationStateError(
            "migration owner must be the only deployment role with BYPASSRLS"
        )


def bootstrap_test_database_roles_from_environment(
    connection: ConnectionLike,
) -> bool:
    """Provision the two bounded compose roles only in the explicit test mode."""

    values = {
        BOOTSTRAP_API_ROLE_ENV: os.environ.get(BOOTSTRAP_API_ROLE_ENV, ""),
        BOOTSTRAP_API_PASSWORD_ENV: os.environ.get(
            BOOTSTRAP_API_PASSWORD_ENV, ""
        ),
        BOOTSTRAP_WORKER_ROLE_ENV: os.environ.get(
            BOOTSTRAP_WORKER_ROLE_ENV, ""
        ),
        BOOTSTRAP_WORKER_PASSWORD_ENV: os.environ.get(
            BOOTSTRAP_WORKER_PASSWORD_ENV, ""
        ),
    }
    configured = {name: value.strip() for name, value in values.items() if value}
    if not configured:
        return False
    if len(configured) != len(values):
        missing = sorted(set(values).difference(configured))
        raise MigrationStateError(
            f"test database role bootstrap is incomplete; missing {missing!r}"
        )
    if os.environ.get("SLEEPAGENT_BACKEND_DEPLOYMENT_MODE") != "test":
        raise MigrationStateError(
            "database role bootstrap is allowed only for deployment_mode=test"
        )
    bootstrap_test_database_roles(
        connection,
        api_role=configured[BOOTSTRAP_API_ROLE_ENV],
        api_password=configured[BOOTSTRAP_API_PASSWORD_ENV],
        worker_role=configured[BOOTSTRAP_WORKER_ROLE_ENV],
        worker_password=configured[BOOTSTRAP_WORKER_PASSWORD_ENV],
    )
    return True


def bootstrap_test_database_roles(
    connection: ConnectionLike,
    *,
    api_role: str,
    api_password: str,
    worker_role: str,
    worker_password: str,
) -> None:
    """Create/update non-owner API and Worker logins and least-privilege grants."""

    if not api_role or not worker_role or api_role == worker_role:
        raise MigrationStateError("test API and Worker roles must be distinct")
    if not api_password or not worker_password:
        raise MigrationStateError("test database role passwords are required")
    try:
        from psycopg import sql
    except ImportError as exc:  # pragma: no cover - production dependency.
        raise MigrationStateError("psycopg SQL composition support is required") from exc

    connection.rollback()
    try:
        for role_name, password in (
            (api_role, api_password),
            (worker_role, worker_password),
        ):
            exists = _connection_fetchone(
                connection,
                "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s",
                (role_name,),
            )
            role_identifier = sql.Identifier(role_name)
            attributes = sql.SQL(
                "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                "NOBYPASSRLS PASSWORD {}"
            ).format(sql.Literal(password))
            if exists is None:
                _connection_execute(
                    connection,
                    sql.SQL("CREATE ROLE {} WITH ").format(role_identifier)
                    + attributes,
                )
            else:
                _connection_execute(
                    connection,
                    sql.SQL("ALTER ROLE {} WITH ").format(role_identifier)
                    + attributes,
                )

        database_name = _connection_fetchone(
            connection, "SELECT current_database()"
        )[0]
        for role_name in (api_role, worker_role):
            _connection_execute(
                connection,
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(str(database_name)), sql.Identifier(role_name)
                ),
            )
            _connection_execute(
                connection,
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(role_name)
                ),
            )

        _connection_execute(
            connection,
            "INSERT INTO public.backend_service_principals ("
            "principal_id, database_role_name, principal_kind, status, "
            "credential_generation, metadata_json) VALUES "
            "(%s, %s, 'bff', 'active', 1, "
            "'{\"test_bootstrap\":true}'::jsonb), "
            "(%s, %s, 'worker', 'active', 1, "
            "'{\"test_bootstrap\":true}'::jsonb) "
            "ON CONFLICT (principal_id) DO UPDATE SET "
            "database_role_name = EXCLUDED.database_role_name, "
            "principal_kind = EXCLUDED.principal_kind, status = 'active', "
            "updated_at = clock_timestamp()",
            (
                os.environ.get(
                    "SLEEPAGENT_BOOTSTRAP_API_SERVICE_PRINCIPAL_ID",
                    "sleepagent-api-test",
                ),
                api_role,
                os.environ.get(
                    "SLEEPAGENT_BOOTSTRAP_WORKER_SERVICE_PRINCIPAL_ID",
                    "sleepagent-worker-test",
                ),
                worker_role,
            ),
        )
        api_read_tables = (
            "radar_agent_schema_migrations_v2",
            "backend_namespaces",
            "backend_namespace_generations",
            "backend_replay_runs",
            "backend_replay_arms",
            "backend_service_principals",
            "backend_actors",
            "backend_subjects",
            "backend_actor_subject_bindings",
            "backend_principal_grants",
            "backend_subject_epochs",
            "backend_command_receipts",
            "backend_monitoring_snapshots_v2",
            "sleep_domain_raw_inbox",
            "sleep_domain_normalization_work",
            "sleep_domain_operations",
            "sleep_domain_night_episodes",
            "sleep_domain_night_episode_revisions",
            "sleep_domain_current_quality",
            "sleep_domain_current_risk",
            "sleep_domain_analysis_role_views",
            "backend_pending_handles",
            "backend_replay_scenario_clocks",
            "backend_demo_seed_allowlist",
            "backend_demo_traces",
            "sleep_domain_domain_outbox",
        )
        api_insert_tables = (
            "backend_authorization_audit",
            "backend_command_receipts",
            "sleep_domain_operations",
            "sleep_domain_raw_inbox",
            "sleep_domain_normalization_work",
            "sleep_domain_domain_outbox",
            "backend_demo_traces",
        )
        worker_tables = (
            "radar_agent_schema_migrations_v2",
            "backend_namespaces",
            "backend_namespace_generations",
            "backend_replay_runs",
            "backend_replay_arms",
            "backend_service_principals",
            "backend_subjects",
            "backend_principal_grants",
            "backend_subject_epochs",
            "sleep_domain_raw_inbox",
            "sleep_domain_normalization_work",
            "sleep_domain_processing_receipts",
            "sleep_domain_adapter_candidates",
            "sleep_domain_canonical_observations",
            "backend_monitoring_snapshots_v2",
            "backend_episode_date_reconciliation",
            "sleep_domain_night_episodes",
            "sleep_domain_night_episode_revisions",
            "sleep_domain_quality_assessments",
            "sleep_domain_current_quality",
            "sleep_domain_risk_assessments",
            "sleep_domain_current_risk",
            "sleep_domain_vendor_alert_instances",
            "sleep_domain_alert_correlation_receipts",
            "sleep_domain_fast_path_signal_receipts",
            "sleep_domain_fast_path_signal_projections",
            "sleep_domain_analysis_revisions",
            "sleep_domain_analysis_role_views",
            "sleep_domain_operations",
            "sleep_domain_domain_outbox",
            "backend_invocations",
            "backend_invocation_journal",
            "backend_operation_heartbeats",
            "backend_operation_checkpoints",
            "backend_delivery_intents",
            "backend_delivery_journal",
            "backend_consumer_inbox",
            "backend_consumer_checkpoints",
            "backend_product_attempts",
            "backend_pending_handles",
            "backend_replay_scenario_clocks",
            "backend_demo_seed_allowlist",
            "backend_demo_traces",
            "backend_retention_classes",
            "backend_retention_deks",
            "backend_retention_bindings",
            "backend_retention_jobs",
            "backend_shred_receipts",
            "backend_retention_events",
        )
        _grant_tables(connection, sql, "SELECT", api_read_tables, api_role)
        _grant_tables(connection, sql, "INSERT", api_insert_tables, api_role)
        _grant_tables(connection, sql, "SELECT", worker_tables, worker_role)
        worker_insert_tables = (
            "sleep_domain_processing_receipts",
            "sleep_domain_adapter_candidates",
            "sleep_domain_canonical_observations",
            "backend_monitoring_snapshots_v2",
            "backend_episode_date_reconciliation",
            "sleep_domain_night_episodes",
            "sleep_domain_night_episode_revisions",
            "sleep_domain_quality_assessments",
            "sleep_domain_current_quality",
            "sleep_domain_risk_assessments",
            "sleep_domain_current_risk",
            "sleep_domain_vendor_alert_instances",
            "sleep_domain_alert_correlation_receipts",
            "sleep_domain_fast_path_signal_receipts",
            "sleep_domain_fast_path_signal_projections",
            "sleep_domain_analysis_revisions",
            "sleep_domain_analysis_role_views",
            "sleep_domain_operations",
            "sleep_domain_domain_outbox",
            "backend_invocations",
            "backend_invocation_journal",
            "backend_operation_heartbeats",
            "backend_operation_checkpoints",
            "backend_delivery_intents",
            "backend_delivery_journal",
            "backend_consumer_inbox",
            "backend_consumer_checkpoints",
            "backend_product_attempts",
            "backend_pending_handles",
            "backend_demo_traces",
            "backend_retention_bindings",
            "backend_retention_jobs",
            "backend_shred_receipts",
            "backend_retention_events",
        )
        worker_update_tables = (
            "sleep_domain_normalization_work",
            "backend_monitoring_snapshots_v2",
            "sleep_domain_night_episodes",
            "sleep_domain_current_quality",
            "sleep_domain_current_risk",
            "sleep_domain_vendor_alert_instances",
            "sleep_domain_fast_path_signal_projections",
            "sleep_domain_analysis_revisions",
            "sleep_domain_analysis_role_views",
            "sleep_domain_operations",
            "backend_invocations",
            "backend_consumer_inbox",
            "backend_consumer_checkpoints",
            "backend_product_attempts",
            "backend_pending_handles",
            "backend_replay_scenario_clocks",
            "backend_retention_deks",
            "backend_retention_bindings",
        )
        _grant_tables(
            connection, sql, "INSERT", worker_insert_tables, worker_role
        )
        _grant_tables(
            connection,
            sql,
            "UPDATE",
            worker_update_tables,
            worker_role,
        )
        _connection_execute(
            connection,
            sql.SQL(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}"
            ).format(sql.Identifier(api_role)),
        )
        _connection_execute(
            connection,
            sql.SQL(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}"
            ).format(sql.Identifier(worker_role)),
        )

        api_functions = (
            "sleepagent_resolve_actor_authority(text,text,text,text)",
            "sleepagent_consume_actor_assertion(text,text,text,timestamptz)",
            "sleepagent_consume_pending_handle(text,bigint,bigint,text,text,text)",
        )
        worker_functions = (
            "sleepagent_claim_normalization_work(text,integer)",
            "sleepagent_heartbeat_normalization_work(text,bigint,text,integer)",
            "sleepagent_finalize_normalization_work(text,bigint,text,text,timestamptz,text)",
            "sleepagent_claim_operation(text,text,integer)",
            "sleepagent_heartbeat_operation(text,bigint,text,integer)",
            "sleepagent_finalize_operation(text,bigint,bigint,text,text,text,timestamptz)",
            "sleepagent_operation_fence_allows(text,bigint,text)",
            "sleepagent_claim_delivery(text,text,integer)",
            "sleepagent_heartbeat_delivery(text,bigint,text,integer)",
            "sleepagent_mark_delivery_dispatching(text,bigint,text)",
            "sleepagent_finalize_delivery(text,bigint,text,text,timestamptz)",
            "sleepagent_claim_retention_job(text,integer)",
            "sleepagent_heartbeat_retention_job(text,bigint,text,integer)",
            "sleepagent_finalize_retention_job(text,bigint,text,text,timestamptz)",
        )
        for signature in api_functions:
            _grant_function(connection, sql, signature, api_role)
        for signature in worker_functions:
            _grant_function(connection, sql, signature, worker_role)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _grant_tables(
    connection: ConnectionLike,
    sql_module: Any,
    privileges: str,
    table_names: Sequence[str],
    role_name: str,
) -> None:
    tables = sql_module.SQL(", ").join(
        sql_module.Identifier("public", name) for name in table_names
    )
    _connection_execute(
        connection,
        sql_module.SQL("GRANT {} ON TABLE {} TO {}").format(
            sql_module.SQL(privileges), tables, sql_module.Identifier(role_name)
        ),
    )


def _grant_function(
    connection: ConnectionLike,
    sql_module: Any,
    signature: str,
    role_name: str,
) -> None:
    # Function signatures are release-owned constants, never environment input.
    _connection_execute(
        connection,
        sql_module.SQL("GRANT EXECUTE ON FUNCTION public.{} TO {}").format(
            sql_module.SQL(signature), sql_module.Identifier(role_name)
        ),
    )


def _connection_execute(
    connection: ConnectionLike,
    query: Any,
    params: Any = None,
) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute(query, params)
        return int(cursor.rowcount)
    finally:
        cursor.close()


def _connection_fetchone(
    connection: ConnectionLike,
    query: Any,
    params: Any = None,
) -> Any:
    cursor = connection.cursor()
    try:
        cursor.execute(query, params)
        return cursor.fetchone()
    finally:
        cursor.close()


def _database_error_code(exc: BaseException) -> str:
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate:
        return f"sqlstate:{sqlstate}"
    return f"python:{type(exc).__name__}"[:128]


def _default_applied_by() -> str:
    configured = os.environ.get("SLEEPAGENT_MIGRATION_APPLIED_BY", "").strip()
    if configured:
        return configured
    return f"{getpass.getuser()}@{socket.gethostname()}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sleepagent-migrate",
        description="Apply or verify the immutable SleepAgent PostgreSQL schema",
    )
    parser.add_argument("action", choices=("apply", "check"))
    parser.add_argument(
        "--database-url-env",
        default=DEFAULT_DATABASE_URL_ENV,
        help=(
            "environment variable containing the migration-owner DSN "
            f"(default: {DEFAULT_DATABASE_URL_ENV})"
        ),
    )
    parser.add_argument("--applied-by", default=_default_applied_by())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dsn = os.environ.get(args.database_url_env, "")
    try:
        version = run_migration_command(
            args.action,
            dsn=dsn,
            applied_by=args.applied_by,
        )
    except MigrationError as exc:
        print(f"migration {args.action} refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # Keep credentials and SQL bodies out of logs.
        print(
            f"migration {args.action} failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 3
    print(f"migration {args.action} complete: schema_version={version:03d}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "BOOTSTRAP_API_PASSWORD_ENV",
    "BOOTSTRAP_API_ROLE_ENV",
    "BOOTSTRAP_WORKER_PASSWORD_ENV",
    "BOOTSTRAP_WORKER_ROLE_ENV",
    "DEFAULT_DATABASE_URL_ENV",
    "LATEST_SCHEMA_VERSION",
    "LEDGER_BOOTSTRAP_VERSION",
    "LEGACY_LAST_VERSION",
    "MIGRATION_ADVISORY_LOCK_ID",
    "MigrationError",
    "MigrationExecutionError",
    "MigrationFile",
    "MigrationLedgerRow",
    "MigrationReleaseError",
    "MigrationStateError",
    "PostgresMigrationRunner",
    "RELEASE_021_SHA256",
    "assert_migration_owner_capability",
    "bootstrap_test_database_roles",
    "bootstrap_test_database_roles_from_environment",
    "build_parser",
    "discover_migrations",
    "main",
    "run_migration_command",
    "validate_ledger_rows",
]
