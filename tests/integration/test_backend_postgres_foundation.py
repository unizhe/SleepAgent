from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
import re
from typing import Any

import pytest

from sleepagent.persistence.migrate import (
    DEFAULT_DATABASE_URL_ENV,
    LATEST_SCHEMA_VERSION,
    MigrationLedgerRow,
    MigrationReleaseError,
    PostgresMigrationRunner,
    MigrationStateError,
    BASELINE_SCHEMA_SHA256,
    bootstrap_test_database_roles_from_environment,
    discover_migrations,
    validate_ledger_rows,
)
from sleepagent.persistence.migrations import (
    EXPECTED_MIGRATION_IDENTITIES,
    MIGRATION_MANIFEST_SHA256,
    MIGRATION_RELEASE,
    migration_identity,
    split_sql_statements,
)
from sleepagent.persistence.uow import (
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
            status="applied",
            transactional=migration.transactional,
            started_at=object(),
            finished_at=object(),
            applied_by="test@ci",
            error_code=None,
        )
        for migration in migrations
    ]
    return migrations, rows


def test_schema_release_is_contiguous_and_manifest_pinned() -> None:
    migrations = discover_migrations()

    assert [item.version for item in migrations] == list(
        range(1, LATEST_SCHEMA_VERSION + 1)
    )
    assert migrations[0].name == "001_initial_schema"
    assert migrations[0].sql_sha256 == BASELINE_SCHEMA_SHA256
    assert migrations[1].name == "002_replay_journey_and_today"
    assert migrations[1].sql_sha256 == MIGRATION_RELEASE.migrations[1].sha256
    assert migrations[2].name == "003_stage2_commands_and_interactions"
    assert migrations[2].sql_sha256 == MIGRATION_RELEASE.migrations[2].sha256
    assert migrations[3].name == "004_stage3_reads_and_scenario_clock"
    assert migrations[3].sql_sha256 == MIGRATION_RELEASE.migrations[3].sha256
    assert len(MIGRATION_MANIFEST_SHA256) == 64
    assert EXPECTED_MIGRATION_IDENTITIES == tuple(
        migration_identity(entry) for entry in MIGRATION_RELEASE.migrations
    )
    assert all(item.transactional for item in migrations)


def test_migration_discovery_rejects_missing_unknown_and_tampered_files(
    tmp_path: Path,
) -> None:
    source = Path("sleepagent/persistence/migrations")

    missing = tmp_path / "missing"
    missing.mkdir()
    (missing / "001_initial_schema.sql").write_bytes(
        (source / "001_initial_schema.sql").read_bytes()
    )
    with pytest.raises(MigrationReleaseError, match="missing"):
        discover_migrations(missing)

    unknown = tmp_path / "unknown"
    unknown.mkdir()
    for entry in MIGRATION_RELEASE.migrations:
        (unknown / entry.filename).write_bytes(
            (source / entry.filename).read_bytes()
        )
    (unknown / "003_unreleased.sql").write_text(
        "-- sleepagent:transactional=true\nSELECT 1;\n",
        encoding="utf-8",
    )
    with pytest.raises(MigrationReleaseError, match="unknown"):
        discover_migrations(unknown)

    tampered = tmp_path / "tampered"
    tampered.mkdir()
    for entry in MIGRATION_RELEASE.migrations:
        body = (source / entry.filename).read_bytes()
        if entry.version == 2:
            body += b"\n-- tampered\n"
        (tampered / entry.filename).write_bytes(body)
    with pytest.raises(MigrationReleaseError, match="checksum"):
        discover_migrations(tampered)


def test_ledger_validation_fails_closed_on_drift_or_unfinished_row() -> None:
    migrations, rows = _complete_ledger()
    validate_ledger_rows(migrations, rows, require_complete=True)

    drifted = list(rows)
    drifted[0] = replace(drifted[0], sql_sha256="0" * 64)
    with pytest.raises(MigrationStateError, match="checksum drift"):
        validate_ledger_rows(migrations, drifted, require_complete=True)

    unfinished = list(rows)
    unfinished[0] = replace(
        unfinished[0], status="started", finished_at=None
    )
    with pytest.raises(MigrationStateError, match="never finished"):
        validate_ledger_rows(migrations, unfinished, require_complete=True)

    validate_ledger_rows(migrations, rows[:1], require_complete=False)
    with pytest.raises(MigrationStateError, match="release target"):
        validate_ledger_rows(migrations, rows[:1], require_complete=True)

    newer = list(rows)
    newer.append(replace(rows[-1], version=LATEST_SCHEMA_VERSION + 1))
    with pytest.raises(MigrationStateError, match="not supported"):
        validate_ledger_rows(migrations, newer, require_complete=True)


def test_migration_cli_defaults_to_compose_database_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DEFAULT_DATABASE_URL_ENV == "SLEEPAGENT_BACKEND_DATABASE_DSN"
    monkeypatch.setenv("SLEEPAGENT_BOOTSTRAP_API_DATABASE_ROLE", "api")
    with pytest.raises(MigrationStateError, match="incomplete"):
        bootstrap_test_database_roles_from_environment(FakeConnection())


def test_migrate_apply_does_not_run_test_bootstrap() -> None:
    source = Path("sleepagent/persistence/migrate.py").read_text(
        encoding="utf-8"
    )
    command = source.split("def run_migration_command(", 1)[1].split(
        "def assert_migration_owner_capability", 1
    )[0]
    compose = Path("compose.yaml").read_text(encoding="utf-8")

    assert "bootstrap_test_database_roles" not in command
    assert "sleepagent.persistence.test_bootstrap" in compose
    assert "test-bootstrap:" in compose


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
        "sleepagent/persistence/migrations"
    )
    baseline = (migration_dir / "001_initial_schema.sql").read_text(
        encoding="utf-8"
    )
    sql = {version: baseline for version in (22, 23, 24, 25)}

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


def test_replay_migration_reservation_is_audited_and_wakes_the_worker() -> None:
    body = Path(
        "sleepagent/persistence/migrations/002_replay_journey_and_today.sql"
    ).read_text(encoding="utf-8")
    reservation = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_reserve_demo_journey", 1
    )[1].split(
        "CREATE OR REPLACE FUNCTION sleepagent_get_demo_operation", 1
    )[0]
    new_root_branch = reservation.split(
        "IF NOT FOUND THEN\n    INSERT INTO public.sleep_domain_operations", 1
    )[1].split(
        "END IF;\n\n  INSERT INTO public.backend_command_receipts", 1
    )[0]

    assert "INSERT INTO public.sleep_domain_operations" in reservation
    assert "INSERT INTO public.backend_demo_journeys" in reservation
    assert "INSERT INTO public.backend_command_receipts" in reservation
    assert "INSERT INTO public.sleep_domain_domain_outbox" in reservation
    assert "'DEMO_JOURNEY_ACCEPTED'" in reservation
    assert "INSERT INTO public.backend_authorization_audit" in reservation
    assert "'demo_seed_reserved'" in reservation
    assert "INSERT INTO public.sleep_domain_domain_outbox" in new_root_branch


def test_replay_bootstrap_conflicts_are_verified_fail_closed() -> None:
    body = Path(
        "sleepagent/persistence/migrations/002_replay_journey_and_today.sql"
    ).read_text(encoding="utf-8")
    bootstrap = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_bootstrap_demo_journey", 1
    )[1].split(
        "CREATE OR REPLACE FUNCTION sleepagent_claim_normalization_work", 1
    )[0]

    for table in (
        "sleep_domain_provider_accounts",
        "sleep_domain_device_identities",
        "sleep_domain_device_bindings",
        "backend_actors",
        "backend_actor_subject_bindings",
    ):
        assert f"INSERT INTO public.{table}" in bootstrap
        assert f"FROM public.{table}" in bootstrap
    assert bootstrap.count("RAISE EXCEPTION 'scenario_contract_invalid'") >= 6


def test_replay_security_definers_have_safe_search_path_and_public_revoke() -> None:
    body = Path(
        "sleepagent/persistence/migrations/002_replay_journey_and_today.sql"
    ).read_text(encoding="utf-8")
    functions = re.findall(
        r"CREATE OR REPLACE FUNCTION\s+([a-z0-9_]+)\b(.*?)"
        r"(?=\nCREATE OR REPLACE FUNCTION|\Z)",
        body,
        flags=re.DOTALL,
    )
    security_definers = [
        (name, definition)
        for name, definition in functions
        if "SECURITY DEFINER" in definition.split("AS $$", 1)[0]
    ]

    assert security_definers
    assert body.count("SECURITY DEFINER") == len(security_definers)
    for name, definition in security_definers:
        assert "SET search_path = pg_catalog, public" in definition
        assert f"REVOKE ALL ON FUNCTION {name}(" in body


def test_replay_closure_requires_exact_episode_membership_set() -> None:
    body = Path(
        "sleepagent/persistence/migrations/002_replay_journey_and_today.sql"
    ).read_text(encoding="utf-8")
    success = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_succeed_demo_journey", 1
    )[1].split(
        "CREATE OR REPLACE FUNCTION sleepagent_bootstrap_demo_journey", 1
    )[0]

    assert "sleep_domain_episode_observation_memberships" in success
    assert "revision.revision_json -> 'observation_ids'" in success
    assert "array_agg(member.observation_id" in success
    assert (
        "ALTER TABLE sleep_domain_episode_observation_memberships\n"
        "  FORCE ROW LEVEL SECURITY"
    ) in body
    role_bootstrap_source = Path(
        "sleepagent/persistence/migrate.py"
    ).read_text(encoding="utf-8")
    assert role_bootstrap_source.count(
        '"sleep_domain_episode_observation_memberships"'
    ) >= 2


def test_replay_closure_uses_the_product_operation_policy_pin() -> None:
    body = Path(
        "sleepagent/persistence/migrations/002_replay_journey_and_today.sql"
    ).read_text(encoding="utf-8")
    success = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_succeed_demo_journey", 1
    )[1].split(
        "CREATE OR REPLACE FUNCTION sleepagent_bootstrap_demo_journey", 1
    )[0]

    assert "max(operation.policy_sha256)" in success
    assert (
        "operation.operation_type = 'product_agent'" in success.split(
            "max(operation.policy_sha256)", 1
        )[1]
    )
    assert "view.policy_sha256 = product_policy_sha256" in success
    assert "view.policy_sha256 = journey_row.policy_sha256" not in success


def test_replay_claim_retry_and_terminal_transitions_are_append_only() -> None:
    body = Path(
        "sleepagent/persistence/migrations/002_replay_journey_and_today.sql"
    ).read_text(encoding="utf-8")
    claim = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_claim_demo_journey", 1
    )[1].split(
        "CREATE OR REPLACE FUNCTION sleepagent_heartbeat_demo_journey", 1
    )[0]
    finalizer = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_finalize_demo_journey_attempt", 1
    )[1].split(
        "CREATE OR REPLACE FUNCTION sleepagent_succeed_demo_journey", 1
    )[0]
    success = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_succeed_demo_journey", 1
    )[1].split(
        "CREATE OR REPLACE FUNCTION sleepagent_bootstrap_demo_journey", 1
    )[0]

    assert "'journey.claimed'" in claim
    assert "claimed.lease_generation::text" in claim
    assert "'journey.retry'" in finalizer
    assert "'retry:' ||" in finalizer
    assert "INSERT INTO public.backend_demo_journey_checkpoints" in finalizer
    for terminal in (finalizer, success):
        assert "INSERT INTO public.backend_demo_journey_receipts" in terminal
        assert "cas_version = cas_version + 1" in terminal
        assert "root_operation_terminal_conflict" in terminal
        assert "'DEMO_JOURNEY_TERMINAL'" in terminal
        assert "INSERT INTO public.backend_authorization_audit" in terminal
        assert "'demo_journey_terminal'" in terminal
        assert "AND lease_generation = journey_row.lease_generation" in terminal


def test_replay_trace_projects_root_and_committed_child_references() -> None:
    body = Path(
        "sleepagent/persistence/migrations/002_replay_journey_and_today.sql"
    ).read_text(encoding="utf-8")
    trace = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_read_demo_trace", 1
    )[1].split(
        "CREATE OR REPLACE FUNCTION sleepagent_claim_demo_journey", 1
    )[0]

    assert "root_operation_id TEXT" in trace
    for reference in (
        "night_episode_revision_id",
        "fast_path_operation_id",
        "product_operation_id",
        "analysis_revision_id",
    ):
        assert f"event.event_json ->> '{reference}'" in trace
    assert "COALESCE(event.correlation_id, target_root_operation_id)" in trace


def test_command_migration_has_real_state_authority_and_queue_capabilities() -> None:
    body = Path(
        "sleepagent/persistence/migrations/003_stage2_commands_and_interactions.sql"
    ).read_text(encoding="utf-8")

    for table in (
        "backend_monitoring_transition_receipts",
        "backend_human_facts",
        "backend_product_interactions",
        "backend_product_interaction_revisions",
        "backend_human_decisions_v2",
        "backend_care_actions_v2",
        "backend_reanalysis_links",
    ):
        assert f"CREATE TABLE {table}" in body
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in body
    authority = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_stage2_authority_allows", 1
    )[1].split("REVOKE ALL ON FUNCTION", 1)[0]
    assert "SECURITY DEFINER" in authority
    assert "SET search_path = pg_catalog, public" in authority
    assert "binding_row.status = 'active'" in authority
    assert "caller_grant.scopes_json ? requested_scope" in authority
    claim = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_claim_operation", 1
    )[1].split("REVOKE ALL ON FUNCTION", 1)[0]
    assert "grant_row.allowed_handlers_json ? requested_queue" in claim
    assert "grant_row.allowed_handlers_json ? op.operation_type" not in claim


def test_read_model_migration_has_fenced_advance_and_append_only_receipt() -> None:
    body = Path(
        "sleepagent/persistence/migrations/004_stage3_reads_and_scenario_clock.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE backend_demo_advance_receipts" in body
    assert "CREATE TABLE backend_replay_staged_facts" in body
    assert "sleepagent_enforce_staged_fact_release" in body
    assert "sleepagent_operation_fence_allows" in body
    assert "backend_demo_advance_receipt_immutable" in body
    assert "FORCE ROW LEVEL SECURITY" in body
    assert "sleep_domain_care_followup_scope" in body
    assert "sleep_domain_care_followup_receipt_scope" in body
    assert "backend_episode_date_reconciliation_v2_contract" in body
    assert "backend_episode_date_reconciliation_operation_fk" in body
    assert "idx_backend_episode_date_reconciliation_pending_v2" in body
    assert "protocol_version = 2 AND operation_id IS NOT NULL" in body
    role_bootstrap = Path("sleepagent/persistence/migrate.py").read_text(
        encoding="utf-8"
    )
    worker_updates = role_bootstrap.split("worker_update_tables = (", 1)[1].split(
        ")", 1
    )[0]
    worker_inserts = role_bootstrap.split("worker_insert_tables = (", 1)[1].split(
        ")", 1
    )[0]
    worker_reads = role_bootstrap.split("worker_tables = (", 1)[1].split(
        ")", 1
    )[0]
    assert '"backend_episode_date_reconciliation"' in worker_updates
    assert '"backend_replay_scenario_clocks"' in worker_inserts
    assert '"backend_replay_staged_facts"' in worker_reads
    operation_claim = Path(
        "sleepagent/persistence/migrations/003_stage2_commands_and_interactions.sql"
    ).read_text(encoding="utf-8").split(
        "CREATE OR REPLACE FUNCTION sleepagent_claim_operation", 1
    )[1].split("REVOKE ALL ON FUNCTION", 1)[0]
    assert "op.operation_type <> 'fast_path'" in operation_claim
    assert "pending_normalization.status <> 'succeeded'" in operation_claim
    replay_bootstrap = Path(
        "sleepagent/persistence/migrations/002_replay_journey_and_today.sql"
    ).read_text(encoding="utf-8")
    assert '"reconciliation"' in replay_bootstrap
    reservation = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_reserve_demo_advance", 1
    )[1].split(
        "CREATE OR REPLACE FUNCTION sleepagent_stage3_advance_authority_allows",
        1,
    )[0]
    assert "backend_command_receipts" in reservation
    assert "sleep_domain_operations" in reservation
    assert "DEMO_ADVANCE_ACCEPTED" in reservation
    assert "demo_advance_reserved" in reservation
    assert "namespace_row.current_generation" in reservation

    functions = re.findall(
        r"CREATE OR REPLACE FUNCTION\s+([a-z0-9_]+)\b(.*?)"
        r"(?=\nCREATE OR REPLACE FUNCTION|\Z)",
        body,
        flags=re.DOTALL,
    )
    security_definers = [
        (name, definition)
        for name, definition in functions
        if "SECURITY DEFINER" in definition.split("AS $$", 1)[0]
    ]
    assert len(security_definers) == 3
    for name, definition in security_definers:
        assert "SET search_path = pg_catalog, public" in definition
        assert f"REVOKE ALL ON FUNCTION {name}(" in body


def test_effect_migration_has_induction_delivery_and_reconciliation_authority() -> None:
    body = Path(
        "sleepagent/persistence/migrations/005_stage4_induction_delivery_reconciliation.sql"
    ).read_text(encoding="utf-8")

    for table in (
        "backend_induction_manifests_v2",
        "backend_personalization_profiles_v2",
        "backend_personalization_profile_revisions_v2",
        "backend_induction_receipts_v2",
        "backend_replay_delivery_effects_v2",
        "backend_delivery_reconciliation_receipts_v2",
    ):
        assert f"CREATE TABLE {table}" in body
        assert f"ALTER TABLE {table}" in body
        assert "FORCE ROW LEVEL SECURITY" in body.split(
            f"ALTER TABLE {table}", 1
        )[1]
    assert "uq_backend_personalization_profile_v2_scope" in body
    assert "backend_delivery_intent_v2_contract" in body
    assert "recipient_binding_id" in body
    authority = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_stage4_delivery_authority_allows",
        1,
    )[1].split("REVOKE ALL ON FUNCTION", 1)[0]
    for invariant in (
        "namespace_row.current_generation = intent.namespace_generation",
        "binding_row.status = 'active'",
        "binding_row.authorization_epoch = epoch_row.authorization_epoch",
        "worker_grant.allowed_handlers_json ? intent.handler_name",
        "decision.choice = 'confirm'",
    ):
        assert invariant in authority
    permit = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_mark_delivery_dispatching", 1
    )[1].split("REVOKE ALL ON FUNCTION", 1)[0]
    assert "sleepagent_stage4_delivery_authority_allows" in permit
    assert "protocol_version < 2" in permit
    internal_status = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_internal_reconciliation_status",
        1,
    )[1].split("REVOKE ALL ON FUNCTION", 1)[0]
    assert "purpose', TRUE), '') <>" in internal_status
    assert "'internal_status'" in internal_status
    assert "operation.operation_type IN" in internal_status
    assert "operation.subject_id" not in internal_status.split(
        "jsonb_build_object", 1
    )[1].split(") INTO result", 1)[0]

    role_bootstrap = Path("sleepagent/persistence/migrate.py").read_text(
        encoding="utf-8"
    )
    worker_tables = role_bootstrap.split("worker_tables = (", 1)[1].split(
        ")", 1
    )[0]
    worker_inserts = role_bootstrap.split("worker_insert_tables = (", 1)[1].split(
        ")", 1
    )[0]
    worker_updates = role_bootstrap.split("worker_update_tables = (", 1)[1].split(
        ")", 1
    )[0]
    assert '"backend_induction_manifests_v2"' in worker_tables
    assert '"backend_command_receipts"' in worker_tables
    assert '"backend_induction_receipts_v2"' in worker_inserts
    assert '"backend_replay_delivery_effects_v2"' in worker_inserts
    assert '"backend_personalization_profiles_v2"' in worker_updates
    assert '"sleep_domain_care_followups"' in worker_updates
    assert '"backend_delivery_intents"' in worker_updates
    assert 'sql.Identifier("public", "backend_delivery_intents")' in role_bootstrap
    assert (
        'sql.Identifier("public", "backend_induction_manifests_v2")'
        in role_bootstrap
    )
    assert 'f"{DELIVERY_AUTHORITY_FUNCTION}(text)"' in role_bootstrap
    assert '"sleepagent_internal_reconciliation_status(text)"' in role_bootstrap


def test_retention_migration_has_bounded_shred_and_durable_reset_protocol() -> None:
    body = Path(
        "sleepagent/persistence/migrations/006_stage5_bounded_retention_and_reset.sql"
    ).read_text(encoding="utf-8")

    assert "'replay_raw_short_v1', 'bounded-replay-raw.v1', 2" in body
    assert "VALIDATE CONSTRAINT sleep_domain_raw_encryption_v2_contract" in body
    assert "backend_retention_binding_generation_fk" in body
    for table in (
        "backend_demo_resets_v2",
        "backend_demo_reset_key_events_v2",
        "backend_demo_reset_key_receipts_v2",
        "backend_demo_reset_receipts_v2",
    ):
        assert f"CREATE TABLE {table}" in body
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in body
    reserve = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_reserve_demo_reset", 1
    )[1].split("REVOKE ALL ON FUNCTION", 1)[0]
    assert "requested_caller_idempotency_key TEXT" in reserve
    assert "requested_sha256 TEXT" in reserve
    assert "reserved_namespace_generation BIGINT" in reserve
    assert (
        "receipt.caller_idempotency_key = requested_caller_idempotency_key"
        in reserve
    )
    for invariant in (
        "current_generation = target_generation",
        "privacy_epoch = privacy_epoch + 1",
        "retrieval_policy_epoch = retrieval_policy_epoch + 1",
        "status = 'revoked'",
        "'subject_forget', 'pending'",
        "'/demo/v1/reset'",
    ):
        assert invariant in reserve
    prepare = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_prepare_demo_reset_key", 1
    )[1].split("REVOKE ALL ON FUNCTION", 1)[0]
    assert "provider_call_reserved" in prepare
    assert "status = 'retired'" in prepare
    assert "target_dek.retention_domain = dek_row.retention_domain" in prepare
    commit = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_commit_demo_reset_key", 1
    )[1].split("REVOKE ALL ON FUNCTION", 1)[0]
    assert "backend_demo_reset_key_receipts_v2" in commit
    assert "status = 'shredded'" in commit
    complete = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_complete_demo_reset", 1
    )[1].split("REVOKE ALL ON FUNCTION", 1)[0]
    assert "job.status <> 'succeeded'" in complete
    assert "backend_demo_reset_receipts_v2" in complete
    assert "sleepagent_finalize_operation" in complete
    wait = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_wait_demo_reset", 1
    )[1].split("REVOKE ALL ON FUNCTION", 1)[0]
    assert "attempt_count = GREATEST(operation.attempt_count - 1, 0)" in wait
    assert "sleepagent_operation_fence_allows" in wait
    for forbidden in (
        "'subject_id', reset_row.subject_id",
        "'raw_ingress_record_id'",
        "'encrypted_payload'",
        "'plaintext'",
    ):
        assert forbidden not in complete

    role_bootstrap = Path("sleepagent/persistence/migrate.py").read_text(
        encoding="utf-8"
    )
    worker_inserts = role_bootstrap.split("worker_insert_tables = (", 1)[1].split(
        ")", 1
    )[0]
    assert '"sleep_domain_raw_inbox"' in worker_inserts
    assert '"sleep_domain_normalization_work"' in worker_inserts
    assert '"backend_retention_deks"' in worker_inserts
    assert '"sleepagent_prepare_demo_reset_key(text,bigint,text,text)"' in role_bootstrap
    assert (
        '"sleepagent_wait_demo_reset(text,bigint,bigint,text,timestamptz)"'
        in role_bootstrap
    )
    assert '"sleepagent_reserve_demo_reset(text,text,text,text,text,text,text)"' in role_bootstrap


def test_stage6_migration_closes_rls_epoch_and_namespace_fairness_gaps() -> None:
    body = Path(
        "sleepagent/persistence/migrations/007_stage6_system_hardening.sql"
    ).read_text(encoding="utf-8")

    for table in (
        "sleep_domain_processing_receipts",
        "sleep_domain_adapter_candidates",
        "sleep_domain_quality_assessments",
        "sleep_domain_risk_assessments",
        "sleep_domain_vendor_alert_instances",
        "sleep_domain_alert_correlation_receipts",
        "sleep_domain_fast_path_signal_projections",
        "sleep_domain_fast_path_signal_receipts",
    ):
        assert f"ALTER TABLE {table}" in body
        assert re.search(
            rf"ALTER TABLE {table}\s+FORCE ROW LEVEL SECURITY",
            body,
        )
    assert "sleepagent_invalidate_epoch_scoped_projections" in body
    assert "DELETE FROM public.sleep_domain_current_quality" in body
    assert "DELETE FROM public.sleep_domain_current_risk" in body
    assert "backend_queue_namespace_fairness" in body
    assert "sleepagent_namespace_has_worker_capacity" in body
    assert "sleepagent_try_reserve_namespace_capacity" in body
    assert "sleepagent_namespace_last_claimed_at" in body
    assert "pg_try_advisory_xact_lock" in body
    assert "sleepagent_internal_operational_metrics" in body
    assert "oldest_ready_age_seconds" in body
    assert "lease_reclaimable_count" in body
    assert "outcome_unknown_count" in body
    assert "backend_product_attempts" in body
    assert "sleep_domain_risk_assessments" in body
    operational = body.split(
        "CREATE OR REPLACE FUNCTION sleepagent_internal_operational_metrics", 1
    )[1].split("REVOKE ALL ON FUNCTION", 1)[0]
    for forbidden in (
        "subject_id'",
        "namespace_id'",
        "resource_id'",
        "error_code'",
    ):
        assert forbidden not in operational
    role_bootstrap = Path("sleepagent/persistence/migrate.py").read_text(
        encoding="utf-8"
    )
    assert '"sleepagent_internal_operational_metrics()"' in role_bootstrap
    for claim in (
        "sleepagent_claim_normalization_work",
        "sleepagent_claim_operation",
        "sleepagent_claim_delivery",
        "sleepagent_claim_retention_job",
        "sleepagent_claim_demo_journey",
    ):
        assert f"public.{claim}" in body


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
        "sleepagent/persistence/migrations/001_initial_schema.sql"
    )
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
