# 本模块负责 PostgreSQL 持久化边界与完整性校验，不提供内存或 SQLite 旁路。
"""Fail-closed PostgreSQL installer for the manifest-pinned schema release."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import socket
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal, Protocol, Sequence, cast

from sleepagent.persistence.migrations import (
    COMMAND_AUTHORITY_FUNCTION,
    DELIVERY_AUTHORITY_FUNCTION,
    LATEST_SCHEMA_VERSION,
    MIGRATION_MANIFEST_SHA256,
    MIGRATION_RELEASE,
    MigrationReleaseManifest,
    SCENARIO_CLOCK_AUTHORITY_FUNCTION,
    split_sql_statements,
)


BASELINE_SCHEMA_SHA256 = (
    "c62168ddb802f975dd2afdeb5ca7854fed380695c7295061f72133af099a023f"
)
MIGRATION_ADVISORY_LOCK_ID = 7_216_457_676_974_471_169
DEFAULT_DATABASE_URL_ENV = "SLEEPAGENT_BACKEND_DATABASE_DSN"
BOOTSTRAP_API_ROLE_ENV = "SLEEPAGENT_BOOTSTRAP_API_DATABASE_ROLE"
BOOTSTRAP_API_PASSWORD_ENV = "SLEEPAGENT_BOOTSTRAP_API_DATABASE_PASSWORD"
BOOTSTRAP_DEMO_ROLE_ENV = "SLEEPAGENT_BOOTSTRAP_DEMO_DATABASE_ROLE"
BOOTSTRAP_DEMO_PASSWORD_ENV = "SLEEPAGENT_BOOTSTRAP_DEMO_DATABASE_PASSWORD"
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
    *,
    manifest: MigrationReleaseManifest = MIGRATION_RELEASE,
) -> tuple[MigrationFile, ...]:
    """Load and validate the exact, contiguous SQL migration release."""

    expected_names = {entry.filename for entry in manifest.migrations}
    discovered_paths = tuple(
        sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    )
    actual_names = {path.name for path in discovered_paths}
    if actual_names != expected_names:
        missing = sorted(expected_names.difference(actual_names))
        unknown = sorted(actual_names.difference(expected_names))
        raise MigrationReleaseError(
            "migration files do not match the release manifest; "
            f"missing={missing!r}, unknown={unknown!r}"
        )
    expected_by_version = {
        entry.version: entry for entry in manifest.migrations
    }
    migrations: list[MigrationFile] = []
    for path in discovered_paths:
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
        if marker is None:
            raise MigrationReleaseError(
                f"migration {path.name} must declare transactional strategy"
            )
        transactional = marker.group("value") == "true"
        expected = expected_by_version.get(version)
        if expected is None or expected.filename != path.name:
            raise MigrationReleaseError(
                f"migration {path.name} is not declared by the release manifest"
            )
        digest = hashlib.sha256(raw_sql).hexdigest()
        if digest != expected.sha256:
            raise MigrationReleaseError(
                f"migration {path.name} does not match its manifest checksum"
            )
        if transactional is not expected.transactional:
            raise MigrationReleaseError(
                f"migration {path.name} transaction strategy drift detected"
            )
        migrations.append(
            MigrationFile(
                version=version,
                name=path.stem,
                path=path,
                sql=sql,
                sql_sha256=digest,
                transactional=transactional,
            )
        )

    expected_versions = list(range(1, manifest.target_version + 1))
    actual_versions = [migration.version for migration in migrations]
    if actual_versions != expected_versions:
        raise MigrationReleaseError(
            "migration release must be contiguous and ordered; "
            f"found {actual_versions!r}"
        )
    if migrations[0].sql_sha256 != BASELINE_SCHEMA_SHA256:
        raise MigrationReleaseError("001 immutable baseline checksum drift")
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
        expected_status = "applied"
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
    if not versions:
        if require_complete:
            raise MigrationStateError("canonical schema ledger is empty")
        return
    if require_complete and versions != sorted(expected):
        missing = sorted(set(expected).difference(versions))
        raise MigrationStateError(
            f"schema is not at release target; missing {missing!r}"
        )


class PostgresMigrationRunner:
    """Apply, check, or report one immutable migration release."""

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
        if [item.version for item in self.migrations] != list(
            range(1, LATEST_SCHEMA_VERSION + 1)
        ):
            raise MigrationReleaseError(
                "runner requires the complete manifest-pinned release"
            )
        if self.migrations[0].sql_sha256 != BASELINE_SCHEMA_SHA256:
            raise MigrationReleaseError("runner received an untrusted baseline")
        expected = {
            entry.version: entry for entry in MIGRATION_RELEASE.migrations
        }
        for migration in self.migrations:
            release_entry = expected.get(migration.version)
            if (
                release_entry is None
                or migration.path.name != release_entry.filename
                or migration.sql_sha256 != release_entry.sha256
                or migration.transactional is not release_entry.transactional
            ):
                raise MigrationReleaseError(
                    f"runner received untrusted migration {migration.version:03d}"
                )

    def apply(self) -> int:
        """Apply all missing migrations and return the resulting version."""

        with self._advisory_lock():
            if not self._table_exists("sleepagent_schema_migrations"):
                if self._database_contains_sleepagent_schema():
                    raise MigrationStateError(
                        "SleepAgent tables exist without the canonical baseline ledger"
                    )
                self._apply_baseline(self.migrations[0])
            rows = self._load_v2_rows()
            validate_ledger_rows(
                self.migrations,
                rows,
                require_complete=False,
            )
            applied_versions = {row.version for row in rows}
            for migration in self.migrations:
                if migration.version in applied_versions:
                    continue
                if migration.version == 1:
                    raise MigrationStateError(
                        "immutable baseline ledger is missing version 001"
                    )
                self._apply_additive(migration)
                rows = self._load_v2_rows()
                validate_ledger_rows(
                    self.migrations,
                    rows,
                    require_complete=False,
                )
                applied_versions = {row.version for row in rows}
            validate_ledger_rows(
                self.migrations,
                rows,
                require_complete=True,
            )
            return rows[-1].version

    def check(self) -> int:
        """Read-only verification of the exact release ledger."""

        with self._advisory_lock():
            if not self._table_exists("sleepagent_schema_migrations"):
                raise MigrationStateError("canonical schema ledger does not exist")
            rows = self._load_v2_rows()
            validate_ledger_rows(
                self.migrations,
                rows,
                require_complete=True,
            )
            return rows[-1].version

    def status(self) -> int:
        """Validate the installed prefix and return its current version."""

        with self._advisory_lock():
            if not self._table_exists("sleepagent_schema_migrations"):
                return 0
            rows = self._load_v2_rows()
            validate_ledger_rows(
                self.migrations,
                rows,
                require_complete=False,
            )
            return 0 if not rows else rows[-1].version

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

    def _apply_baseline(self, migration: MigrationFile) -> None:
        """Install schema and ledger identity in one transaction."""

        self.connection.rollback()
        try:
            for statement in split_sql_statements(migration.sql):
                self._execute(statement)
            self._execute(
                "INSERT INTO sleepagent_schema_migrations ("
                "version, migration_name, sql_sha256, status, transactional, "
                "started_at, finished_at, applied_by, error_code) VALUES "
                "(%s, %s, %s, 'applied', %s, clock_timestamp(), "
                "clock_timestamp(), %s, NULL)",
                (
                    migration.version,
                    migration.name,
                    migration.sql_sha256,
                    migration.transactional,
                    self.applied_by,
                ),
            )
            self.connection.commit()
        except Exception as exc:
            self.connection.rollback()
            raise MigrationExecutionError(
                "canonical schema baseline installation failed"
            ) from exc

    def _apply_additive(self, migration: MigrationFile) -> None:
        """Apply one transactional additive migration atomically."""

        if not migration.transactional:
            raise MigrationStateError(
                f"migration {migration.version:03d} is non-transactional; "
                "operator-run recovery is required"
            )
        self.connection.rollback()
        try:
            self._execute(
                "INSERT INTO sleepagent_schema_migrations ("
                "version, migration_name, sql_sha256, status, transactional, "
                "started_at, finished_at, applied_by, error_code) VALUES "
                "(%s, %s, %s, 'started', %s, clock_timestamp(), "
                "NULL, %s, NULL)",
                (
                    migration.version,
                    migration.name,
                    migration.sql_sha256,
                    migration.transactional,
                    self.applied_by,
                ),
            )
            for statement in split_sql_statements(migration.sql):
                self._execute(statement)
            self._execute(
                "UPDATE sleepagent_schema_migrations "
                "SET status = 'applied', finished_at = clock_timestamp() "
                "WHERE version = %s AND status = 'started'",
                (migration.version,),
            )
            self.connection.commit()
        except Exception as exc:
            self.connection.rollback()
            raise MigrationExecutionError(
                f"migration {migration.version:03d} failed atomically"
            ) from exc

    def _load_v2_rows(self) -> list[MigrationLedgerRow]:
        rows = self._fetchall(
            "SELECT version, migration_name, sql_sha256, status, "
            "transactional, started_at, finished_at, applied_by, error_code "
            "FROM sleepagent_schema_migrations ORDER BY version"
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
            "sleepagent_schema_migrations",
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
    action: Literal["apply", "check", "status"],
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
        typed_connection = cast(ConnectionLike, connection)
        assert_migration_owner_capability(typed_connection)
        runner = PostgresMigrationRunner(typed_connection, applied_by=applied_by)
        if action == "apply":
            version = runner.apply()
        elif action == "check":
            version = runner.check()
        else:
            version = runner.status()
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
        BOOTSTRAP_DEMO_ROLE_ENV: os.environ.get(BOOTSTRAP_DEMO_ROLE_ENV, ""),
        BOOTSTRAP_DEMO_PASSWORD_ENV: os.environ.get(
            BOOTSTRAP_DEMO_PASSWORD_ENV, ""
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
        demo_role=configured[BOOTSTRAP_DEMO_ROLE_ENV],
        demo_password=configured[BOOTSTRAP_DEMO_PASSWORD_ENV],
        worker_role=configured[BOOTSTRAP_WORKER_ROLE_ENV],
        worker_password=configured[BOOTSTRAP_WORKER_PASSWORD_ENV],
    )
    return True


def bootstrap_test_database_roles(
    connection: ConnectionLike,
    *,
    api_role: str,
    api_password: str,
    demo_role: str,
    demo_password: str,
    worker_role: str,
    worker_password: str,
) -> None:
    """Create/update non-owner API and Worker logins and least-privilege grants."""

    role_names = (api_role, demo_role, worker_role)
    if any(not role for role in role_names) or len(set(role_names)) != 3:
        raise MigrationStateError(
            "test BFF, Demo, and Worker roles must be distinct"
        )
    if not api_password or not demo_password or not worker_password:
        raise MigrationStateError("test database role passwords are required")
    try:
        from psycopg import sql
    except ImportError as exc:  # pragma: no cover - production dependency.
        raise MigrationStateError("psycopg SQL composition support is required") from exc

    connection.rollback()
    try:
        for role_name, password in (
            (api_role, api_password),
            (demo_role, demo_password),
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
        for role_name in role_names:
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
            "(%s, %s, 'demo_controller', 'active', 1, "
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
                    "SLEEPAGENT_BOOTSTRAP_DEMO_SERVICE_PRINCIPAL_ID",
                    "sleepagent-demo-test",
                ),
                demo_role,
                os.environ.get(
                    "SLEEPAGENT_BOOTSTRAP_WORKER_SERVICE_PRINCIPAL_ID",
                    "sleepagent-worker-test",
                ),
                worker_role,
            ),
        )
        api_read_tables = (
            "sleepagent_schema_migrations",
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
            "sleep_domain_device_identities",
            "sleep_domain_device_bindings",
            "sleep_domain_device_binding_audit",
            "backend_acquisition_schedules",
            "backend_acquisition_schedule_fires",
            "sleep_domain_night_finalizations",
            "sleep_domain_night_finalization_revisions",
            "backend_command_receipts",
            "backend_monitoring_snapshots_v2",
            "sleep_domain_raw_inbox",
            "sleep_domain_normalization_work",
            "sleep_domain_operations",
            "sleep_domain_night_episodes",
            "sleep_domain_night_episode_revisions",
            "sleep_domain_current_quality",
            "sleep_domain_current_risk",
            "sleep_domain_analysis_revisions",
            "sleep_domain_analysis_role_views",
            "backend_product_attempts",
            "backend_invocations",
            "backend_invocation_journal",
            "backend_pending_handles",
            "backend_habit_question_selections_v2",
            "backend_habit_profile_revisions_v2",
            "backend_governed_memory_revisions_v2",
            "backend_memory_read_receipts_v2",
            "backend_monitoring_transition_receipts",
            "backend_human_facts",
            "backend_product_interactions",
            "backend_product_interaction_revisions",
            "backend_human_decisions_v2",
            "backend_care_actions_v2",
            "backend_care_action_proposals_v3",
            "backend_care_action_decisions_v3",
            "backend_approval_grants_v3",
            "backend_care_plans_v1",
            "backend_care_execution_states_v1",
            "backend_care_execution_events_v1",
            "backend_care_outcome_evaluations_v1",
            "backend_care_outcomes_v1",
            "backend_personalization_effect_receipts_v1",
            "sleep_domain_care_followups",
            "backend_reanalysis_links",
            "backend_replay_scenario_clocks",
            "backend_demo_seed_allowlist",
            "backend_demo_traces",
            "sleep_domain_domain_outbox",
        )
        api_insert_tables = (
            "backend_authorization_audit",
            "backend_command_receipts",
            "sleep_domain_operations",
            "backend_acquisition_schedules",
            "sleep_domain_raw_inbox",
            "sleep_domain_normalization_work",
            "sleep_domain_domain_outbox",
            "backend_demo_traces",
            "backend_pending_handles",
            "backend_habit_question_selections_v2",
            "backend_habit_profile_revisions_v2",
            "backend_governed_memory_revisions_v2",
            "backend_memory_read_receipts_v2",
        )
        worker_tables = (
            "sleepagent_schema_migrations",
            "backend_namespaces",
            "backend_namespace_generations",
            "backend_replay_runs",
            "backend_replay_arms",
            "backend_service_principals",
            "backend_subjects",
            "backend_principal_grants",
            "backend_subject_epochs",
            "backend_command_receipts",
            "sleep_domain_raw_inbox",
            "sleep_domain_normalization_work",
            "sleep_domain_provider_accounts",
            "sleep_domain_device_bindings",
            "sleep_domain_device_identities",
            "sleep_domain_device_binding_audit",
            "backend_acquisition_schedules",
            "backend_acquisition_schedule_fires",
            "sleep_domain_night_finalizations",
            "sleep_domain_night_finalization_revisions",
            "sleep_domain_processing_receipts",
            "sleep_domain_quarantine",
            "sleep_domain_adapter_candidates",
            "sleep_domain_canonical_observations",
            "sleep_domain_observation_semantics_v2",
            "sleep_domain_source_reports",
            "sleep_domain_pull_checkpoints",
            "sleep_domain_observation_fact_values",
            "sleep_domain_observation_acquisitions",
            "sleep_domain_observation_conflicts",
            "backend_monitoring_snapshots_v2",
            "backend_episode_date_reconciliation",
            "sleep_domain_night_episodes",
            "sleep_domain_night_episode_revisions",
            "sleep_domain_episode_observation_memberships",
            "sleep_domain_pending_episode_associations",
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
            "backend_induction_manifests_v2",
            "backend_personalization_profiles_v2",
            "backend_personalization_profile_revisions_v2",
            "backend_induction_receipts_v2",
            "backend_replay_delivery_effects_v2",
            "backend_delivery_reconciliation_receipts_v2",
            "backend_product_attempts",
            "backend_pending_handles",
            "backend_habit_question_selections_v2",
            "backend_habit_profile_revisions_v2",
            "backend_governed_memory_revisions_v2",
            "backend_memory_read_receipts_v2",
            "backend_monitoring_transition_receipts",
            "backend_human_facts",
            "backend_product_interactions",
            "backend_product_interaction_revisions",
            "backend_human_decisions_v2",
            "backend_care_actions_v2",
            "backend_care_action_proposals_v3",
            "backend_care_action_decisions_v3",
            "backend_approval_grants_v3",
            "backend_care_plans_v1",
            "backend_care_execution_states_v1",
            "backend_care_execution_events_v1",
            "backend_care_outcome_evaluations_v1",
            "backend_care_outcomes_v1",
            "backend_personalization_effect_receipts_v1",
            "sleep_domain_care_followups",
            "sleep_domain_care_followup_transition_receipts",
            "backend_reanalysis_links",
            "backend_replay_scenario_clocks",
            "backend_replay_staged_facts",
            "backend_demo_advance_receipts",
            "backend_demo_seed_allowlist",
            "backend_demo_traces",
            "backend_demo_journeys",
            "backend_demo_journey_checkpoints",
            "backend_demo_journey_events",
            "backend_demo_journey_receipts",
            "backend_replay_ingress_manifests",
            "backend_replay_ingress_batches",
            "backend_retention_classes",
            "backend_retention_deks",
            "backend_retention_bindings",
            "backend_retention_jobs",
            "backend_shred_receipts",
            "backend_retention_events",
            "backend_demo_resets_v2",
            "backend_demo_reset_key_events_v2",
            "backend_demo_reset_key_receipts_v2",
            "backend_demo_reset_receipts_v2",
        )
        _grant_tables(connection, sql, "SELECT", api_read_tables, api_role)
        _grant_tables(connection, sql, "INSERT", api_insert_tables, api_role)
        _grant_tables(
            connection,
            sql,
            "UPDATE",
            ("backend_acquisition_schedules",),
            api_role,
        )
        # Product report reservation locks the exact marked-current episode
        # before deriving its durable semantic key. PostgreSQL row-locking
        # SELECTs require UPDATE on at least one column, so expose only the
        # immutable RLS scope anchor rather than mutable episode state.
        _connection_execute(
            connection,
            sql.SQL("GRANT UPDATE ({}) ON TABLE {} TO {}").format(
                sql.Identifier("namespace_id"),
                sql.Identifier("public", "sleep_domain_night_episodes"),
                sql.Identifier(api_role),
            ),
        )
        _grant_tables(
            connection,
            sql,
            "SELECT",
            ("sleepagent_schema_migrations",),
            demo_role,
        )
        _grant_tables(connection, sql, "SELECT", worker_tables, worker_role)
        # PostgreSQL row-locking SELECTs require UPDATE on at least one column.
        # The worker may lock the subject epoch row through its immutable RLS
        # scope anchor, but it receives no write privilege on governance state.
        _connection_execute(
            connection,
            sql.SQL("GRANT UPDATE ({}) ON TABLE {} TO {}").format(
                sql.Identifier("namespace_id"),
                sql.Identifier("public", "backend_subject_epochs"),
                sql.Identifier(worker_role),
            ),
        )
        # Delivery outcome-unknown finalization locks the exact intent before
        # deriving its sole reconciliation operation.  Keep that row-lock
        # capability column-scoped: state mutation continues to go through the
        # fenced SECURITY DEFINER transition functions below.
        _connection_execute(
            connection,
            sql.SQL("GRANT UPDATE ({}) ON TABLE {} TO {}").format(
                sql.Identifier("namespace_id"),
                sql.Identifier("public", "backend_delivery_intents"),
                sql.Identifier(worker_role),
            ),
        )
        # Induction locks its immutable manifest together with the mutable
        # operation and Product attempt, so the committed source cannot drift
        # during profile projection.  The worker only needs the row-lock
        # capability, not permission to rewrite manifest contents.
        _connection_execute(
            connection,
            sql.SQL("GRANT UPDATE ({}) ON TABLE {} TO {}").format(
                sql.Identifier("namespace_id"),
                sql.Identifier("public", "backend_induction_manifests_v2"),
                sql.Identifier(worker_role),
            ),
        )
        # Product final gates share-lock the exact immutable episode revision
        # alongside mutable current Episode/Quality/Risk rows.  Permit only a
        # row-lock through its immutable RLS scope anchor.
        _connection_execute(
            connection,
            sql.SQL("GRANT UPDATE ({}) ON TABLE {} TO {}").format(
                sql.Identifier("namespace_id"),
                sql.Identifier(
                    "public", "sleep_domain_night_episode_revisions"
                ),
                sql.Identifier(worker_role),
            ),
        )
        _connection_execute(
            connection,
            sql.SQL(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}"
            ).format(sql.Identifier(demo_role)),
        )
        worker_insert_tables = (
            "backend_authorization_audit",
            "backend_acquisition_schedule_fires",
            "sleep_domain_night_finalizations",
            "sleep_domain_night_finalization_revisions",
            "sleep_domain_raw_inbox",
            "sleep_domain_normalization_work",
            "sleep_domain_processing_receipts",
            "sleep_domain_quarantine",
            "sleep_domain_adapter_candidates",
            "sleep_domain_canonical_observations",
            "sleep_domain_observation_semantics_v2",
            "sleep_domain_source_reports",
            "sleep_domain_pull_checkpoints",
            "sleep_domain_observation_fact_values",
            "sleep_domain_observation_acquisitions",
            "sleep_domain_observation_conflicts",
            "backend_monitoring_snapshots_v2",
            "backend_episode_date_reconciliation",
            "sleep_domain_night_episodes",
            "sleep_domain_night_episode_revisions",
            "sleep_domain_episode_observation_memberships",
            "sleep_domain_pending_episode_associations",
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
            "backend_care_action_proposals_v3",
            "backend_care_action_decisions_v3",
            "backend_invocations",
            "backend_invocation_journal",
            "backend_operation_heartbeats",
            "backend_operation_checkpoints",
            "backend_delivery_intents",
            "backend_delivery_journal",
            "backend_consumer_inbox",
            "backend_consumer_checkpoints",
            "backend_induction_manifests_v2",
            "backend_personalization_profiles_v2",
            "backend_personalization_profile_revisions_v2",
            "backend_induction_receipts_v2",
            "backend_replay_delivery_effects_v2",
            "backend_delivery_reconciliation_receipts_v2",
            "backend_product_attempts",
            "backend_memory_read_receipts_v2",
            "backend_care_outcomes_v1",
            "backend_personalization_effect_receipts_v1",
            "backend_pending_handles",
            "backend_monitoring_transition_receipts",
            "backend_human_facts",
            "backend_product_interactions",
            "backend_product_interaction_revisions",
            "backend_human_decisions_v2",
            "backend_care_actions_v2",
            "sleep_domain_care_followups",
            "sleep_domain_care_followup_transition_receipts",
            "backend_reanalysis_links",
            "backend_replay_scenario_clocks",
            "backend_replay_staged_facts",
            "backend_demo_advance_receipts",
            "backend_demo_traces",
            "backend_demo_journey_checkpoints",
            "backend_demo_journey_events",
            "backend_demo_journey_receipts",
            "backend_replay_ingress_manifests",
            "backend_replay_ingress_batches",
            "backend_retention_deks",
            "backend_retention_bindings",
            "backend_retention_jobs",
            "backend_shred_receipts",
            "backend_retention_events",
        )
        worker_update_tables = (
            "sleep_domain_normalization_work",
            "backend_acquisition_schedules",
            "backend_acquisition_schedule_fires",
            "sleep_domain_night_finalizations",
            "backend_monitoring_snapshots_v2",
            "backend_episode_date_reconciliation",
            "sleep_domain_night_episodes",
            "sleep_domain_current_quality",
            "sleep_domain_current_risk",
            "sleep_domain_vendor_alert_instances",
            "sleep_domain_fast_path_signal_projections",
            "sleep_domain_analysis_revisions",
            "sleep_domain_analysis_role_views",
            "sleep_domain_operations",
            "backend_care_action_proposals_v3",
            "backend_invocations",
            "backend_delivery_intents",
            "backend_consumer_inbox",
            "backend_consumer_checkpoints",
            "backend_personalization_profiles_v2",
            "backend_care_outcome_evaluations_v1",
            "backend_product_attempts",
            "backend_pending_handles",
            "backend_product_interactions",
            "backend_care_actions_v2",
            "sleep_domain_care_followups",
            "backend_replay_scenario_clocks",
            "backend_replay_staged_facts",
            "backend_demo_journeys",
            "backend_replay_ingress_batches",
            "backend_retention_deks",
            "backend_retention_bindings",
            "backend_retention_jobs",
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
        # Pull evidence is append-only.  Only the cursor/commit fence of the
        # exact v2 checkpoint may advance, and the Worker receives no UPDATE
        # privilege over its provider, subject, binding, surface, or overlap.
        _connection_execute(
            connection,
            sql.SQL("GRANT UPDATE ({}) ON TABLE {} TO {}").format(
                sql.SQL(", ").join(
                    map(
                        sql.Identifier,
                        (
                            "cursor_at",
                            "lateness_watermark_at",
                            "cas_version",
                            "updated_at",
                            "last_raw_ingress_record_id",
                            "last_normalization_work_id",
                            "last_canonical_observation_id",
                            "last_canonical_commit_at",
                        ),
                    )
                ),
                sql.Identifier("public", "sleep_domain_pull_checkpoints"),
                sql.Identifier(worker_role),
            ),
        )
        _connection_execute(
            connection,
            sql.SQL("GRANT UPDATE ({}) ON TABLE {} TO {}").format(
                sql.SQL(", ").join(
                    map(
                        sql.Identifier,
                        ("evidence_json", "consumed_at"),
                    )
                ),
                sql.Identifier(
                    "public", "backend_habit_question_selections_v2"
                ),
                sql.Identifier(api_role),
            ),
        )
        _connection_execute(
            connection,
            sql.SQL("GRANT UPDATE ({}) ON TABLE {} TO {}").format(
                sql.SQL(", ").join(
                    map(
                        sql.Identifier,
                        (
                            "status",
                            "consumed_at",
                            "consumed_by_command_receipt_id",
                            "cas_version",
                        ),
                    )
                ),
                sql.Identifier("public", "backend_pending_handles"),
                sql.Identifier(api_role),
            ),
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
            "sleepagent_attest_actor_verification_keys()",
            "sleepagent_resolve_actor_authority(text,text,text,text)",
            "sleepagent_consume_actor_assertion(text,text,text,timestamptz)",
            "sleepagent_internal_reconciliation_status(text)",
            "sleepagent_internal_operational_metrics()",
            "sleepagent_care_governance_operational_metrics_v3()",
            "sleepagent_care_execution_operational_metrics_v1()",
            "sleepagent_care_outcome_operational_metrics_v1()",
            "sleepagent_care_evaluation_operational_metrics_v1()",
            "sleepagent_decide_care_action_proposal_v3(text,bigint,text,text,text,text,text,timestamptz)",
            "sleepagent_revoke_care_approval_v3(text,bigint,text,text,text,text,timestamptz)",
            "sleepagent_execute_care_plan_v1(text,bigint,text,text,text,text,timestamptz)",
            "sleepagent_ingest_perceptor_push(text,bigint,text,text,text,text,text,text,timestamptz,timestamptz,text,text,bytea,text,text,integer,timestamptz,text,boolean,boolean,text,text,text,text,text,jsonb)",
            "sleepagent_ingest_perceptor_pull(text,bigint,text,text,text,text,text,text,text,timestamptz,timestamptz,date,timestamptz,timestamptz,text,text,text,bytea,text,text,integer,timestamptz,boolean,boolean,text,text,text,text,text,jsonb)",
            "sleepagent_plan_perceptor_history(text,bigint,text,text,integer,text,timestamptz,timestamptz)",
            "sleepagent_manage_device_binding(text,text,text,bigint,text,text,text,text,text,jsonb,text,text,timestamptz,text,text,text,jsonb,jsonb)",
        )
        worker_functions = (
            "sleepagent_ingest_perceptor_pull(text,bigint,text,text,text,text,text,text,text,timestamptz,timestamptz,date,timestamptz,timestamptz,text,text,text,bytea,text,text,integer,timestamptz,boolean,boolean,text,text,text,text,text,jsonb)",
            "sleepagent_plan_perceptor_history(text,bigint,text,text,integer,text,timestamptz,timestamptz)",
            "sleepagent_bootstrap_demo_journey(text,text)",
            "sleepagent_claim_demo_journey(text,integer)",
            "sleepagent_heartbeat_demo_journey(text,bigint,text,integer)",
            "sleepagent_wait_demo_journey(text,bigint,text,text,timestamptz)",
            "sleepagent_finalize_demo_journey_attempt(text,bigint,text,text,text,timestamptz)",
            "sleepagent_succeed_demo_journey(text,bigint,text,jsonb)",
            "sleepagent_claim_normalization_work(text,integer)",
            "sleepagent_heartbeat_normalization_work(text,bigint,text,integer)",
            "sleepagent_finalize_normalization_work(text,bigint,text,text,timestamptz,text)",
            "sleepagent_claim_operation(text,text,integer)",
            "sleepagent_fire_due_acquisition_schedules(text,integer,boolean)",
            "sleepagent_heartbeat_operation(text,bigint,text,integer)",
            "sleepagent_finalize_operation(text,bigint,bigint,text,text,text,timestamptz)",
            "sleepagent_operation_fence_allows(text,bigint,text)",
            f"{COMMAND_AUTHORITY_FUNCTION}(text,text,text)",
            f"{SCENARIO_CLOCK_AUTHORITY_FUNCTION}(text)",
            "sleepagent_claim_delivery(text,text,integer)",
            "sleepagent_heartbeat_delivery(text,bigint,text,integer)",
            "sleepagent_mark_delivery_dispatching(text,bigint,text)",
            f"{DELIVERY_AUTHORITY_FUNCTION}(text)",
            "sleepagent_finalize_delivery(text,bigint,text,text,timestamptz)",
            "sleepagent_claim_retention_job(text,integer)",
            "sleepagent_heartbeat_retention_job(text,bigint,text,integer)",
            "sleepagent_finalize_retention_job(text,bigint,text,text,timestamptz)",
            "sleepagent_prepare_demo_reset_key(text,bigint,text,text)",
            "sleepagent_commit_demo_reset_key(text,bigint,text,text,text,text)",
            "sleepagent_wait_demo_reset(text,bigint,bigint,text,timestamptz)",
            "sleepagent_complete_demo_reset(text,bigint,bigint,text,text)",
        )
        demo_functions = (
            "sleepagent_reserve_demo_journey(text,text,text,integer,text,text,text,text)",
            "sleepagent_reserve_demo_advance(text,text,integer,text,text,text,text)",
            "sleepagent_reserve_demo_reset(text,text,text,text,text,text,text)",
            "sleepagent_get_demo_operation(text)",
            "sleepagent_read_demo_clock()",
            "sleepagent_read_demo_trace(text,bigint,integer)",
            "sleepagent_read_demo_technical_trace(text)",
        )
        for signature in api_functions:
            _grant_function(connection, sql, signature, api_role)
        for signature in worker_functions:
            _grant_function(connection, sql, signature, worker_role)
        for signature in demo_functions:
            _grant_function(connection, sql, signature, demo_role)
        _bootstrap_replay_seed_allowlist(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _bootstrap_replay_seed_allowlist(connection: ConnectionLike) -> None:
    """Verify and pin the packaged facts-only replay seed for test profiles."""

    from sleepagent.config import BackendKeyError, BackendKeyProvider
    from sleepagent.config import DeploymentMode
    from sleepagent.simulation.generator import CanonicalReplayGenerator
    from sleepagent.simulation.replay_ingress import replay_external_fact_adapter
    from sleepagent.simulation.seed_registry import (
        load_replay_seed_registry,
        verify_packaged_seed,
    )

    actor_key_sha256 = os.environ.get(
        "SLEEPAGENT_BOOTSTRAP_ACTOR_VERIFICATION_KEY_SHA256",
        "",
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", actor_key_sha256):
        raise MigrationStateError(
            "test bootstrap requires the actor verification-key fingerprint"
        )
    try:
        loaded_actor_key_sha256 = BackendKeyProvider(
            DeploymentMode(
                os.environ.get(
                    "SLEEPAGENT_BACKEND_DEPLOYMENT_MODE",
                    "production",
                )
            )
        ).actor_verification_key(
            os.environ.get("SLEEPAGENT_BACKEND_SIGNING_KEY_REF", ""),
            key_id=os.environ.get(
                "SLEEPAGENT_BACKEND_ACTOR_ASSERTION_KEY_ID",
                "primary",
            ),
        ).public_key_sha256
    except (BackendKeyError, ValueError) as exc:
        raise MigrationStateError(
            "test bootstrap could not load the actor verification key"
        ) from exc
    if loaded_actor_key_sha256 != actor_key_sha256:
        raise MigrationStateError(
            "test bootstrap actor verification-key fingerprint mismatches key"
        )
    registry = load_replay_seed_registry()
    generator = CanonicalReplayGenerator()
    for seed in registry.seeds:
        scenario = verify_packaged_seed(seed)
        adapter = replay_external_fact_adapter(
            seed.adapter_version,
            observation_semantics_version="v2",
        )
        adapted = adapter.adapt(scenario, generator.generate(scenario))
        manifest = adapted.manifest
        manifest_bytes = json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        expected = (
            seed.scenario_sha256,
            seed.generator_version,
            seed.adapter_version,
            seed.component_pins_sha256,
            seed.canonical_sequence_sha256,
            seed.manifest_sha256,
            seed.observation_count,
            seed.night_count,
            seed.first_received_at,
            seed.last_received_at,
        )
        actual = (
            manifest.scenario_sha256,
            manifest.generator_version,
            manifest.adapter_version,
            manifest.component_pins_sha256,
            manifest.canonical_sequence_sha256,
            manifest_sha256,
            manifest.observation_count,
            manifest.night_count,
            manifest.first_received_at,
            manifest.last_received_at,
        )
        if actual != expected:
            raise MigrationStateError(
                f"packaged replay seed {seed.seed_id!r} drifted from registry"
            )
        metadata = seed.database_metadata(
            artifact_family=registry.artifact_family,
            schema_manifest_sha256=MIGRATION_MANIFEST_SHA256,
        )
        metadata["actor_verification_key_sha256"] = actor_key_sha256
        metadata["actor_assertion_issuer"] = os.environ.get(
            "SLEEPAGENT_BACKEND_ACTOR_ASSERTION_ISSUER",
            "sleepagent-bff-v1",
        )
        _connection_execute(
            connection,
            "INSERT INTO public.backend_demo_seed_allowlist ("
            "seed_id, seed_sha256, schema_version, generator_version, active, "
            "metadata_json, artifact_family, scenario_id, adapter_version, "
            "manifest_schema_version, expected_observation_count, "
            "component_pins_sha256, canonical_sequence_sha256, "
            "manifest_sha256, first_received_at, last_received_at, night_count"
            ") VALUES ("
            "%s, %s, 'canonical_replay_scenario.v1', %s, TRUE, %s::jsonb, "
            "%s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s"
            ") ON CONFLICT (seed_id) DO UPDATE SET "
            "seed_sha256 = EXCLUDED.seed_sha256, "
            "schema_version = EXCLUDED.schema_version, "
            "generator_version = EXCLUDED.generator_version, "
            "active = TRUE, metadata_json = EXCLUDED.metadata_json, "
            "artifact_family = EXCLUDED.artifact_family, "
            "scenario_id = EXCLUDED.scenario_id, "
            "adapter_version = EXCLUDED.adapter_version, "
            "manifest_schema_version = EXCLUDED.manifest_schema_version, "
            "expected_observation_count = EXCLUDED.expected_observation_count, "
            "component_pins_sha256 = EXCLUDED.component_pins_sha256, "
            "canonical_sequence_sha256 = EXCLUDED.canonical_sequence_sha256, "
            "manifest_sha256 = EXCLUDED.manifest_sha256, "
            "first_received_at = EXCLUDED.first_received_at, "
            "last_received_at = EXCLUDED.last_received_at, "
            "night_count = EXCLUDED.night_count",
            (
                seed.seed_id,
                seed.scenario_sha256,
                seed.generator_version,
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                registry.artifact_family,
                seed.scenario_id,
                seed.adapter_version,
                manifest.schema_version,
                seed.observation_count,
                seed.component_pins_sha256,
                seed.canonical_sequence_sha256,
                seed.manifest_sha256,
                seed.first_received_at,
                seed.last_received_at,
                seed.night_count,
            ),
        )


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


def _default_applied_by() -> str:
    configured = os.environ.get("SLEEPAGENT_MIGRATION_APPLIED_BY", "").strip()
    if configured:
        return configured
    return f"{getpass.getuser()}@{socket.gethostname()}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sleepagent-migrate",
        description=(
            "Apply, verify, or report the manifest-pinned SleepAgent "
            "PostgreSQL schema release"
        ),
    )
    parser.add_argument("action", choices=("apply", "check", "status"))
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
    "BOOTSTRAP_DEMO_PASSWORD_ENV",
    "BOOTSTRAP_DEMO_ROLE_ENV",
    "BOOTSTRAP_WORKER_PASSWORD_ENV",
    "BOOTSTRAP_WORKER_ROLE_ENV",
    "BASELINE_SCHEMA_SHA256",
    "DEFAULT_DATABASE_URL_ENV",
    "LATEST_SCHEMA_VERSION",
    "MIGRATION_ADVISORY_LOCK_ID",
    "MigrationError",
    "MigrationExecutionError",
    "MigrationFile",
    "MigrationLedgerRow",
    "MigrationReleaseError",
    "MigrationStateError",
    "PostgresMigrationRunner",
    "assert_migration_owner_capability",
    "bootstrap_test_database_roles",
    "bootstrap_test_database_roles_from_environment",
    "build_parser",
    "discover_migrations",
    "main",
    "run_migration_command",
    "validate_ledger_rows",
]
