from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sleepagent.app import BoundedRequestMiddleware
from sleepagent.config import (
    ApiSurface,
    BackendKeyProvider,
    DataMode,
    DeploymentMode,
    ModelMode,
    ObservationSemanticsVersion,
    ProcessRole,
    ProviderMode,
    SleepBackendSettings,
)
from sleepagent.domain.postgres_slice import RawPayloadCipher
from sleepagent.integrations.perceptor.ingestion import (
    WEBHOOK_PATH,
    PerceptorWebhookError,
    PerceptorWebhookService,
    create_perceptor_router,
)
from sleepagent.integrations.perceptor.signing import (
    PUSH_SIGNING_KEY_MODE,
    PUSH_SIGNING_PATH,
    sign_parameters,
)
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
)
from sleepagent.workers.ingestion import build_b3_worker_handlers
from sleepagent.workers.runtime import PostgresDurableWorkStore, WorkContext


pytestmark = pytest.mark.postgres
UTC = timezone.utc
NOW = datetime(2026, 8, 21, 3, 0, tzinfo=UTC)
SECRET = "synthetic-postgres-push-secret"
NAMESPACE = "live:p4d-postgres-proof"
ACCOUNT = "perceptor-p4d-account"
SUBJECT = "p4d-subject"
BINDING = "p4d-device-binding"
PROVIDER_DEVICE_ID = "7000000000000000002"


def _dsn(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} is required")
    return value


def _pool(dsn: str, name: str) -> PsycopgPoolProvider:
    provider = PsycopgPoolProvider.from_dsn(
        dsn,
        configuration=PoolConfiguration(min_size=1, max_size=2),
        application_name=name,
    )
    provider.open()
    return provider


def _api_settings(dsn: str) -> SleepBackendSettings:
    return SleepBackendSettings(
        profile="p4d-postgres-api",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.API,
        data_mode=DataMode.LIVE,
        database_dsn="postgresql://sleepagent_p4d_api@localhost/postgres",
        database_identity="postgres",
        database_role="sleepagent_p4d_api",
        service_principal_id=_dsn("SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL"),
        database_scope=DataMode.LIVE,
        namespace_prefixes=(NAMESPACE,),
        enabled_surfaces=frozenset({ApiSurface.PERCEPTOR_PUSH}),
        signing_key_ref="test:p4d-signing",
        encryption_key_ref="test:p4d-encryption",
        perceptor_client_secret_ref="test:p4d-perceptor",
        perceptor_provider_account_id=ACCOUNT,
        perceptor_namespace_id=NAMESPACE,
        perceptor_namespace_generation=1,
        perceptor_authorization_epoch=1,
        perceptor_freshness_seconds=300,
        raw_retention_seconds=3600,
    )


def _worker_settings(dsn: str) -> SleepBackendSettings:
    return SleepBackendSettings(
        profile="p4d-postgres-worker",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.WORKER,
        data_mode=DataMode.LIVE,
        database_dsn="postgresql://sleepagent_p4d_worker@localhost/postgres",
        database_identity="postgres",
        database_role="sleepagent_p4d_worker",
        service_principal_id=_dsn("SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL"),
        database_scope=DataMode.LIVE,
        namespace_prefixes=(NAMESPACE,),
        enabled_surfaces=frozenset(),
        worker_queues=("ingestion",),
        provider_mode=ProviderMode.DISABLED,
        model_mode=ModelMode.DISABLED,
        signing_key_ref="test:p4d-signing",
        encryption_key_ref="test:p4d-encryption",
        observation_semantics_version=ObservationSemanticsVersion.V2,
    )


def _payload(
    *,
    message_id: str,
    timestamp: datetime = NOW,
    device_id: str = PROVIDER_DEVICE_ID,
    event_type: str = "VitalSignsDataEvent",
    heart_rate: int = 70,
) -> dict[str, object]:
    data = (
        f'{{"HeartRate":{heart_rate},"BreathRate":16,"BodyShake":1,'
        f'"OnBed":1,"ReportTime":"{int(timestamp.timestamp() * 1000)}",'
        '"DateTime":"2026-08-21T11:00:00.000"}'
    )
    if event_type != "VitalSignsDataEvent":
        data = '{"FutureField":"opaque"}'
    return {
        "client_id": "synthetic-postgres-client",
        "version": "2.0",
        "timestamp": int(timestamp.timestamp()),
        "sign_version": "2.0",
        "sign_nonce": f"nonce-{message_id}",
        "sign_method": "HMAC-SHA1",
        "message_id": message_id,
        "product_id": 7000000000000000001,
        "device_id": int(device_id),
        "device_name": "SYNTHETIC-P4D-RADAR",
        "home_id": 7000000000000000003,
        "type": event_type,
        "data": data,
    }


def _raw(**kwargs: Any) -> bytes:
    payload = _payload(**kwargs)
    payload["sign"] = sign_parameters(
        payload,
        client_secret=SECRET,
        signing_path=PUSH_SIGNING_PATH,
        key_mode=PUSH_SIGNING_KEY_MODE,
    )
    return json.dumps(payload, separators=(",", ":")).encode()


def _seed(admin_dsn: str) -> None:
    psycopg = pytest.importorskip("psycopg")
    client_hash = hashlib.sha256(
        b"synthetic-postgres-client"
    ).hexdigest()
    binding_json = {
        "schema_version": "device_binding.v1",
        "data_mode": "live",
        "device_binding_id": BINDING,
        "binding_version": 1,
        "device_id": "internal-radar-device",
        "provider_id": "perceptor",
        "provider_account_id": ACCOUNT,
        "provider_device": {
            "schema_version": "provider_device_identity.v1",
            "provider_device_id": PROVIDER_DEVICE_ID,
            "provider_device_name": "SYNTHETIC-P4D-RADAR",
            "product_id": "7000000000000000001",
            "home_id": "7000000000000000003",
            "native_keys": {},
        },
        "subject_id": SUBJECT,
        "timezone_name": "Asia/Shanghai",
        "effective_from": (NOW - timedelta(days=1)).isoformat(),
        "effective_until": None,
        "status": "active",
        "changed_by_actor_id": "p4d-local-proof",
        "change_reason": "synthetic PostgreSQL integration proof",
        "recorded_at": (NOW - timedelta(days=1)).isoformat(),
    }
    with psycopg.connect(admin_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO backend_namespaces (namespace_id, data_mode, "
                "current_generation, status, synthetic_non_release) VALUES "
                "(%s, 'live', 1, 'active', FALSE) ON CONFLICT DO NOTHING",
                (NAMESPACE,),
            )
            cursor.execute(
                "INSERT INTO backend_namespace_generations (namespace_id, "
                "data_mode, generation, status, configuration_sha256) VALUES "
                "(%s, 'live', 1, 'active', %s) ON CONFLICT DO NOTHING",
                (NAMESPACE, "a" * 64),
            )
            cursor.execute(
                "INSERT INTO backend_subjects (namespace_id, data_mode, "
                "subject_id, timezone_name, status) VALUES "
                "(%s, 'live', %s, 'Asia/Shanghai', 'active') "
                "ON CONFLICT DO NOTHING",
                (NAMESPACE, SUBJECT),
            )
            cursor.execute(
                "INSERT INTO backend_subject_epochs (namespace_id, data_mode, "
                "subject_id, authorization_epoch, privacy_epoch, "
                "retrieval_policy_epoch) VALUES (%s, 'live', %s, 1, 1, 1) "
                "ON CONFLICT DO NOTHING",
                (NAMESPACE, SUBJECT),
            )
            for grant_id, principal, purpose, handlers in (
                (
                    "grant-p4d-api",
                    _dsn("SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL"),
                    "perceptor_ingress",
                    [],
                ),
                (
                    "grant-p4d-worker",
                    _dsn("SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL"),
                    "worker",
                    ["normalization"],
                ),
            ):
                cursor.execute(
                    "INSERT INTO backend_principal_grants (grant_id, "
                    "principal_id, namespace_id, data_mode, purpose, "
                    "scopes_json, allowed_handlers_json, authorization_epoch, "
                    "status, valid_from) VALUES (%s, %s, %s, 'live', %s, "
                    "'[]'::jsonb, %s::jsonb, 1, 'active', %s) "
                    "ON CONFLICT DO NOTHING",
                    (
                        grant_id,
                        principal,
                        NAMESPACE,
                        purpose,
                        json.dumps(handlers),
                        NOW - timedelta(days=1),
                    ),
                )
            cursor.execute(
                "INSERT INTO sleep_domain_provider_accounts (namespace_id, "
                "data_mode, provider_account_id, provider_id, "
                "configuration_fingerprint, status, account_metadata_json, "
                "created_at) VALUES (%s, 'live', %s, 'perceptor', %s, "
                "'active', %s::jsonb, %s) ON CONFLICT DO NOTHING",
                (
                    NAMESPACE,
                    ACCOUNT,
                    "b" * 64,
                    json.dumps({"client_id_sha256": client_hash}),
                    NOW - timedelta(days=1),
                ),
            )
            cursor.execute(
                "INSERT INTO sleep_domain_device_bindings (device_binding_id, "
                "namespace_id, data_mode, binding_version, device_id, "
                "provider_id, provider_account_id, subject_id, timezone_name, "
                "effective_from, status, binding_json, recorded_at) VALUES "
                "(%s, %s, 'live', 1, 'internal-radar-device', 'perceptor', "
                "%s, %s, 'Asia/Shanghai', %s, 'active', %s::jsonb, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    BINDING,
                    NAMESPACE,
                    ACCOUNT,
                    SUBJECT,
                    NOW - timedelta(days=1),
                    json.dumps(binding_json),
                    NOW - timedelta(days=1),
                ),
            )
        connection.commit()


def test_production_push_to_canonical_postgres_boundary() -> None:
    admin_dsn = _dsn("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _dsn("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    worker_dsn = _dsn("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    _seed(admin_dsn)
    psycopg = pytest.importorskip("psycopg")

    api_pool = _pool(api_dsn, "p4d-api-proof")
    worker_pool = _pool(worker_dsn, "p4d-worker-proof")
    try:
        api_uow = UnitOfWorkFactory(api_pool)
        worker_uow = UnitOfWorkFactory(worker_pool)
        key = BackendKeyProvider(DeploymentMode.TEST).encryption_key(
            "test:p4d-encryption"
        )
        service = PerceptorWebhookService(
            _api_settings(api_dsn),
            api_uow,
            client_secret=SECRET,
            cipher=RawPayloadCipher(key, key_id="test:p4d-encryption"),
            now_factory=lambda: NOW,
        )
        raw = _raw(message_id="p4d-valid-1")
        app = FastAPI()
        app.include_router(create_perceptor_router(service))
        app.add_middleware(
            BoundedRequestMiddleware,
            max_compressed_bytes=4096,
            max_decompressed_bytes=4096,
            max_json_depth=16,
            max_json_members=200,
            request_timeout_seconds=2,
            data_mode="live",
        )
        response = TestClient(app).request(
            "POST",
            WEBHOOK_PATH,
            content=raw,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["success"] is True
        duplicate = service.accept(
            raw,
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert duplicate.duplicate is True
        assert duplicate.raw_ingress_record_id is not None

        worker_settings = _worker_settings(worker_dsn)
        store = PostgresDurableWorkStore(
            worker_settings,
            worker_uow,
            purpose="worker",
        )
        claim = store.claim(
            queue="ingestion",
            worker_instance="p4d-worker-instance",
            lease_seconds=60,
        )
        assert claim is not None
        handler = build_b3_worker_handlers(worker_settings)["ingestion"]
        result = handler(WorkContext(claim, store, threading.Event()))
        assert result.disposition.value == "succeeded"
        assert result.finalization_mode.value == "handler_owned"
        assert store.claim(
            queue="ingestion",
            worker_instance="p4d-worker-retry",
            lease_seconds=60,
        ) is None

        unbound = service.accept(
            _raw(message_id="p4d-unbound-1", device_id="7000000000000099999"),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert unbound.disposition == "quarantined_unbound"
        assert unbound.subject_id is None
        unknown = service.accept(
            _raw(message_id="p4d-unknown-1", event_type="FutureVendorEvent"),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert unknown.disposition == "quarantined_unknown"
        conflict = service.accept(
            _raw(message_id="p4d-valid-1", heart_rate=71),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert conflict.disposition == "quarantined_conflict"
        with pytest.raises(PerceptorWebhookError, match="stale_request"):
            service.accept(
                _raw(
                    message_id="p4d-stale-1",
                    timestamp=NOW - timedelta(hours=1),
                ),
                content_type="application/json",
                request_path=WEBHOOK_PATH,
            )

        with psycopg.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*), count(*) FILTER (WHERE "
                    "pre_normalization_payload_sha256 = %s), "
                    "bool_and(encrypted_payload <> %s) FROM "
                    "sleep_domain_raw_inbox WHERE namespace_id = %s",
                    (hashlib.sha256(raw).hexdigest(), raw, NAMESPACE),
                )
                assert cursor.fetchone() == (4, 1, True)
                cursor.execute(
                    "SELECT count(*), count(DISTINCT idempotency_key) FROM "
                    "sleep_domain_canonical_observations WHERE namespace_id = %s",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (4, 4)
                cursor.execute(
                    "SELECT count(*), count(DISTINCT semantic_identity), "
                    "count(*) FILTER (WHERE metric_id = 'movement_index') "
                    "FROM sleep_domain_observation_semantics_v2 "
                    "WHERE namespace_id = %s",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (4, 4, 1)
                cursor.execute(
                    "SELECT has_table_privilege(%s, %s, 'SELECT'), "
                    "has_table_privilege(%s, %s, 'INSERT'), "
                    "has_table_privilege(%s, %s, 'INSERT'), "
                    "has_table_privilege(%s, %s, 'SELECT')",
                    (
                        _dsn("SLEEPAGENT_TEST_POSTGRES_WORKER_USER"),
                        "sleep_domain_observation_semantics_v2",
                        _dsn("SLEEPAGENT_TEST_POSTGRES_WORKER_USER"),
                        "sleep_domain_observation_semantics_v2",
                        _dsn("SLEEPAGENT_TEST_POSTGRES_API_USER"),
                        "sleep_domain_observation_semantics_v2",
                        _dsn("SLEEPAGENT_TEST_POSTGRES_DEMO_USER"),
                        "sleep_domain_observation_semantics_v2",
                    ),
                )
                assert cursor.fetchone() == (True, True, False, False)
                with psycopg.connect(worker_dsn) as unscoped_worker:
                    with unscoped_worker.cursor() as worker_cursor:
                        worker_cursor.execute(
                            "SELECT count(*) FROM "
                            "sleep_domain_observation_semantics_v2"
                        )
                        assert worker_cursor.fetchone() == (0,)
                cursor.execute(
                    "SELECT count(*) FROM sleep_domain_normalization_work "
                    "WHERE namespace_id = %s AND status = 'succeeded'",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (1,)
                cursor.execute(
                    "SELECT count(*) FROM sleep_domain_quarantine "
                    "WHERE namespace_id = %s",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (3,)
                cursor.execute(
                    "SELECT count(*) FROM backend_invocations WHERE "
                    "namespace_id = %s",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (0,)
                cursor.execute(
                    "SELECT count(*) FROM sleep_domain_operations WHERE "
                    "namespace_id = %s",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (0,)
    finally:
        worker_pool.close()
        api_pool.close()
