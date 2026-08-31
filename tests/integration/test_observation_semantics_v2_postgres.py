from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

import pytest

from sleepagent.domain.contracts import (
    ObservationType,
    ProviderDeviceIdentity,
    SleepObservation,
)
from sleepagent.integrations.perceptor.pull import (
    normalize_history,
    normalize_sleep_report,
)
from sleepagent.persistence.migrate import (
    LATEST_SCHEMA_VERSION,
    PostgresMigrationRunner,
    discover_migrations,
)
from sleepagent.persistence.migrations import split_sql_statements
from sleepagent.persistence.observation_semantics_upcast import run_upcast


pytestmark = pytest.mark.postgres
UTC = timezone.utc
AT = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
NAMESPACE = "live:g2b-m3-upgrade"
SUBJECT = "g2b-m3-subject"
ACCOUNT = "g2b-m3-account"
BINDING = "g2b-m3-binding"
DEVICE = ProviderDeviceIdentity(provider_device_id="g2b-m3-device")


def _admin_dsn() -> str:
    value = os.environ.get("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN", "").strip()
    if not value:
        pytest.skip("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN is required")
    return value


def _database_dsn(dsn: str, database: str) -> str:
    parsed = urlsplit(dsn)
    return urlunsplit(parsed._replace(path=f"/{database}"))


def _temporary_database(admin_dsn: str, prefix: str) -> tuple[str, str]:
    psycopg = pytest.importorskip("psycopg")
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            psycopg.sql.SQL("CREATE DATABASE {}").format(
                psycopg.sql.Identifier(name)
            )
        )
    return name, _database_dsn(admin_dsn, name)


def _drop_database(admin_dsn: str, name: str) -> None:
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        connection.execute(
            psycopg.sql.SQL("DROP DATABASE {}").format(
                psycopg.sql.Identifier(name)
            )
        )


def _apply_prefix(connection: object, target: int) -> None:
    migrations = discover_migrations()
    for migration in migrations[:target]:
        with connection.cursor() as cursor:
            for statement in split_sql_statements(migration.sql):
                cursor.execute(statement)
            cursor.execute(
                "INSERT INTO sleepagent_schema_migrations ("
                "version, migration_name, sql_sha256, status, transactional, "
                "started_at, finished_at, applied_by, error_code) VALUES ("
                "%s, %s, %s, 'applied', %s, clock_timestamp(), "
                "clock_timestamp(), 'g2b-m3-upgrade-test', NULL)",
                (
                    migration.version,
                    migration.name,
                    migration.sql_sha256,
                    migration.transactional,
                ),
            )
        connection.commit()


def _context() -> dict[str, object]:
    return {
        "provider_account_id": ACCOUNT,
        "provider_device": DEVICE,
        "raw_sha256": "c" * 64,
        "requested_at": AT,
        "received_at": AT + timedelta(seconds=1),
    }


def _observation(candidate: object, suffix: str) -> SleepObservation:
    return SleepObservation(
        observation_id=f"g2b-m3-observation-{suffix}",
        data_mode=candidate.data_mode,
        observation_type=candidate.observation_type,
        payload=candidate.payload,
        subject_id=SUBJECT,
        device_id="g2b-m3-internal-device",
        device_binding_id=BINDING,
        binding_version=1,
        request_signed_at=candidate.request_signed_at,
        measurement_at=candidate.measurement_at,
        event_occurred_at=candidate.event_occurred_at,
        received_at=candidate.received_at,
        source_timestamp_text=candidate.source_timestamp_text,
        timezone_status=candidate.timezone_status,
        source_kind=candidate.source_kind,
        quality=candidate.quality,
        provenance=candidate.provenance,
        source_key=candidate.source_key,
        idempotency_key=candidate.idempotency_key,
    )


def _legacy_rows() -> list[tuple[object, SleepObservation]]:
    history = normalize_history(
        [
            {
                "device_id": DEVICE.provider_device_id,
                "heart_rate": "60",
                "breath_rate": "14",
                "body_shake": "12.5",
                "send_time": "2026-08-23T08:00:00",
            }
        ],
        binding_timezone_name="UTC",
        **_context(),
    )
    index = next(
        item
        for item in history.candidates
        if item.observation_type is ObservationType.MOVEMENT
    )
    heart = next(
        item
        for item in history.candidates
        if item.observation_type is ObservationType.HEART_RATE
    )
    count = normalize_sleep_report(
        {"body_shake_data": [{"hour": 8, "count": 21}]},
        report_date=date(2026, 8, 23),
        binding_timezone_name="UTC",
        **_context(),
    ).candidates[0]
    ambiguous = normalize_sleep_report(
        {"body_shake_data": [{"time_long": int(AT.timestamp()), "value": 21}]},
        report_date=date(2026, 8, 23),
        binding_timezone_name="UTC",
        **_context(),
    ).candidates[0]
    duplicate = index.model_copy(
        update={
            "candidate_id": "g2b-m3-duplicate-candidate",
            "source_key": "g2b-m3-duplicate-source",
            "idempotency_key": "g2b-m3-duplicate-idempotency",
        }
    )
    return [
        (index, _observation(index, "index")),
        (count, _observation(count, "count")),
        (ambiguous, _observation(ambiguous, "ambiguous")),
        (heart, _observation(heart, "heart")),
        (duplicate, _observation(duplicate, "duplicate")),
    ]


def _seed_legacy(connection: object) -> tuple[str, str]:
    rows = _legacy_rows()
    raw_id = rows[0][0].provenance.raw_ingress_record_id
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO sleep_domain_provider_accounts VALUES ("
            "%s, 'live', %s, 'perceptor', %s, 'active', '{}'::jsonb, %s)",
            (NAMESPACE, ACCOUNT, "f" * 64, AT),
        )
        cursor.execute(
            "INSERT INTO sleep_domain_device_bindings ("
            "device_binding_id, namespace_id, data_mode, binding_version, "
            "device_id, provider_id, provider_account_id, subject_id, "
            "timezone_name, effective_from, status, binding_json, recorded_at) "
            "VALUES (%s, %s, 'live', 1, %s, 'perceptor', %s, %s, 'UTC', %s, "
            "'active', '{}'::jsonb, %s)",
            (
                BINDING,
                NAMESPACE,
                "g2b-m3-internal-device",
                ACCOUNT,
                SUBJECT,
                AT - timedelta(days=1),
                AT,
            ),
        )
        cursor.execute(
            "INSERT INTO sleep_domain_raw_inbox ("
            "raw_ingress_record_id, namespace_id, data_mode, provider_id, "
            "provider_account_id, event_type, received_at, "
            "signature_verification, idempotency_identity, idempotency_version, "
            "pre_normalization_payload_sha256, encrypted_payload, "
            "encryption_key_id, encrypted_at, content_type, payload_size_bytes, "
            "retention_until, raw_metadata_json) VALUES ("
            "%s, %s, 'live', 'perceptor', %s, 'legacy-upgrade-proof', %s, "
            "'verified', 'g2b-m3-raw', 'v1', %s, %s, 'test-key', %s, "
            "'application/json', 14, %s, '{}'::jsonb)",
            (
                raw_id,
                NAMESPACE,
                ACCOUNT,
                AT,
                "d" * 64,
                b"legacy-raw-v1",
                AT,
                AT + timedelta(days=1),
            ),
        )
        for candidate, observation in rows:
            cursor.execute(
                "INSERT INTO sleep_domain_adapter_candidates ("
                "candidate_id, namespace_id, data_mode, raw_ingress_record_id, "
                "provider_account_id, source_key, idempotency_key, "
                "observation_type, candidate_json, received_at, created_at) "
                "VALUES (%s, %s, 'live', %s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                (
                    candidate.candidate_id,
                    NAMESPACE,
                    raw_id,
                    ACCOUNT,
                    candidate.source_key,
                    candidate.idempotency_key,
                    candidate.observation_type.value,
                    candidate.model_dump_json(),
                    candidate.received_at,
                    AT,
                ),
            )
            cursor.execute(
                "INSERT INTO sleep_domain_canonical_observations ("
                "observation_id, namespace_id, data_mode, candidate_id, "
                "raw_ingress_record_id, subject_id, device_id, "
                "device_binding_id, binding_version, observation_type, "
                "source_key, idempotency_key, observation_json, measurement_at, "
                "event_occurred_at, received_at, created_at) VALUES ("
                "%s, %s, 'live', %s, %s, %s, %s, %s, 1, %s, %s, %s, "
                "%s::jsonb, %s, %s, %s, %s)",
                (
                    observation.observation_id,
                    NAMESPACE,
                    candidate.candidate_id,
                    raw_id,
                    SUBJECT,
                    observation.device_id,
                    BINDING,
                    observation.observation_type.value,
                    observation.source_key,
                    observation.idempotency_key,
                    observation.model_dump_json(),
                    observation.measurement_at,
                    observation.event_occurred_at,
                    observation.received_at,
                    AT,
                ),
            )
        cursor.execute(
            "SELECT pre_normalization_payload_sha256, "
            "encode(digest(encrypted_payload, 'sha256'), 'hex') "
            "FROM sleep_domain_raw_inbox WHERE raw_ingress_record_id = %s",
            (raw_id,),
        )
        hashes = cursor.fetchone()
    connection.commit()
    return str(hashes[0]), str(hashes[1])


def test_migration_014_fresh_upgrade_upcast_rls_and_privileges() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _admin_dsn()
    fresh_name, fresh_dsn = _temporary_database(admin_dsn, "sleepagent_m3_fresh")
    upgrade_name, upgrade_dsn = _temporary_database(admin_dsn, "sleepagent_m3_upgrade")
    try:
        with psycopg.connect(fresh_dsn) as connection:
            runner = PostgresMigrationRunner(
                connection, applied_by="g2b-m3-fresh-test"
            )
            assert runner.apply() == LATEST_SCHEMA_VERSION
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = 'sleep_domain_observation_semantics_v2'::regclass"
                )
                assert cursor.fetchone() == (True, True)
                cursor.execute(
                    "SELECT count(*) FROM information_schema.table_privileges "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'sleep_domain_observation_semantics_v2' "
                    "AND grantee = 'PUBLIC'"
                )
                assert cursor.fetchone() == (0,)
                cursor.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                    "AND tablename = 'sleep_domain_observation_semantics_v2' "
                    "ORDER BY indexname"
                )
                assert {
                    "idx_sleep_domain_observation_semantics_v2_count_window",
                    "idx_sleep_domain_observation_semantics_v2_metric_time",
                }.issubset({str(row[0]) for row in cursor.fetchall()})

        with psycopg.connect(upgrade_dsn) as connection:
            _apply_prefix(connection, 13)
            before_hashes = _seed_legacy(connection)
            runner = PostgresMigrationRunner(
                connection, applied_by="g2b-m3-upgrade-test"
            )
            assert runner.apply() == LATEST_SCHEMA_VERSION
            dry_run = run_upcast(
                connection, batch_size=2, max_rows=20, dry_run=True
            )
            assert dry_run.scanned == 4
            assert dry_run.already_classified == 0
            assert dry_run.inserted == 0
            assert dry_run.errors == 0
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM "
                    "sleep_domain_observation_semantics_v2"
                )
                assert cursor.fetchone() == (0,)
            first = run_upcast(connection, batch_size=2, max_rows=20)
            assert first.scanned == 4
            assert first.movement_index == 2
            assert first.movement_event_count == 1
            assert first.legacy_ambiguous_movement == 1
            assert first.duplicate_semantic_identity == 1
            assert first.inserted == 3
            assert first.errors == 0
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT metric_id, count(*) FROM "
                    "sleep_domain_observation_semantics_v2 GROUP BY metric_id "
                    "ORDER BY metric_id"
                )
                assert cursor.fetchall() == [
                    ("legacy_ambiguous_movement", 1),
                    ("movement_event_count", 1),
                    ("movement_index", 1),
                ]
                cursor.execute(
                    "SELECT pre_normalization_payload_sha256, "
                    "encode(digest(encrypted_payload, 'sha256'), 'hex') "
                    "FROM sleep_domain_raw_inbox"
                )
                assert tuple(map(str, cursor.fetchone())) == before_hashes
                cursor.execute(
                    "SELECT count(*) FROM sleep_domain_canonical_observations "
                    "WHERE observation_type = 'heart_rate'"
                )
                assert cursor.fetchone() == (1,)
                cursor.execute("SAVEPOINT invalid_semantics")
                with pytest.raises(psycopg.errors.CheckViolation):
                    cursor.execute(
                        "INSERT INTO sleep_domain_observation_semantics_v2 ("
                        "observation_id, namespace_id, data_mode, subject_id, "
                        "schema_version, observation_type, metric_id, "
                        "semantic_payload_json, canonical_unit, occurred_at, "
                        "source_kind, provenance_json, semantics_version, "
                        "ontology_version, normalizer_version, semantic_identity, "
                        "transport_receipt_identity, trusted_for_analytics, "
                        "upcast_status, classification_evidence, created_at) "
                        "VALUES ("
                        "'g2b-m3-observation-duplicate', %s, 'live', %s, "
                        "'canonical_observation.v2', 'movement', "
                        "'movement_event_count', '{\"value\":5}'::jsonb, "
                        "'count', %s, 'vendor_derived', '{}'::jsonb, "
                        "'observation_semantics.v2', 'observation_semantics.v2', "
                        "'invalid-test', %s, 'invalid-test', TRUE, 'native_v2', "
                        "'{}'::jsonb, %s)",
                        (NAMESPACE, SUBJECT, AT, "e" * 64, AT),
                    )
                cursor.execute("ROLLBACK TO SAVEPOINT invalid_semantics")
                cursor.execute("SAVEPOINT immutable_semantics")
                with pytest.raises(psycopg.errors.RaiseException):
                    cursor.execute(
                        "UPDATE sleep_domain_observation_semantics_v2 "
                        "SET trusted_for_analytics = FALSE"
                    )
                cursor.execute("ROLLBACK TO SAVEPOINT immutable_semantics")
            second = run_upcast(connection, batch_size=2, max_rows=20)
            assert second.already_classified == 3
            assert second.inserted == 0
            assert second.duplicate_semantic_identity == 1
            assert second.errors == 0
    finally:
        _drop_database(admin_dsn, upgrade_name)
        _drop_database(admin_dsn, fresh_name)
