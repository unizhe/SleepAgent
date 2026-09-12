from __future__ import annotations

import os

import pytest

from sleepagent.persistence.migrate import PostgresMigrationRunner
from sleepagent.persistence.migrations import LATEST_SCHEMA_VERSION
from tests.integration.test_observation_semantics_v2_postgres import (
    _apply_prefix,
    _drop_database,
    _temporary_database,
)


pytestmark = pytest.mark.postgres


def test_r1_upgrade_025_to_latest_is_additive_and_security_complete() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = os.environ.get("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN", "").strip()
    if not admin_dsn:
        pytest.skip("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN is required")
    database_name, upgrade_dsn = _temporary_database(
        admin_dsn,
        "sleepagent_r1_upgrade",
    )
    try:
        with psycopg.connect(upgrade_dsn) as admin:
            _apply_prefix(admin, 25)
            assert admin.execute(
                "SELECT max(version) FROM public.sleepagent_schema_migrations"
            ).fetchone() == (25,)
            runner = PostgresMigrationRunner(admin, applied_by="r1-upgrade-proof")
            assert runner.apply() == LATEST_SCHEMA_VERSION == 31
            assert runner.check() == 31
            assert admin.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema='public' "
                "AND column_name IN "
                "('execution_reclaim_count','max_execution_reclaims') "
                "AND table_name IN "
                "('sleep_domain_normalization_work','sleep_domain_operations',"
                "'backend_delivery_intents','backend_retention_jobs')"
            ).fetchone() == (8,)
            constraint = admin.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='backend_acquisition_schedule_jitter_lt_cadence'"
            ).fetchone()
            assert constraint is not None
            assert "jitter_seconds < cadence_seconds" in str(constraint[0])
            policy_text = " ".join(
                str(row[0] or "") + " " + str(row[1] or "")
                for row in admin.execute(
                    "SELECT qual, with_check FROM pg_policies "
                    "WHERE schemaname='public' AND tablename LIKE "
                    "'backend_care%'"
                ).fetchall()
            )
            assert "internal_status" not in policy_text
            assert admin.execute(
                "SELECT prosecdef, proconfig @> ARRAY['search_path=pg_catalog, public'] "
                "FROM pg_proc WHERE oid = "
                "'public.sleepagent_internal_status_principal_allows_v1()'"
                "::regprocedure"
            ).fetchone() == (True, True)
    finally:
        _drop_database(admin_dsn, database_name)
