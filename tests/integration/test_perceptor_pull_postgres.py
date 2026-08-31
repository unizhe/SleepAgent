from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

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
from sleepagent.domain.contracts import DeviceBinding, DeviceBindingStatus
from sleepagent.infrastructure.postgres_sleep_slice import (
    NormalizationLease,
    RawPayloadCipher,
)
from sleepagent.integrations.perceptor.client import (
    HISTORY_ENDPOINT,
    REALTIME_READ_ENDPOINT,
    SLEEP_REPORT_ENDPOINT,
    PlatformEvidencedRead,
    PlatformRawResponseEvidence,
)
from sleepagent.integrations.perceptor.ingestion import (
    WEBHOOK_PATH,
    PerceptorWebhookService,
)
from sleepagent.integrations.perceptor.pull_ingestion import (
    DurablePerceptorPullIngress,
    PerceptorLiveNormalizationDispatcher,
    PerceptorPullBackfillRunner,
    PerceptorPullNormalizationProcessor,
    PullRequestCoordinates,
)
from sleepagent.integrations.perceptor.signing import (
    PUSH_SIGNING_KEY_MODE,
    PUSH_SIGNING_PATH,
    sign_parameters,
)
from sleepagent.persistence.migrations import (
    EXPECTED_MIGRATION_IDENTITIES,
    LATEST_SCHEMA_VERSION,
    MIGRATION_MANIFEST_SHA256,
)
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
)
from sleepagent.process import DatabaseAttestation, SleepBackendRuntime
from sleepagent.workers.ingestion import NormalizationWorkHandlerAdapter
from sleepagent.workers.runtime import (
    DurableWorkerRuntime,
    PostgresDurableWorkStore,
    WorkContext,
    exact_worker_scope,
)


pytestmark = pytest.mark.postgres
UTC = timezone.utc
NOW = datetime(2026, 8, 23, 3, 0, tzinfo=UTC)
SECRET = "synthetic-postgres-pull-secret"
CLIENT_ID = "synthetic-postgres-pull-client"
NAMESPACE = "live:p4d2-b2-pull-postgres-proof"
ACCOUNT = "perceptor-p4d2-b2-account"
SUBJECT = "p4d2-b2-subject"
BINDING_ID = "p4d2-b2-device-binding"
PROVIDER_DEVICE_ID = "7200000000000000002"
PROVIDER_DEVICE_NAME = "SYNTHETIC-P4D2-B2-RADAR"


class _SimulatedCrash(RuntimeError):
    pass


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


def _api_settings() -> SleepBackendSettings:
    return SleepBackendSettings(
        profile="p4d2-b2-postgres-api",
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
        signing_key_ref="test:p4d2-b2-signing",
        encryption_key_ref="test:p4d2-b2-encryption",
        perceptor_client_secret_ref="test:p4d2-b2-perceptor",
        perceptor_provider_account_id=ACCOUNT,
        perceptor_namespace_id=NAMESPACE,
        perceptor_namespace_generation=1,
        perceptor_authorization_epoch=1,
        perceptor_freshness_seconds=300,
        raw_retention_seconds=3600,
    )


def _worker_settings() -> SleepBackendSettings:
    return SleepBackendSettings(
        profile="p4d2-b2-postgres-worker",
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
        signing_key_ref="test:p4d2-b2-signing",
        encryption_key_ref="test:p4d2-b2-encryption",
    )


def _binding_json() -> dict[str, object]:
    return {
        "schema_version": "device_binding.v1",
        "data_mode": "live",
        "device_binding_id": BINDING_ID,
        "binding_version": 1,
        "device_id": "internal-p4d2-b2-radar",
        "provider_id": "perceptor",
        "provider_account_id": ACCOUNT,
        "provider_device": {
            "schema_version": "provider_device_identity.v1",
            "provider_device_id": PROVIDER_DEVICE_ID,
            "provider_device_name": PROVIDER_DEVICE_NAME,
            "product_id": "7200000000000000001",
            "home_id": "7200000000000000003",
            "native_keys": {},
        },
        "subject_id": SUBJECT,
        "timezone_name": "Asia/Shanghai",
        "effective_from": (NOW - timedelta(days=2)).isoformat(),
        "effective_until": None,
        "status": "active",
        "changed_by_actor_id": "p4d2-b2-local-proof",
        "change_reason": "synthetic PostgreSQL reconciliation proof",
        "recorded_at": (NOW - timedelta(days=1)).isoformat(),
    }


def _seed(admin_dsn: str) -> DeviceBinding:
    psycopg = pytest.importorskip("psycopg")
    binding_json = _binding_json()
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
                    "grant-p4d2-b2-api",
                    _dsn("SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL"),
                    "perceptor_ingress",
                    [],
                ),
                (
                    "grant-p4d2-b2-worker",
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
                    json.dumps(
                        {"client_id_sha256": hashlib.sha256(CLIENT_ID.encode()).hexdigest()}
                    ),
                    NOW - timedelta(days=2),
                ),
            )
            cursor.execute(
                "INSERT INTO sleep_domain_device_bindings (device_binding_id, "
                "namespace_id, data_mode, binding_version, device_id, "
                "provider_id, provider_account_id, subject_id, timezone_name, "
                "effective_from, status, binding_json, recorded_at) VALUES "
                "(%s, %s, 'live', 1, 'internal-p4d2-b2-radar', 'perceptor', "
                "%s, %s, 'Asia/Shanghai', %s, 'active', %s::jsonb, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    BINDING_ID,
                    NAMESPACE,
                    ACCOUNT,
                    SUBJECT,
                    binding_json["effective_from"],
                    json.dumps(binding_json),
                    NOW - timedelta(days=1),
                ),
            )
        connection.commit()
    return DeviceBinding.model_validate(binding_json)


def _reassign_binding(
    admin_dsn: str,
    binding: DeviceBinding,
    *,
    effective_at: datetime,
) -> DeviceBinding:
    """Create a real temporal version instead of mutating historical identity."""

    psycopg = pytest.importorskip("psycopg")
    ended = binding.model_copy(
        update={
            "effective_until": effective_at,
            "status": DeviceBindingStatus.ENDED,
            "changed_by_actor_id": "p4d2-b2-temporal-proof",
            "change_reason": "synthetic reassignment boundary",
        }
    )
    reassigned = binding.model_copy(
        update={
            "device_binding_id": f"{binding.device_binding_id}-v2",
            "binding_version": binding.binding_version + 1,
            "effective_from": effective_at,
            "effective_until": None,
            "status": DeviceBindingStatus.ACTIVE,
            "changed_by_actor_id": "p4d2-b2-temporal-proof",
            "change_reason": "synthetic reassignment version",
            "recorded_at": effective_at,
        }
    )
    with psycopg.connect(admin_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE public.sleep_domain_device_bindings AS binding_row
                SET status = 'ended', effective_until = %s, ended_at = %s,
                    binding_json = %s::jsonb,
                    cas_version = binding_row.cas_version + 1
                WHERE device_binding_id = %s AND cas_version = 0
                """,
                (
                    effective_at,
                    effective_at,
                    ended.model_dump_json(),
                    binding.device_binding_id,
                ),
            )
            assert cursor.rowcount == 1
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_device_bindings (
                  device_binding_id, namespace_id, data_mode, binding_version,
                  device_id, provider_id, provider_account_id, subject_id,
                  timezone_name, effective_from, status, binding_json,
                  recorded_at, cas_version, supersedes_device_binding_id
                ) VALUES (
                  %s, %s, 'live', %s, %s, %s, %s, %s, %s, %s, 'active',
                  %s::jsonb, %s, 0, %s
                )
                """,
                (
                    reassigned.device_binding_id,
                    NAMESPACE,
                    reassigned.binding_version,
                    reassigned.device_id,
                    reassigned.provider_id,
                    reassigned.provider_account_id,
                    reassigned.subject_id,
                    reassigned.timezone_name,
                    reassigned.effective_from,
                    reassigned.model_dump_json(),
                    reassigned.recorded_at,
                    binding.device_binding_id,
                ),
            )
    return reassigned


def _push_raw(*, message_id: str, heart_rate: int = 70) -> bytes:
    payload: dict[str, object] = {
        "client_id": CLIENT_ID,
        "version": "2.0",
        "timestamp": int(NOW.timestamp()),
        "sign_version": "2.0",
        "sign_nonce": f"nonce-{message_id}",
        "sign_method": "HMAC-SHA1",
        "message_id": message_id,
        "product_id": 7200000000000000001,
        "device_id": int(PROVIDER_DEVICE_ID),
        "device_name": PROVIDER_DEVICE_NAME,
        "home_id": 7200000000000000003,
        "type": "VitalSignsDataEvent",
        "data": (
            f'{{"HeartRate":{heart_rate},"BreathRate":16,"BodyShake":1,'
            f'"OnBed":1,"ReportTime":"{int(NOW.timestamp() * 1000)}",'
            '"DateTime":"2026-08-23T11:00:00.000"}'
        ),
    }
    payload["sign"] = sign_parameters(
        payload,
        client_secret=SECRET,
        signing_path=PUSH_SIGNING_PATH,
        key_mode=PUSH_SIGNING_KEY_MODE,
    )
    return json.dumps(payload, separators=(",", ":")).encode()


def _history_data(*, local_send_time: str, heart_rate: int = 70) -> list[dict[str, str]]:
    return [
        {
            # The recorded-real history contract uses the bound device name in
            # this vendor field despite its ``device_id`` label.
            "device_id": PROVIDER_DEVICE_NAME,
            "heart_rate": str(heart_rate),
            "breath_rate": "16",
            "body_shake": "1",
            "create_time": local_send_time,
            "send_time": local_send_time,
        }
    ]


def _sleep_no_data() -> dict[str, object]:
    return {
        "sleep_profile": {"sleep_duration": None},
        "sleep_stage_list": None,
        "heart_rate_data": None,
        "heart_rate_avg": 0,
        "breathe_data": None,
        "breathe_avg": 0,
        "body_shake_data": [],
        "sum_body_shake_times": 0,
        "getups": [],
    }


def _read(
    endpoint: str,
    data: Any,
    *,
    requested_at: datetime,
    received_at: datetime,
    envelope_nonce: str,
) -> PlatformEvidencedRead[Any]:
    raw = json.dumps(
        {
            "success": True,
            "code": 200,
            "message": "success",
            "request_id": envelope_nonce,
            "data": data,
        },
        separators=(",", ":"),
    ).encode()
    evidence = PlatformRawResponseEvidence(
        endpoint=endpoint,
        requested_at=requested_at,
        received_at=received_at,
        http_status=200,
        raw_body=raw,
    )
    return PlatformEvidencedRead(endpoint=endpoint, data=data, evidence=evidence)


def _lease(claim: Any) -> NormalizationLease:
    return NormalizationLease(
        work_id=claim.work_id,
        lease_generation=claim.lease_generation,
        fencing_token=claim.fencing_token,
        worker_instance=claim.worker_instance,
    )


def _claim_scope(store: PostgresDurableWorkStore, worker: str) -> tuple[Any, Any]:
    claim = store.claim(queue="ingestion", worker_instance=worker, lease_seconds=60)
    assert claim is not None
    context = WorkContext(claim, store, threading.Event())
    return claim, exact_worker_scope(context, allowed_handler="normalization")


def _process_next(
    store: PostgresDurableWorkStore,
    worker_uow: UnitOfWorkFactory[Any],
    cipher: RawPayloadCipher,
    *,
    worker: str,
    observation_semantics_version: ObservationSemanticsVersion = (
        ObservationSemanticsVersion.V1
    ),
) -> Any:
    claim, scope = _claim_scope(store, worker)
    return PerceptorLiveNormalizationDispatcher(
        worker_uow,
        cipher=cipher,
        observation_semantics_version=observation_semantics_version,
    ).process(scope, _lease(claim))


def _durable_ingestion_runtime(
    *,
    pool: PsycopgPoolProvider,
    worker_uow: UnitOfWorkFactory[Any],
    store: PostgresDurableWorkStore,
    processor: PerceptorPullNormalizationProcessor,
    worker_instance: str,
) -> DurableWorkerRuntime:
    settings = _worker_settings()
    handler = NormalizationWorkHandlerAdapter(processor=processor)
    backend_runtime = SleepBackendRuntime(
        settings,
        pool=pool,
        uow_factory=worker_uow,
        attestor=lambda: DatabaseAttestation(
            database_identity=settings.database_identity,
            database_role=settings.database_role,
            schema_version=LATEST_SCHEMA_VERSION,
            migrations_clean=True,
            migration_manifest_sha256=MIGRATION_MANIFEST_SHA256,
            applied_migration_identities=EXPECTED_MIGRATION_IDENTITIES,
        ),
        worker_handlers={"ingestion": handler},
    )
    return DurableWorkerRuntime(
        backend_runtime,
        store=store,
        handlers={"ingestion": handler},
        lease_seconds=60,
        heartbeat_interval_seconds=10.0,
        worker_instance=worker_instance,
    )


def _count(admin_dsn: str, table: str) -> int:
    psycopg = pytest.importorskip("psycopg")
    allowed = {
        "sleep_domain_canonical_observations",
        "sleep_domain_observation_acquisitions",
        "sleep_domain_observation_conflicts",
        "sleep_domain_observation_semantics_v2",
        "sleep_domain_night_episodes",
    }
    assert table in allowed
    with psycopg.connect(admin_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT count(*) FROM {table} WHERE namespace_id = %s",
                (NAMESPACE,),
            )
            return int(cursor.fetchone()[0])


def test_push_pull_reconciliation_and_crash_replay_postgres() -> None:
    admin_dsn = _dsn("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _dsn("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    worker_dsn = _dsn("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    binding = _seed(admin_dsn)
    psycopg = pytest.importorskip("psycopg")

    api_pool = _pool(api_dsn, "p4d2-b2-pull-api-proof")
    worker_pool = _pool(worker_dsn, "p4d2-b2-pull-worker-proof")
    try:
        api_uow = UnitOfWorkFactory(api_pool)
        worker_uow = UnitOfWorkFactory(worker_pool)
        key = BackendKeyProvider(DeploymentMode.TEST).encryption_key(
            "test:p4d2-b2-encryption"
        )
        cipher = RawPayloadCipher(key, key_id="test:p4d2-b2-encryption")
        webhook = PerceptorWebhookService(
            _api_settings(),
            api_uow,
            client_secret=SECRET,
            cipher=cipher,
            now_factory=lambda: NOW,
        )
        ingress = DurablePerceptorPullIngress(
            _api_settings(),
            api_uow,
            client_id_sha256=hashlib.sha256(CLIENT_ID.encode()).hexdigest(),
            cipher=cipher,
        )
        initial_plan = ingress.plan_history_window(
            binding=binding,
            requested_start_at=NOW - timedelta(minutes=1),
            requested_end_at=NOW + timedelta(minutes=1),
        )
        assert initial_plan.resumed_from_checkpoint is False
        assert initial_plan.window_start_at == NOW - timedelta(minutes=1)
        assert initial_plan.window_end_at == NOW + timedelta(minutes=1)

        # The API cannot read Worker-owned checkpoints directly; it can only
        # execute the timestamp-only, authority-resolving planner.
        with psycopg.connect(api_dsn) as api_connection:
            with api_connection.cursor() as api_cursor:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    api_cursor.execute(
                        "SELECT cursor_at FROM sleep_domain_pull_checkpoints"
                    )
            api_connection.rollback()
        store = PostgresDurableWorkStore(
            _worker_settings(),
            worker_uow,
            purpose="worker",
            base_retry_seconds=0.01,
            max_retry_seconds=0.01,
        )

        pushed = webhook.accept(
            _push_raw(message_id="p4d2-b2-push-1"),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert pushed.disposition == "accepted"
        push_result = _process_next(
            store, worker_uow, cipher, worker="p4d2-b2-push-worker"
        )
        assert push_result.canonical_created_count == 4

        exact_data = _history_data(local_send_time="2026-08-23T11:00:00")
        exact_coordinates = PullRequestCoordinates(
            endpoint=HISTORY_ENDPOINT,
            window_start_at=NOW - timedelta(minutes=1),
            window_end_at=NOW + timedelta(minutes=1),
        )
        exact_ingress = ingress.accept(
            _read(
                HISTORY_ENDPOINT,
                exact_data,
                requested_at=NOW + timedelta(seconds=1),
                received_at=NOW + timedelta(seconds=2),
                envelope_nonce="exact-1",
            ),
            binding=binding,
            coordinates=exact_coordinates,
        )
        assert exact_ingress.disposition == "accepted"
        exact_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="p4d2-b2-exact-pull-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        )
        assert exact_result.canonical_created_count == 0
        assert exact_result.push_pull_overlap_count == 3
        assert exact_result.conflict_created_count == 0
        assert exact_result.checkpoint_advanced is True
        assert _count(admin_dsn, "sleep_domain_canonical_observations") == 4
        assert _count(
            admin_dsn, "sleep_domain_observation_semantics_v2"
        ) == 3
        with psycopg.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM (SELECT fact_slot_key, value_sha256 "
                    "FROM sleep_domain_observation_acquisitions "
                    "WHERE namespace_id = %s GROUP BY fact_slot_key, value_sha256 "
                    "HAVING bool_or(acquisition_channel = 'PUSH') AND "
                    "bool_or(acquisition_channel = 'PULL')) AS overlap",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (3,)

        overlapping_coordinates = PullRequestCoordinates(
            endpoint=HISTORY_ENDPOINT,
            window_start_at=NOW - timedelta(seconds=30),
            window_end_at=NOW + timedelta(seconds=90),
        )
        overlapping_ingress = ingress.accept(
            _read(
                HISTORY_ENDPOINT,
                exact_data,
                requested_at=NOW + timedelta(seconds=2),
                received_at=NOW + timedelta(seconds=3),
                envelope_nonce="overlapping-window-1",
            ),
            binding=binding,
            coordinates=overlapping_coordinates,
        )
        assert overlapping_ingress.disposition == "accepted"
        overlapping_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="p4d2-b2-overlap-window-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        )
        assert overlapping_result.canonical_created_count == 0
        assert overlapping_result.push_pull_overlap_count == 3
        assert overlapping_result.conflict_created_count == 0
        assert _count(admin_dsn, "sleep_domain_canonical_observations") == 4

        work_count_before_repeat: int
        with psycopg.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM sleep_domain_normalization_work "
                    "WHERE namespace_id = %s",
                    (NAMESPACE,),
                )
                work_count_before_repeat = int(cursor.fetchone()[0])
        repeated = ingress.accept(
            _read(
                HISTORY_ENDPOINT,
                exact_data,
                requested_at=NOW + timedelta(seconds=3),
                received_at=NOW + timedelta(seconds=4),
                envelope_nonce="exact-response-envelope-varied",
            ),
            binding=binding,
            coordinates=exact_coordinates,
        )
        assert repeated.disposition == "semantic_duplicate"
        assert repeated.duplicate is True
        with psycopg.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM sleep_domain_normalization_work "
                    "WHERE namespace_id = %s",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (work_count_before_repeat,)

        conflict_ingress = ingress.accept(
            _read(
                HISTORY_ENDPOINT,
                _history_data(
                    local_send_time="2026-08-23T11:00:00", heart_rate=71
                ),
                requested_at=NOW + timedelta(seconds=5),
                received_at=NOW + timedelta(seconds=6),
                envelope_nonce="conflict-1",
            ),
            binding=binding,
            coordinates=exact_coordinates,
        )
        assert conflict_ingress.disposition == "accepted"
        conflict_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="p4d2-b2-conflict-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        )
        assert conflict_result.canonical_created_count == 1
        assert conflict_result.push_pull_overlap_count == 2
        assert conflict_result.conflict_created_count == 1
        assert conflict_result.checkpoint_advanced is False
        assert _count(admin_dsn, "sleep_domain_observation_conflicts") == 1

        realtime_data = {
            "endTime": int((NOW + timedelta(minutes=4)).timestamp() * 1000),
            "HeartRate": "68",
            "BreathRate": "15",
            "probStatus": 5,
            "time": str(int((NOW + timedelta(minutes=2)).timestamp() * 1000)),
            "timeFormat": "2026-08-23 11:02:00",
        }
        before_realtime = _count(
            admin_dsn, "sleep_domain_canonical_observations"
        )
        realtime_ingress = ingress.accept(
            _read(
                REALTIME_READ_ENDPOINT,
                realtime_data,
                requested_at=NOW + timedelta(minutes=2),
                received_at=NOW + timedelta(minutes=2, seconds=1),
                envelope_nonce="realtime-1",
            ),
            binding=binding,
            coordinates=PullRequestCoordinates(endpoint=REALTIME_READ_ENDPOINT),
        )
        assert realtime_ingress.disposition == "accepted"
        realtime_result = _process_next(
            store, worker_uow, cipher, worker="p4d2-b2-realtime-worker"
        )
        assert realtime_result.canonical_created_count == 3
        repeated_realtime = ingress.accept(
            _read(
                REALTIME_READ_ENDPOINT,
                realtime_data,
                requested_at=NOW + timedelta(minutes=2, seconds=3),
                received_at=NOW + timedelta(minutes=2, seconds=4),
                envelope_nonce="realtime-envelope-varied",
            ),
            binding=binding,
            coordinates=PullRequestCoordinates(endpoint=REALTIME_READ_ENDPOINT),
        )
        assert repeated_realtime.disposition == "semantic_duplicate"
        assert repeated_realtime.duplicate is True
        assert (
            _count(admin_dsn, "sleep_domain_canonical_observations")
            == before_realtime + 3
        )

        before_backfill_canonical = _count(
            admin_dsn, "sleep_domain_canonical_observations"
        )
        before_backfill_acquisitions = _count(
            admin_dsn, "sleep_domain_observation_acquisitions"
        )
        backfill_coordinates = PullRequestCoordinates(
            endpoint=HISTORY_ENDPOINT,
            window_start_at=NOW + timedelta(minutes=4),
            window_end_at=NOW + timedelta(minutes=6),
        )
        backfill_ingress = ingress.accept(
            _read(
                HISTORY_ENDPOINT,
                _history_data(local_send_time="2026-08-23T11:05:00"),
                requested_at=NOW + timedelta(minutes=5, seconds=1),
                received_at=NOW + timedelta(minutes=5, seconds=2),
                envelope_nonce="backfill-crash-1",
            ),
            binding=binding,
            coordinates=backfill_coordinates,
        )
        assert backfill_ingress.disposition == "accepted"
        assert (
            _count(admin_dsn, "sleep_domain_canonical_observations")
            == before_backfill_canonical
        )
        with psycopg.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status FROM sleep_domain_normalization_work "
                    "WHERE work_id = %s",
                    (backfill_ingress.normalization_work_id,),
                )
                assert cursor.fetchone() == ("pending",)
                cursor.execute(
                    "SELECT cursor_at FROM sleep_domain_pull_checkpoints "
                    "WHERE namespace_id = %s AND data_surface = 'history'",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (
                    overlapping_coordinates.window_end_at,
                )
        crash_work_id = backfill_ingress.normalization_work_id
        assert crash_work_id is not None

        def fail_after_commit(phase: str) -> None:
            assert phase == "after_reconciliation_commit"
            raise _SimulatedCrash("simulated crash after canonical reconciliation")

        crash_processor = PerceptorPullNormalizationProcessor(
            worker_uow,
            cipher=cipher,
            fault_injector=fail_after_commit,
        )
        crash_runtime = _durable_ingestion_runtime(
            pool=worker_pool,
            worker_uow=worker_uow,
            store=store,
            processor=crash_processor,
            worker_instance="p4d2-b2-crash-worker",
        )
        assert crash_runtime.run_once() is True

        assert (
            _count(admin_dsn, "sleep_domain_canonical_observations")
            == before_backfill_canonical + 3
        )
        assert (
            _count(admin_dsn, "sleep_domain_observation_acquisitions")
            == before_backfill_acquisitions + 3
        )
        with psycopg.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status, attempt_count, lease_generation, "
                    "work_json ->> 'reconciliation_committed', last_error_code, "
                    "lease_expires_at, worker_instance "
                    "FROM sleep_domain_normalization_work WHERE work_id = %s",
                    (crash_work_id,),
                )
                assert cursor.fetchone() == (
                    "retry",
                    1,
                    1,
                    "true",
                    "unclassified_normalization_processor_failure",
                    None,
                    None,
                )
                cursor.execute(
                    "SELECT cursor_at FROM sleep_domain_pull_checkpoints "
                    "WHERE namespace_id = %s AND data_surface = 'history'",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (
                    overlapping_coordinates.window_end_at,
                )

        replay_runtime = _durable_ingestion_runtime(
            pool=worker_pool,
            worker_uow=worker_uow,
            store=store,
            processor=PerceptorPullNormalizationProcessor(
                worker_uow, cipher=cipher
            ),
            worker_instance="p4d2-b2-replay-worker",
        )
        replayed = False
        retry_deadline = time.monotonic() + 1.0
        while time.monotonic() < retry_deadline:
            if replay_runtime.run_once():
                replayed = True
                break
            time.sleep(0.01)
        assert replayed is True
        assert (
            _count(admin_dsn, "sleep_domain_canonical_observations")
            == before_backfill_canonical + 3
        )
        assert (
            _count(admin_dsn, "sleep_domain_observation_acquisitions")
            == before_backfill_acquisitions + 3
        )
        resumed_plan = ingress.plan_history_window(
            binding=binding,
            requested_start_at=NOW + timedelta(minutes=20),
            requested_end_at=NOW + timedelta(minutes=21),
        )
        assert resumed_plan.resumed_from_checkpoint is True
        assert resumed_plan.checkpoint_cursor_at == (
            backfill_coordinates.window_end_at
        )
        assert resumed_plan.lateness_watermark_at == (
            backfill_coordinates.window_end_at - timedelta(seconds=3)
        )
        assert resumed_plan.window_start_at == resumed_plan.lateness_watermark_at
        assert resumed_plan.window_end_at == NOW + timedelta(minutes=21)
        with psycopg.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT work.status, work.attempt_count, "
                    "work.lease_generation, work.last_error_code, "
                    "checkpoint.cursor_at, checkpoint.lateness_watermark_at, "
                    "checkpoint.last_canonical_observation_id, "
                    "observation.observation_id "
                    "FROM sleep_domain_normalization_work AS work "
                    "JOIN sleep_domain_pull_checkpoints AS checkpoint "
                    "ON checkpoint.last_normalization_work_id = work.work_id "
                    "LEFT JOIN sleep_domain_canonical_observations AS observation "
                    "ON observation.observation_id = "
                    "checkpoint.last_canonical_observation_id "
                    "WHERE work.work_id = %s",
                    (crash_work_id,),
                )
                row = cursor.fetchone()
                assert row[:6] == (
                    "succeeded",
                    2,
                    2,
                    None,
                    backfill_coordinates.window_end_at,
                    backfill_coordinates.window_end_at - timedelta(seconds=3),
                )
                assert row[6] is not None
                assert row[7] == row[6]

        class _HistoryMustNotBeCalled:
            call_count = 0

            def get_history_evidenced(
                self,
                *,
                device_names: tuple[str, ...],
                start_at: datetime,
                end_at: datetime,
            ) -> PlatformEvidencedRead[Any]:
                del device_names, start_at, end_at
                self.call_count += 1
                raise AssertionError(
                    "an already-covered checkpoint window reached the vendor"
                )

        with psycopg.connect(admin_dsn) as connection:
            counts_before_noop = connection.execute(
                "SELECT "
                "(SELECT count(*) FROM sleep_domain_raw_inbox "
                "WHERE namespace_id = %s), "
                "(SELECT count(*) FROM sleep_domain_normalization_work "
                "WHERE namespace_id = %s), "
                "(SELECT count(*) FROM sleep_domain_canonical_observations "
                "WHERE namespace_id = %s), "
                "(SELECT count(*) FROM sleep_domain_pull_checkpoints "
                "WHERE namespace_id = %s), "
                "(SELECT cursor_at FROM sleep_domain_pull_checkpoints "
                "WHERE namespace_id = %s AND data_surface = 'history')",
                (NAMESPACE,) * 5,
            ).fetchone()
        no_call_client = _HistoryMustNotBeCalled()
        backfill_runner = PerceptorPullBackfillRunner(
            no_call_client,  # type: ignore[arg-type]
            ingress,
            binding,
        )
        first_checkpoint_noop = backfill_runner.pull_history(
            start_at=backfill_coordinates.window_start_at,
            end_at=backfill_coordinates.window_end_at,
        )
        second_checkpoint_noop = backfill_runner.pull_history(
            start_at=backfill_coordinates.window_start_at,
            end_at=backfill_coordinates.window_end_at,
        )
        assert first_checkpoint_noop.disposition == "checkpoint_already_advanced"
        assert first_checkpoint_noop.duplicate is True
        assert first_checkpoint_noop.raw_ingress_record_id is None
        assert first_checkpoint_noop.normalization_work_id is None
        assert first_checkpoint_noop.response_semantic_sha256 is None
        assert second_checkpoint_noop == first_checkpoint_noop
        assert no_call_client.call_count == 0
        with psycopg.connect(admin_dsn) as connection:
            counts_after_noop = connection.execute(
                "SELECT "
                "(SELECT count(*) FROM sleep_domain_raw_inbox "
                "WHERE namespace_id = %s), "
                "(SELECT count(*) FROM sleep_domain_normalization_work "
                "WHERE namespace_id = %s), "
                "(SELECT count(*) FROM sleep_domain_canonical_observations "
                "WHERE namespace_id = %s), "
                "(SELECT count(*) FROM sleep_domain_pull_checkpoints "
                "WHERE namespace_id = %s), "
                "(SELECT cursor_at FROM sleep_domain_pull_checkpoints "
                "WHERE namespace_id = %s AND data_surface = 'history')",
                (NAMESPACE,) * 5,
            ).fetchone()
        assert counts_after_noop == counts_before_noop

        before_no_data = _count(admin_dsn, "sleep_domain_canonical_observations")
        no_data_ingress = ingress.accept(
            _read(
                SLEEP_REPORT_ENDPOINT,
                _sleep_no_data(),
                requested_at=NOW + timedelta(minutes=7),
                received_at=NOW + timedelta(minutes=7, seconds=1),
                envelope_nonce="sleep-no-data-1",
            ),
            binding=binding,
            coordinates=PullRequestCoordinates(
                endpoint=SLEEP_REPORT_ENDPOINT,
                report_date=date(2026, 8, 22),
            ),
        )
        assert no_data_ingress.disposition == "accepted"
        no_data_result = _process_next(
            store, worker_uow, cipher, worker="p4d2-b2-no-data-worker"
        )
        assert no_data_result.no_data is True
        assert no_data_result.canonical_observation_ids == ()
        assert no_data_result.checkpoint_advanced is True
        assert _count(admin_dsn, "sleep_domain_canonical_observations") == before_no_data
        assert _count(admin_dsn, "sleep_domain_night_episodes") == 0
        with psycopg.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*), bool_and(is_empty) FROM "
                    "sleep_domain_source_reports WHERE namespace_id = %s",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (1, True)
                cursor.execute(
                    "SELECT count(*) FROM sleep_domain_processing_receipts "
                    "WHERE namespace_id = %s AND stage = 'normalization' "
                    "AND receipt_json -> 'reconciliation_summary' ->> 'no_data' = 'true'",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (1,)

        before_unknown_device = _count(
            admin_dsn, "sleep_domain_canonical_observations"
        )
        unknown_device_data = _history_data(
            local_send_time="2026-08-23T11:08:00"
        )
        unknown_device_data[0]["device_id"] = "UNBOUND-SYNTHETIC-DEVICE"
        unknown_device = ingress.accept(
            _read(
                HISTORY_ENDPOINT,
                unknown_device_data,
                requested_at=NOW + timedelta(minutes=8),
                received_at=NOW + timedelta(minutes=8, seconds=1),
                envelope_nonce="unknown-device-1",
            ),
            binding=binding,
            coordinates=PullRequestCoordinates(
                endpoint=HISTORY_ENDPOINT,
                window_start_at=NOW + timedelta(minutes=7),
                window_end_at=NOW + timedelta(minutes=9),
            ),
        )
        assert unknown_device.disposition == "quarantined_device_mismatch"
        assert unknown_device.normalization_work_id is None
        assert unknown_device.subject_id is None
        assert (
            _count(admin_dsn, "sleep_domain_canonical_observations")
            == before_unknown_device
        )

        before_out_of_window = _count(
            admin_dsn, "sleep_domain_canonical_observations"
        )
        out_of_window = ingress.accept(
            _read(
                HISTORY_ENDPOINT,
                _history_data(local_send_time="2026-08-23T11:00:00"),
                requested_at=NOW + timedelta(minutes=30),
                received_at=NOW + timedelta(minutes=30, seconds=1),
                envelope_nonce="outside-requested-window",
            ),
            binding=binding,
            coordinates=PullRequestCoordinates(
                endpoint=HISTORY_ENDPOINT,
                window_start_at=NOW + timedelta(minutes=29),
                window_end_at=NOW + timedelta(minutes=31),
            ),
        )
        assert out_of_window.disposition == "accepted"
        rejected = _process_next(
            store, worker_uow, cipher, worker="p4d2-b2-window-worker"
        )
        assert rejected.quarantined is True
        assert (
            _count(admin_dsn, "sleep_domain_canonical_observations")
            == before_out_of_window
        )

        # A namespace reset permits byte-identical Push and Pull evidence to
        # normalize again without colliding with generation-one rows hidden by
        # Worker RLS. Same-generation Push/Pull overlap remains intact.
        before_generation_reset = _count(
            admin_dsn, "sleep_domain_canonical_observations"
        )
        with psycopg.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE backend_namespace_generations SET status = 'retired' "
                    "WHERE namespace_id = %s AND data_mode = 'live' "
                    "AND generation = 1",
                    (NAMESPACE,),
                )
                cursor.execute(
                    "INSERT INTO backend_namespace_generations (namespace_id, "
                    "data_mode, generation, status, configuration_sha256) VALUES "
                    "(%s, 'live', 2, 'active', %s)",
                    (NAMESPACE, "c" * 64),
                )
                cursor.execute(
                    "UPDATE backend_namespaces SET current_generation = 2 "
                    "WHERE namespace_id = %s AND data_mode = 'live'",
                    (NAMESPACE,),
                )
            connection.commit()

        generation_two_api_settings = _api_settings().model_copy(
            update={"perceptor_namespace_generation": 2}
        )
        generation_two_webhook = PerceptorWebhookService(
            generation_two_api_settings,
            api_uow,
            client_secret=SECRET,
            cipher=cipher,
            now_factory=lambda: NOW,
        )
        generation_two_ingress = DurablePerceptorPullIngress(
            generation_two_api_settings,
            api_uow,
            client_id_sha256=hashlib.sha256(CLIENT_ID.encode()).hexdigest(),
            cipher=cipher,
        )

        generation_two_push = generation_two_webhook.accept(
            _push_raw(message_id="p4d2-b2-push-1"),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert generation_two_push.disposition == "accepted"
        generation_two_push_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="p4d2-b2-generation-two-push-worker",
        )
        assert generation_two_push_result.canonical_created_count == 4

        generation_two_pull = generation_two_ingress.accept(
            _read(
                HISTORY_ENDPOINT,
                exact_data,
                requested_at=NOW + timedelta(seconds=1),
                received_at=NOW + timedelta(seconds=2),
                envelope_nonce="exact-1",
            ),
            binding=binding,
            coordinates=exact_coordinates,
        )
        assert generation_two_pull.disposition == "accepted"
        generation_two_pull_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="p4d2-b2-generation-two-pull-worker",
        )
        assert generation_two_pull_result.canonical_created_count == 0
        assert generation_two_pull_result.push_pull_overlap_count == 3
        assert generation_two_pull_result.conflict_created_count == 0
        assert (
            _count(admin_dsn, "sleep_domain_canonical_observations")
            == before_generation_reset + 4
        )

        generation_two_no_data = generation_two_ingress.accept(
            _read(
                SLEEP_REPORT_ENDPOINT,
                _sleep_no_data(),
                requested_at=NOW + timedelta(minutes=7),
                received_at=NOW + timedelta(minutes=7, seconds=1),
                envelope_nonce="sleep-no-data-1",
            ),
            binding=binding,
            coordinates=PullRequestCoordinates(
                endpoint=SLEEP_REPORT_ENDPOINT,
                report_date=date(2026, 8, 22),
            ),
        )
        assert generation_two_no_data.disposition == "accepted"
        generation_two_no_data_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="p4d2-b2-generation-two-no-data-worker",
        )
        assert generation_two_no_data_result.no_data is True
        assert generation_two_no_data_result.checkpoint_advanced is True

        with psycopg.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT raw.namespace_generation, "
                    "array_agg(candidate.candidate_id "
                    "ORDER BY candidate.candidate_id), "
                    "array_agg(candidate.source_key "
                    "ORDER BY candidate.source_key), "
                    "array_agg(candidate.idempotency_key "
                    "ORDER BY candidate.idempotency_key) "
                    "FROM sleep_domain_adapter_candidates AS candidate "
                    "JOIN sleep_domain_raw_inbox AS raw "
                    "ON raw.raw_ingress_record_id = candidate.raw_ingress_record_id "
                    "AND raw.namespace_id = candidate.namespace_id "
                    "AND raw.data_mode = candidate.data_mode "
                    "WHERE raw.raw_ingress_record_id IN (%s, %s, %s, %s) "
                    "GROUP BY raw.namespace_generation "
                    "ORDER BY raw.namespace_generation",
                    (
                        pushed.raw_ingress_record_id,
                        exact_ingress.raw_ingress_record_id,
                        generation_two_push.raw_ingress_record_id,
                        generation_two_pull.raw_ingress_record_id,
                    ),
                )
                identity_rows = cursor.fetchall()
                assert [row[0] for row in identity_rows] == [1, 2]
                for identity_index in (1, 2, 3):
                    assert set(identity_rows[0][identity_index]).isdisjoint(
                        identity_rows[1][identity_index]
                    )
                cursor.execute(
                    "SELECT raw.namespace_generation, "
                    "array_agg(DISTINCT fact.fact_slot_key "
                    "ORDER BY fact.fact_slot_key) "
                    "FROM sleep_domain_observation_fact_values AS fact "
                    "JOIN sleep_domain_canonical_observations AS observation "
                    "ON observation.observation_id = fact.observation_id "
                    "AND observation.namespace_id = fact.namespace_id "
                    "AND observation.data_mode = fact.data_mode "
                    "JOIN sleep_domain_raw_inbox AS raw "
                    "ON raw.raw_ingress_record_id = observation.raw_ingress_record_id "
                    "AND raw.namespace_id = observation.namespace_id "
                    "AND raw.data_mode = observation.data_mode "
                    "GROUP BY raw.namespace_generation "
                    "ORDER BY raw.namespace_generation"
                )
                fact_rows = cursor.fetchall()
                assert [row[0] for row in fact_rows] == [1, 2]
                assert set(fact_rows[0][1]).isdisjoint(fact_rows[1][1])
                cursor.execute(
                    "SELECT count(*), count(DISTINCT provider_device_key), "
                    "count(DISTINCT source_report_version_id) "
                    "FROM sleep_domain_source_reports WHERE namespace_id = %s",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (2, 2, 2)

                cursor.execute(
                    "SELECT count(*) FROM sleep_domain_raw_inbox "
                    "WHERE namespace_id = %s",
                    (NAMESPACE,),
                )
                raw_before_binding_period_rejections = int(cursor.fetchone()[0])

        reassigned_binding = _reassign_binding(
            admin_dsn,
            binding,
            effective_at=NOW + timedelta(minutes=10, seconds=30),
        )
        pre_binding_coordinates = PullRequestCoordinates(
            endpoint=HISTORY_ENDPOINT,
            window_start_at=NOW + timedelta(minutes=9),
            window_end_at=NOW + timedelta(minutes=11),
        )
        with pytest.raises(
            ValueError,
            match="precedes DeviceBinding effective interval",
        ):
            generation_two_ingress.accept(
                _read(
                    HISTORY_ENDPOINT,
                    _history_data(local_send_time="2026-08-23T11:10:00"),
                    requested_at=NOW + timedelta(minutes=30),
                    received_at=NOW + timedelta(minutes=30, seconds=1),
                    envelope_nonce="pre-binding-period",
                ),
                binding=reassigned_binding,
                coordinates=pre_binding_coordinates,
            )

        with pytest.raises(
            psycopg.errors.RaiseException,
            match="outside DeviceBinding effective interval",
        ):
            generation_two_ingress.accept(
                _read(
                    HISTORY_ENDPOINT,
                    _history_data(local_send_time="2026-08-23T11:10:00"),
                    requested_at=NOW + timedelta(minutes=10),
                    received_at=NOW + timedelta(minutes=10, seconds=1),
                    envelope_nonce="reassignment-crossing-period",
                ),
                binding=binding,
                coordinates=pre_binding_coordinates,
            )

        with psycopg.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM sleep_domain_raw_inbox "
                    "WHERE namespace_id = %s",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (raw_before_binding_period_rejections,)
    finally:
        worker_pool.close()
        api_pool.close()
