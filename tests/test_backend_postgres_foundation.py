from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from sleepagent.radar_agent.persistence.migrate import (
    DEFAULT_DATABASE_URL_ENV,
    LATEST_SCHEMA_VERSION,
    MigrationLedgerRow,
    PostgresMigrationRunner,
    MigrationStateError,
    RELEASE_021_SHA256,
    bootstrap_test_database_roles_from_environment,
    discover_migrations,
    validate_ledger_rows,
)
from sleepagent.radar_agent.persistence.migrations import split_sql_statements
from sleepagent.radar_agent.persistence.uow import (
    AuthorityResolutionScope,
    PoolContextLeakError,
    PostgresUnitOfWork,
    RepositoryTransactionControlError,
    UnitOfWorkStateError,
    UowScope,
    WorkerClaimScope,
    reset_pooled_connection,
)


pytestmark = pytest.mark.unit


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.rowcount = 1
        self._row: Any = None

    def execute(self, query: str, params: Any = None) -> None:
        self.connection.statements.append((query, params))
        if query.startswith("SELECT current_setting"):
            count = query.count("current_setting")
            self._row = tuple(self.connection.leaked_scope for _ in range(count))

    def fetchone(self) -> Any:
        return self._row

    def fetchall(self) -> list[Any]:
        return []

    def close(self) -> None:
        self.connection.closed_cursors += 1


class FakeConnection:
    def __init__(self, *, leaked_scope: str | None = None) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed_cursors = 0
        self.leaked_scope = leaked_scope

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def execute(self, query: str, params: Any = None) -> None:
        self.statements.append((query, params))

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeCheckout(AbstractContextManager[FakeConnection]):
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.exits = 0

    def __enter__(self) -> FakeConnection:
        return self.connection

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.exits += 1
        return False


class FakeProvider:
    def __init__(self, connection: FakeConnection) -> None:
        self.checkout = FakeCheckout(connection)

    def connection(self) -> FakeCheckout:
        return self.checkout


def _worker_scope() -> UowScope:
    return UowScope(
        namespace_id="replay:backend-test",
        namespace_generation=3,
        data_mode="replay",
        run_id="run-7",
        arm_id="arm-control",
        process_role="worker",
        purpose="product_analysis",
        service_principal_id="sleepagent-worker-test",
        subject_id="subject-1",
        authorization_epoch=8,
        privacy_epoch=5,
        retrieval_policy_epoch=4,
        worker_instance="worker-1",
    )


def test_uow_sets_exact_transaction_local_scope_and_owns_commit() -> None:
    raw = FakeConnection()
    uow = PostgresUnitOfWork(FakeProvider(raw), _worker_scope())

    with uow:
        facade = uow.connection
        facade.execute("SELECT 1")
        with pytest.raises(RepositoryTransactionControlError):
            facade.commit()
        uow.commit()

    configured = {
        params[0]: params[1]
        for query, params in raw.statements
        if query == "SELECT set_config(%s, %s, true)"
    }
    assert configured["sleepagent.namespace_id"] == "replay:backend-test"
    assert configured["sleepagent.namespace_generation"] == "3"
    assert configured["sleepagent.run_id"] == "run-7"
    assert configured["sleepagent.arm_id"] == "arm-control"
    assert configured["sleepagent.authorization_epoch"] == "8"
    assert configured["sleepagent.worker_instance"] == "worker-1"
    assert uow.committed is True
    assert raw.commits >= 2  # business commit plus pool reset

    with pytest.raises(UnitOfWorkStateError, match="single-use"):
        uow.__enter__()


def test_uow_rolls_back_without_explicit_commit() -> None:
    raw = FakeConnection()
    with PostgresUnitOfWork(FakeProvider(raw), _worker_scope()):
        pass

    assert raw.rollbacks >= 3  # checkout clean, implicit rollback, reset checks


def test_pre_scope_contexts_expose_no_forged_namespace_or_epochs() -> None:
    authority = AuthorityResolutionScope(
        data_mode="replay",
        purpose="read_sleep",
        service_principal_id="sleepagent-api-test",
        actor_id="actor-1",
    ).guc_values()
    claim = WorkerClaimScope(
        data_mode="replay",
        purpose="durable_work",
        service_principal_id="sleepagent-worker-test",
        worker_instance="worker-1",
    ).guc_values()

    assert authority["sleepagent.namespace_id"] == ""
    assert authority["sleepagent.authorization_epoch"] == ""
    assert authority["sleepagent.process_role"] == "api"
    assert claim["sleepagent.subject_id"] == ""
    assert claim["sleepagent.process_role"] == "worker"


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"run_id": None}, "requires run_id and arm_id"),
        ({"namespace_generation": 0}, "must be positive"),
        ({"authorization_epoch": None}, "all governance epochs"),
        ({"actor_id": "forged"}, "cannot self-assert an actor_id"),
    ],
)
def test_uow_rejects_incomplete_or_forged_scope(
    changes: dict[str, Any], message: str
) -> None:
    values = {
        field: getattr(_worker_scope(), field)
        for field in _worker_scope().__dataclass_fields__
    }
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        UowScope(**values)


def test_pool_reset_detects_scope_leak() -> None:
    with pytest.raises(PoolContextLeakError):
        reset_pooled_connection(FakeConnection(leaked_scope="replay:leaked"))


def _complete_ledger() -> tuple[Any, list[MigrationLedgerRow]]:
    migrations = discover_migrations()
    rows = [
        MigrationLedgerRow(
            version=migration.version,
            migration_name=migration.name,
            sql_sha256=migration.sql_sha256,
            status=(
                "legacy_attested" if migration.version <= 20 else "applied"
            ),
            transactional=migration.transactional,
            started_at=object(),
            finished_at=object(),
            applied_by="test@ci",
            error_code=None,
        )
        for migration in migrations
    ]
    return migrations, rows


def test_migration_release_is_contiguous_and_021_is_pinned() -> None:
    migrations = discover_migrations()

    assert [item.version for item in migrations] == list(
        range(1, LATEST_SCHEMA_VERSION + 1)
    )
    assert migrations[20].sql_sha256 == RELEASE_021_SHA256
    assert all(item.transactional for item in migrations)


def test_ledger_validation_fails_closed_on_drift_or_unfinished_row() -> None:
    migrations, rows = _complete_ledger()
    validate_ledger_rows(migrations, rows, require_complete=True)

    drifted = list(rows)
    drifted[21] = replace(drifted[21], sql_sha256="0" * 64)
    with pytest.raises(MigrationStateError, match="checksum drift"):
        validate_ledger_rows(migrations, drifted, require_complete=True)

    unfinished = list(rows)
    unfinished[22] = replace(
        unfinished[22], status="started", finished_at=None
    )
    with pytest.raises(MigrationStateError, match="never finished"):
        validate_ledger_rows(migrations, unfinished, require_complete=True)


def test_migration_cli_defaults_to_compose_database_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DEFAULT_DATABASE_URL_ENV == "SLEEPAGENT_BACKEND_DATABASE_DSN"
    monkeypatch.setenv("SLEEPAGENT_BOOTSTRAP_API_DATABASE_ROLE", "api")
    with pytest.raises(MigrationStateError, match="incomplete"):
        bootstrap_test_database_roles_from_environment(FakeConnection())


def test_migration_runner_uses_public_as_the_ddl_target_schema() -> None:
    connection = FakeConnection()
    runner = PostgresMigrationRunner(connection, applied_by="test@ci")

    with runner._advisory_lock():
        pass

    assert ("SET search_path = public, pg_catalog", None) in connection.statements
    assert ("SET search_path = pg_catalog, public", None) not in connection.statements


def test_sql_splitter_ignores_semicolons_in_comments_and_quoted_regions() -> None:
    sql = """
    -- Explain the first statement; this is not a boundary.
    CREATE TABLE "odd;name" (value TEXT DEFAULT 'a;b');
    /* outer; comment /* nested; comment */ still one comment; */
    CREATE FUNCTION example() RETURNS void LANGUAGE plpgsql AS $body$
    BEGIN
      PERFORM ';'; -- function-body semicolon
    END;
    $body$;
    """

    statements = split_sql_statements(sql)

    assert len(statements) == 2
    assert 'CREATE TABLE "odd;name"' in statements[0]
    assert "CREATE FUNCTION example()" in statements[1]


def test_new_sql_contracts_cover_locked_security_and_durability() -> None:
    migration_dir = Path(
        "sleepagent/radar_agent/persistence/migrations"
    )
    sql = {
        version: (migration_dir / filename).read_text(encoding="utf-8")
        for version, filename in {
            22: "022_backend_scope_identity.sql",
            23: "023_backend_work_protocol.sql",
            24: "024_episode_contract_v2.sql",
            25: "025_backend_encryption_retention.sql",
        }.items()
    }

    assert "database_role_name::text = session_user::text" in sql[22]
    assert "sleepagent_resolve_actor_authority" in sql[22]
    assert "sleepagent_consume_actor_assertion" in sql[22]
    assert "ON CONFLICT DO NOTHING" in sql[22]
    assert "sleepagent_subject_generation_scope_allows" in sql[22]
    assert "sleepagent_claim_normalization_work" in sql[23]
    assert "sleepagent_claim_operation" in sql[23]
    assert "sleepagent_claim_delivery" in sql[23]
    assert "sleepagent_finalize_operation" in sql[23]
    assert "backend_pending_handles" in sql[23]
    assert "sleepagent_consume_pending_handle" in sql[23]
    assert "backend_product_attempts" in sql[23]
    assert "query_visible BOOLEAN NOT NULL DEFAULT FALSE" in sql[23]
    assert "backend_replay_scenario_clocks" in sql[23]
    assert "ScenarioClock mutation requires a live operation fence" in sql[23]
    assert "vendor_wake_date" in sql[24]
    assert "'vendor_wake'" not in sql[24]
    assert "date_confidence" in sql[24]
    assert "COALESCE(run_id, '')" in sql[24]
    assert "sleepagent_claim_retention_job" in sql[25]
    assert "sleepagent_finalize_retention_job" in sql[25]
    assert "sleepagent_enforce_shred_receipt_fence" in sql[25]
    assert "sleepagent_retention_fence_allows" in sql[25]
    assert "backend_retention_event_fence" in sql[25]
    assert "retention DEK shred requires a current fenced job" in sql[25]
    assert "lease_generation BIGINT NOT NULL CHECK" in sql[25]

    for body in sql.values():
        assert len(split_sql_statements(body)) > 10


@pytest.mark.parametrize(
    ("version", "function_name"),
    [
        (23, "sleepagent_claim_normalization_work"),
        (23, "sleepagent_claim_operation"),
        (23, "sleepagent_claim_delivery"),
        (25, "sleepagent_claim_retention_job"),
    ],
)
def test_every_cross_scope_claim_returns_an_exact_subject_uow_scope(
    version: int,
    function_name: str,
) -> None:
    migration_path = Path(
        "sleepagent/radar_agent/persistence/migrations"
    ) / {
        23: "023_backend_work_protocol.sql",
        25: "025_backend_encryption_retention.sql",
    }[version]
    body = migration_path.read_text(encoding="utf-8")
    function = body.split(
        f"CREATE OR REPLACE FUNCTION {function_name}", 1
    )[1].split("LANGUAGE plpgsql", 1)[0]

    for declaration in (
        "namespace_id TEXT",
        "data_mode TEXT",
        "namespace_generation BIGINT",
        "run_id TEXT",
        "arm_id TEXT",
        "subject_id TEXT",
        "authorization_epoch BIGINT",
        "privacy_epoch BIGINT",
        "retrieval_policy_epoch BIGINT",
        "lease_generation BIGINT",
        "fencing_token TEXT",
    ):
        assert declaration in function
