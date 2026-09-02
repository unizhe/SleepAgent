from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
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
from sleepagent.application.acquisition import (
    AcquisitionJobType,
    AcquisitionScheduleService,
    PostgresAcquisitionScheduler,
)
from sleepagent.application.device_bindings import ManagedDeviceBinding
from sleepagent.application.night_finalization import (
    NightFinalizationPending,
    NightFinalizationService,
    NightFinalizationState,
)
from sleepagent.domain.contracts import DeviceBinding, DeviceBindingStatus
from sleepagent.domain.episodes import (
    EpisodeAssignmentBasis,
    EpisodeDateConfidence,
    NightEpisodeV2,
    UUID7Generator,
    episode_anchor_key,
)
from sleepagent.infrastructure.postgres_sleep_slice import (
    EpisodeProjectionBoundary,
    NormalizationLease,
    RawPayloadCipher,
    default_sleep_slice_policy,
    project_authoritative_canonical_observations,
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
    PerceptorNormalizationProcessor,
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
    UowScope,
)
from sleepagent.process import DatabaseAttestation, SleepBackendRuntime
from sleepagent.workers.ingestion import NormalizationWorkHandlerAdapter
from sleepagent.workers.acquisition import build_acquisition_worker_handlers
from sleepagent.workers.kernel import WorkDisposition
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
            cursor.execute(
                "INSERT INTO backend_actors (actor_id, actor_kind, status) "
                "VALUES ('p4d2-b2-local-proof', 'human', 'active') "
                "ON CONFLICT DO NOTHING"
            )
            cursor.execute(
                "INSERT INTO backend_actor_subject_bindings (binding_id, "
                "namespace_id, data_mode, actor_id, subject_id, role, status, "
                "purpose_json, scopes_json, authorization_epoch, valid_from) "
                "VALUES ('actor-binding-p4d2-b2', %s, 'live', "
                "'p4d2-b2-local-proof', %s, 'elder', 'active', "
                "'[\"device_binding_management\"]'::jsonb, "
                "'[\"device:binding:manage\"]'::jsonb, 1, %s) "
                "ON CONFLICT DO NOTHING",
                (NAMESPACE, SUBJECT, NOW - timedelta(days=1)),
            )
            for grant_id, principal, purpose, scopes, handlers in (
                (
                    "grant-p4d2-b2-api",
                    _dsn("SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL"),
                    "perceptor_ingress",
                    [],
                    [],
                ),
                (
                    "grant-p4d2-b2-worker",
                    _dsn("SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL"),
                    "worker",
                    [],
                    [
                        "normalization",
                        *[item.value for item in AcquisitionJobType],
                    ],
                ),
                (
                    "grant-p4d2-b2-worker-acquisition",
                    _dsn("SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL"),
                    "acquisition_schedule",
                    [],
                    [item.value for item in AcquisitionJobType],
                ),
                (
                    "grant-p4d2-b2-api-device",
                    _dsn("SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL"),
                    "device_binding_management",
                    ["device:binding:manage"],
                    [],
                ),
            ):
                cursor.execute(
                    "INSERT INTO backend_principal_grants (grant_id, "
                    "principal_id, namespace_id, data_mode, purpose, "
                    "scopes_json, allowed_handlers_json, authorization_epoch, "
                    "status, valid_from) VALUES (%s, %s, %s, 'live', %s, "
                    "%s::jsonb, %s::jsonb, 1, 'active', %s) "
                    "ON CONFLICT DO NOTHING",
                    (
                        grant_id,
                        principal,
                        NAMESPACE,
                        purpose,
                        json.dumps(scopes),
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
            cursor.execute(
                "INSERT INTO sleep_domain_device_identities (namespace_id, "
                "data_mode, provider_id, provider_account_id, "
                "provider_device_key, device_id, provider_device_json, "
                "created_at) VALUES (%s, 'live', 'perceptor', %s, %s, "
                "'internal-p4d2-b2-radar', %s::jsonb, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    NAMESPACE,
                    ACCOUNT,
                    PROVIDER_DEVICE_ID,
                    json.dumps(binding_json["provider_device"]),
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


def _push_raw(
    *,
    message_id: str,
    heart_rate: int = 70,
    on_bed: int = 1,
    report_at: datetime = NOW,
    signed_at: datetime = NOW,
    local_datetime: str = "2026-08-23T11:00:00.000",
) -> bytes:
    payload: dict[str, object] = {
        "client_id": CLIENT_ID,
        "version": "2.0",
        "timestamp": int(signed_at.timestamp()),
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
            f'"OnBed":{on_bed},'
            f'"ReportTime":"{int(report_at.timestamp() * 1000)}",'
            f'"DateTime":"{local_datetime}"}}'
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


def _single_fact_sleep_report(measured_at: datetime) -> dict[str, object]:
    return {
        "sleep_profile": None,
        "sleep_stage_list": None,
        "heart_rate_data": [
            {
                "time_long": int(measured_at.timestamp()),
                "type": None,
                "value": 67,
            }
        ],
        "heart_rate_avg": None,
        "breathe_data": None,
        "breathe_avg": None,
        "body_shake_data": [],
        "sum_body_shake_times": None,
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

        first_push_payload = _push_raw(message_id="p4d2-b2-push-1")
        pushed = webhook.accept(
            first_push_payload,
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert pushed.disposition == "accepted"
        push_result = _process_next(
            store, worker_uow, cipher, worker="p4d2-b2-push-worker"
        )
        assert push_result.canonical_created_count == 4
        with psycopg.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT episode.night_episode_id, episode.subject_id, "
                    "episode.timezone_name, episode.state, "
                    "episode.current_revision_number, member.device_binding_id, "
                    "member.binding_version, count(*) OVER () "
                    "FROM sleep_domain_night_episodes AS episode "
                    "JOIN sleep_domain_episode_observation_memberships AS member "
                    "ON member.night_episode_id = episode.night_episode_id "
                    "AND member.namespace_id = episode.namespace_id "
                    "AND member.data_mode = episode.data_mode "
                    "WHERE episode.namespace_id = %s",
                    (NAMESPACE,),
                )
                push_episode = cursor.fetchone()
                assert push_episode is not None
                episode_id = str(push_episode[0])
                assert push_episode[1:] == (
                    SUBJECT,
                    "Asia/Shanghai",
                    "collecting",
                    4,
                    BINDING_ID,
                    1,
                    4,
                )

        repeated_push = webhook.accept(
            first_push_payload,
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert repeated_push.duplicate is True
        with psycopg.connect(admin_dsn) as connection:
            assert connection.execute(
                "SELECT current_revision_number, "
                "(SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships "
                "WHERE night_episode_id = %s) "
                "FROM sleep_domain_night_episodes "
                "WHERE night_episode_id = %s",
                (episode_id, episode_id),
            ).fetchone() == (4, 4)

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
                cursor.execute(
                    "SELECT current_revision_number, "
                    "(SELECT count(*) FROM "
                    "sleep_domain_episode_observation_memberships "
                    "WHERE night_episode_id = %s) "
                    "FROM sleep_domain_night_episodes "
                    "WHERE night_episode_id = %s",
                    (episode_id, episode_id),
                )
                assert cursor.fetchone() == (4, 4)

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
        with psycopg.connect(admin_dsn) as connection:
            assert connection.execute(
                "SELECT current_revision_number, "
                "(SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships "
                "WHERE night_episode_id = %s) "
                "FROM sleep_domain_night_episodes "
                "WHERE night_episode_id = %s",
                (episode_id, episode_id),
            ).fetchone() == (4, 4)

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
        with psycopg.connect(admin_dsn) as connection:
            assert connection.execute(
                "SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships AS member "
                "JOIN sleep_domain_canonical_observations AS canonical "
                "ON canonical.observation_id = member.observation_id "
                "WHERE member.night_episode_id = %s "
                "AND canonical.observation_json -> 'quality' "
                "-> 'quality_flags' ? 'push_pull_conflict'",
                (episode_id,),
            ).fetchone() == (0,)

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

        projection_retry_push = webhook.accept(
            _push_raw(
                message_id="p4d2-b2-projection-retry",
                report_at=NOW + timedelta(minutes=3),
                local_datetime="2026-08-23T11:03:00.000",
            ),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert projection_retry_push.disposition == "accepted"
        before_projection_failure = _count(
            admin_dsn, "sleep_domain_canonical_observations"
        )
        with psycopg.connect(admin_dsn) as connection:
            episode_before_projection_failure = connection.execute(
                "SELECT current_revision_number, "
                "(SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships "
                "WHERE night_episode_id = %s) "
                "FROM sleep_domain_night_episodes "
                "WHERE night_episode_id = %s",
                (episode_id, episode_id),
            ).fetchone()
        assert episode_before_projection_failure is not None

        def fail_before_projection(phase: str) -> None:
            assert phase == "before_episode_projection"
            raise _SimulatedCrash("simulated crash before Episode projection")

        projection_crash_runtime = _durable_ingestion_runtime(
            pool=worker_pool,
            worker_uow=worker_uow,
            store=store,
            processor=PerceptorNormalizationProcessor(
                worker_uow,
                cipher=cipher,
                projection_fault_injector=fail_before_projection,
            ),  # type: ignore[arg-type]
            worker_instance="p4d2-b2-projection-crash-worker",
        )
        assert projection_crash_runtime.run_once() is True
        assert (
            _count(admin_dsn, "sleep_domain_canonical_observations")
            == before_projection_failure
        )
        with psycopg.connect(admin_dsn) as connection:
            assert connection.execute(
                "SELECT current_revision_number, "
                "(SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships "
                "WHERE night_episode_id = %s) "
                "FROM sleep_domain_night_episodes "
                "WHERE night_episode_id = %s",
                (episode_id, episode_id),
            ).fetchone() == episode_before_projection_failure
            assert connection.execute(
                "SELECT status, attempt_count FROM "
                "sleep_domain_normalization_work WHERE work_id = %s",
                (projection_retry_push.normalization_work_id,),
            ).fetchone() == ("retry", 1)

        projection_retry_runtime = _durable_ingestion_runtime(
            pool=worker_pool,
            worker_uow=worker_uow,
            store=store,
            processor=PerceptorNormalizationProcessor(
                worker_uow,
                cipher=cipher,
            ),  # type: ignore[arg-type]
            worker_instance="p4d2-b2-projection-retry-worker",
        )
        projection_retried = False
        projection_retry_deadline = time.monotonic() + 1.0
        while time.monotonic() < projection_retry_deadline:
            if projection_retry_runtime.run_once():
                projection_retried = True
                break
            time.sleep(0.01)
        assert projection_retried is True
        assert (
            _count(admin_dsn, "sleep_domain_canonical_observations")
            == before_projection_failure + 4
        )
        with psycopg.connect(admin_dsn) as connection:
            projection_retry_episode = connection.execute(
                "SELECT current_revision_number, "
                "(SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships "
                "WHERE night_episode_id = %s) "
                "FROM sleep_domain_night_episodes "
                "WHERE night_episode_id = %s",
                (episode_id, episode_id),
            ).fetchone()
        assert projection_retry_episode == (
            int(episode_before_projection_failure[0]) + 4,
            int(episode_before_projection_failure[1]) + 4,
        )

        before_backfill_canonical = _count(
            admin_dsn, "sleep_domain_canonical_observations"
        )
        before_backfill_acquisitions = _count(
            admin_dsn, "sleep_domain_observation_acquisitions"
        )
        with psycopg.connect(admin_dsn) as connection:
            episode_before_post_commit_crash = connection.execute(
                "SELECT current_revision_number, "
                "(SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships "
                "WHERE night_episode_id = %s) "
                "FROM sleep_domain_night_episodes "
                "WHERE night_episode_id = %s",
                (episode_id, episode_id),
            ).fetchone()
        assert episode_before_post_commit_crash is not None
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
                    "SELECT current_revision_number, "
                    "(SELECT count(*) FROM "
                    "sleep_domain_episode_observation_memberships "
                    "WHERE night_episode_id = %s) "
                    "FROM sleep_domain_night_episodes "
                    "WHERE night_episode_id = %s",
                    (episode_id, episode_id),
                )
                episode_after_post_commit_crash = cursor.fetchone()
                assert episode_after_post_commit_crash == (
                    int(episode_before_post_commit_crash[0]) + 3,
                    int(episode_before_post_commit_crash[1]) + 3,
                )
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
        with psycopg.connect(admin_dsn) as connection:
            assert connection.execute(
                "SELECT current_revision_number, "
                "(SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships "
                "WHERE night_episode_id = %s) "
                "FROM sleep_domain_night_episodes "
                "WHERE night_episode_id = %s",
                (episode_id, episode_id),
            ).fetchone() == episode_after_post_commit_crash
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

        full_report_fixture = json.loads(
            (
                Path(__file__).parents[1]
                / "fixtures"
                / "perceptor_v2_5_2"
                / "sanitized_recorded_real_pull_get_sleep_report_full.json"
                ).read_text(encoding="utf-8")
            )
        full_report_data = full_report_fixture["data"]
        report_instants = tuple(
            NOW + timedelta(minutes=minute) for minute in (1, 2, 3, 4)
        )
        for item, start_at, end_at in zip(
            full_report_data["sleep_stage_list"],
            report_instants[:3],
            report_instants[1:],
            strict=True,
        ):
            item["start_time"] = int(start_at.timestamp())
            item["end_time"] = int(end_at.timestamp())
        for field in ("heart_rate_data", "breathe_data"):
            for item, measured_at in zip(
                full_report_data[field],
                (report_instants[0], report_instants[2], report_instants[3]),
                strict=True,
            ):
                item["time_long"] = int(measured_at.timestamp())
        full_report_data["body_shake_data"] = [{"hour": "11", "count": 2}]
        full_report_data["getups"] = [
            "2026-08-23 11:03:30",
            "2026-08-23 11:05:00",
        ]
        with psycopg.connect(admin_dsn) as connection:
            episode_before_sleep_report = connection.execute(
                "SELECT current_revision_number, "
                "(SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships "
                "WHERE night_episode_id = %s) "
                "FROM sleep_domain_night_episodes "
                "WHERE night_episode_id = %s",
                (episode_id, episode_id),
            ).fetchone()
        assert episode_before_sleep_report is not None
        full_report_ingress = ingress.accept(
            _read(
                SLEEP_REPORT_ENDPOINT,
                full_report_data,
                requested_at=NOW + timedelta(minutes=6),
                received_at=NOW + timedelta(minutes=6, seconds=1),
                envelope_nonce="sleep-full-v2-1",
            ),
            binding=binding,
            coordinates=PullRequestCoordinates(
                endpoint=SLEEP_REPORT_ENDPOINT,
                report_date=date(2026, 8, 23),
            ),
        )
        assert full_report_ingress.disposition == "accepted"
        full_report_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="p4d2-b2-full-sleep-report-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        )
        assert full_report_result.quarantined is False
        assert full_report_result.canonical_created_count == 17
        assert full_report_result.duplicate_count == 0
        assert full_report_result.conflict_created_count == 0
        assert full_report_result.checkpoint_advanced is True
        with psycopg.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT metric_id, canonical_unit, source_kind, count(*) "
                    "FROM sleep_domain_observation_semantics_v2 "
                    "WHERE namespace_id = %s AND observation_id = ANY(%s) "
                    "GROUP BY metric_id, canonical_unit, source_kind "
                    "ORDER BY metric_id",
                    (
                        NAMESPACE,
                        list(full_report_result.canonical_observation_ids),
                    ),
                )
                assert cursor.fetchall() == [
                    ("bed_exit_event", "event", "vendor_derived", 2),
                    ("deep_sleep_ratio", "percent", "vendor_derived", 1),
                    ("heart_rate", "beats_per_minute", "device_measured", 3),
                    ("heart_rate_mean", "beats_per_minute", "vendor_derived", 1),
                    ("movement_event_count", "count", "vendor_derived", 1),
                    ("movement_event_total", "count", "vendor_derived", 1),
                    (
                        "respiratory_rate",
                        "breaths_per_minute",
                        "device_measured",
                        3,
                    ),
                    (
                        "respiratory_rate_mean",
                        "breaths_per_minute",
                        "vendor_derived",
                        1,
                    ),
                    ("sleep_efficiency", "percent", "vendor_derived", 1),
                    ("sleep_stage", "stage_interval", "vendor_derived", 3),
                ]
                cursor.execute(
                    "SELECT count(*) FROM sleep_domain_observation_semantics_v2 "
                    "WHERE namespace_id = %s AND observation_id = ANY(%s) "
                    "AND (canonical_unit IS NULL OR NOT trusted_for_analytics)",
                    (
                        NAMESPACE,
                        list(full_report_result.canonical_observation_ids),
                    ),
                )
                assert cursor.fetchone() == (0,)
                cursor.execute(
                    "SELECT current_revision_number, "
                    "(SELECT count(*) FROM "
                    "sleep_domain_episode_observation_memberships "
                    "WHERE night_episode_id = %s) "
                    "FROM sleep_domain_night_episodes "
                    "WHERE night_episode_id = %s",
                    (episode_id, episode_id),
                )
                assert cursor.fetchone() == (
                    int(episode_before_sleep_report[0]) + 17,
                    int(episode_before_sleep_report[1]) + 17,
                )

        closing_push = webhook.accept(
            _push_raw(
                message_id="p4d2-b2-push-close",
                on_bed=0,
                report_at=NOW + timedelta(minutes=7),
                local_datetime="2026-08-23T11:07:00.000",
            ),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert closing_push.disposition == "accepted"
        closing_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="p4d2-b2-push-close-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        )
        assert closing_result.canonical_created_count == 4
        with psycopg.connect(admin_dsn) as connection:
            live_chain = connection.execute(
                "SELECT episode.state, episode.episode_local_date, "
                "episode.timezone_name, episode.current_revision_number, "
                "(SELECT count(*) FROM "
                "sleep_domain_night_episode_revisions AS revision "
                "WHERE revision.night_episode_id = episode.night_episode_id), "
                "(SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships AS member "
                "WHERE member.night_episode_id = episode.night_episode_id), "
                "(SELECT coalesce(sum(octet_length(revision_json::text)), 0) "
                "FROM sleep_domain_night_episode_revisions AS revision "
                "WHERE revision.night_episode_id = episode.night_episode_id) "
                "FROM sleep_domain_night_episodes AS episode "
                "WHERE episode.night_episode_id = %s",
                (episode_id,),
            ).fetchone()
        assert live_chain is not None
        assert live_chain[0:3] == (
            "awaiting_report",
            date(2026, 8, 23),
            "Asia/Shanghai",
        )
        assert live_chain[3] == live_chain[4]
        assert live_chain[3] == live_chain[5]
        assert int(live_chain[6]) > 0

        final_scope = UowScope(
            namespace_id=NAMESPACE,
            namespace_generation=1,
            data_mode="live",
            process_role="worker",
            purpose="worker",
            service_principal_id=_dsn(
                "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL"
            ),
            subject_id=SUBJECT,
            authorization_epoch=1,
            privacy_epoch=1,
            retrieval_policy_epoch=1,
            worker_instance="p4d2-b2-live-finalizer",
        )
        finalized = NightFinalizationService(worker_uow).finalize(
            final_scope,
            night_episode_id=episode_id,
            evaluated_at=NOW + timedelta(minutes=8),
        )
        assert finalized.state is NightFinalizationState.HARD_FINALIZED
        assert finalized.source_report_version_id is not None
        assert finalized.source_night_episode_revision_id is not None
        assert finalized.reanalysis_operation_id is not None
        with psycopg.connect(admin_dsn) as connection:
            assert connection.execute(
                "SELECT state, current_revision_number FROM "
                "sleep_domain_night_finalizations "
                "WHERE night_episode_id = %s",
                (episode_id,),
            ).fetchone() == ("hard_finalized", 1)

        # C1B authority proof: a later-arriving vendor Push is normalized and
        # reconciled normally, then routed back to the one compatible closed
        # Episode.  The test never synthesizes an Episode revision directly.
        late_received_at = NOW + timedelta(hours=1, minutes=8)
        late_report_at = NOW + timedelta(minutes=4, seconds=30)
        late_payload = _push_raw(
            message_id="p4d2-b2-late-closed-night",
            report_at=late_report_at,
            signed_at=late_received_at,
            local_datetime="2026-08-23T11:04:30.000",
        )
        late_webhook = PerceptorWebhookService(
            _api_settings(),
            api_uow,
            client_secret=SECRET,
            cipher=cipher,
            now_factory=lambda: late_received_at,
        )
        with psycopg.connect(admin_dsn) as connection:
            episode_before_late = connection.execute(
                "SELECT current_revision_number, current_revision_id, "
                "(SELECT revision_json FROM "
                "sleep_domain_night_episode_revisions "
                "WHERE night_episode_revision_id = episode.current_revision_id), "
                "(SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships "
                "WHERE night_episode_id = episode.night_episode_id) "
                "FROM sleep_domain_night_episodes AS episode "
                "WHERE night_episode_id = %s",
                (episode_id,),
            ).fetchone()
        assert episode_before_late is not None

        late_ingress = late_webhook.accept(
            late_payload,
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert late_ingress.disposition == "accepted"
        late_failure_count = 0

        def fail_late_before_projection(phase: str) -> None:
            nonlocal late_failure_count
            assert phase == "before_episode_projection"
            late_failure_count += 1
            raise _SimulatedCrash("late projection pre-commit crash")

        late_crash_runtime = _durable_ingestion_runtime(
            pool=worker_pool,
            worker_uow=worker_uow,
            store=store,
            processor=PerceptorNormalizationProcessor(
                worker_uow,
                cipher=cipher,
                projection_fault_injector=fail_late_before_projection,
                observation_semantics_version=ObservationSemanticsVersion.V2,
            ),  # type: ignore[arg-type]
            worker_instance="p4d2-b2-late-crash-worker",
        )
        assert late_crash_runtime.run_once() is True
        assert late_failure_count == 1
        with psycopg.connect(admin_dsn) as connection:
            assert connection.execute(
                "SELECT count(*) FROM sleep_domain_canonical_observations "
                "WHERE raw_ingress_record_id = %s",
                (late_ingress.raw_ingress_record_id,),
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT current_revision_number FROM "
                "sleep_domain_night_episodes WHERE night_episode_id = %s",
                (episode_id,),
            ).fetchone() == (episode_before_late[0],)

        late_retry_runtime = _durable_ingestion_runtime(
            pool=worker_pool,
            worker_uow=worker_uow,
            store=store,
            processor=PerceptorNormalizationProcessor(
                worker_uow,
                cipher=cipher,
                observation_semantics_version=ObservationSemanticsVersion.V2,
            ),  # type: ignore[arg-type]
            worker_instance="p4d2-b2-late-retry-worker",
        )
        late_retried = False
        late_retry_deadline = time.monotonic() + 1.0
        while time.monotonic() < late_retry_deadline:
            if late_retry_runtime.run_once():
                late_retried = True
                break
            time.sleep(0.01)
        assert late_retried is True
        with psycopg.connect(admin_dsn) as connection:
            late_observation_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT observation_id FROM "
                    "sleep_domain_canonical_observations "
                    "WHERE raw_ingress_record_id = %s ORDER BY observation_id",
                    (late_ingress.raw_ingress_record_id,),
                ).fetchall()
            )
        assert len(late_observation_ids) == 4
        with psycopg.connect(admin_dsn) as connection:
            episode_after_late = connection.execute(
                "SELECT current_revision_number, current_revision_id, "
                "(SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships "
                "WHERE night_episode_id = episode.night_episode_id), "
                "(SELECT count(*) FROM "
                "sleep_domain_night_episode_revisions "
                "WHERE night_episode_id = episode.night_episode_id), "
                "(SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships "
                "WHERE night_episode_id = episode.night_episode_id "
                "AND observation_id = ANY(%s) "
                "AND lateness_watermark_at = %s "
                "AND late_after_watermark = FALSE) "
                "FROM sleep_domain_night_episodes AS episode "
                "WHERE night_episode_id = %s",
                (
                    list(late_observation_ids),
                    NOW + timedelta(hours=2, minutes=7),
                    episode_id,
                ),
            ).fetchone()
            original_revision_json = connection.execute(
                "SELECT revision_json FROM "
                "sleep_domain_night_episode_revisions "
                "WHERE night_episode_revision_id = %s",
                (str(episode_before_late[1]),),
            ).fetchone()
            dormant = connection.execute(
                "SELECT state, active_night_episode_id FROM "
                "backend_monitoring_snapshots_v2 "
                "WHERE namespace_id = %s AND subject_id = %s "
                "AND namespace_generation = 1",
                (NAMESPACE, SUBJECT),
            ).fetchone()
            associated_evidence = connection.execute(
                "SELECT count(*), bool_and(status = 'associated'), "
                "bool_and(reason_code = 'LATE_OBSERVATION_ASSOCIATED') "
                "FROM sleep_domain_pending_episode_associations "
                "WHERE namespace_id = %s AND source_resource_id = ANY(%s)",
                (NAMESPACE, list(late_observation_ids)),
            ).fetchone()
        assert episode_after_late is not None
        assert int(episode_after_late[0]) == int(episode_before_late[0]) + 4
        assert int(episode_after_late[2]) == int(episode_before_late[3]) + 4
        assert episode_after_late[0] == episode_after_late[3]
        assert episode_after_late[4] == 4
        assert original_revision_json == (episode_before_late[2],)
        assert dormant == ("dormant", None)
        assert associated_evidence == (4, True, True)

        with psycopg.connect(admin_dsn) as connection:
            connection.execute(
                "INSERT INTO sleep_domain_pending_episode_associations ("
                "association_id, namespace_id, data_mode, association_kind, "
                "source_resource_id, subject_id, status, reason_code, "
                "association_json, created_at, namespace_generation) VALUES ("
                "'c1b-cross-subject-evidence', %s, 'live', 'observation', "
                "'c1b-cross-subject-observation', 'different-subject', "
                "'quarantined', 'LATE_ASSOCIATION_NO_MATCH', '{}'::jsonb, "
                "%s, 1)",
                (NAMESPACE, late_received_at),
            )
        with worker_uow.begin(final_scope) as uow:
            scoped_cursor = uow.connection.cursor()
            try:
                scoped_cursor.execute(
                    "SELECT count(*) FROM "
                    "sleep_domain_pending_episode_associations "
                    "WHERE association_id = 'c1b-cross-subject-evidence'"
                )
                assert scoped_cursor.fetchone() == (0,)
            finally:
                scoped_cursor.close()

        concurrent_policy = default_sleep_slice_policy()

        def concurrently_recheck_same_late_observation() -> bool:
            boundary = EpisodeProjectionBoundary(concurrent_policy)
            with worker_uow.begin(final_scope) as uow:
                decisions = project_authoritative_canonical_observations(
                    uow.connection,
                    final_scope,
                    (late_observation_ids[0],),
                    committed_at=NOW + timedelta(hours=1, minutes=9),
                    policy=concurrent_policy,
                    projection_boundary=boundary,
                    id_generator=boundary.id_generator,
                )
                uow.commit()
            return decisions[0].persist_required

        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_results = tuple(
                executor.map(
                    lambda _index: concurrently_recheck_same_late_observation(),
                    range(2),
                )
            )
        assert concurrent_results == (False, False)
        with psycopg.connect(admin_dsn) as connection:
            assert connection.execute(
                "SELECT current_revision_number FROM "
                "sleep_domain_night_episodes WHERE night_episode_id = %s",
                (episode_id,),
            ).fetchone() == (episode_after_late[0],)

        revised_finalization = NightFinalizationService(worker_uow).finalize(
            final_scope,
            night_episode_id=episode_id,
            evaluated_at=NOW + timedelta(hours=1, minutes=10),
        )
        assert revised_finalization.state is NightFinalizationState.HARD_FINALIZED
        assert revised_finalization.finalization_revision_number == 2
        assert revised_finalization.parent_finalization_revision_id == (
            finalized.night_finalization_revision_id
        )
        assert revised_finalization.source_night_episode_revision_id == str(
            episode_after_late[1]
        )
        assert revised_finalization.reanalysis_operation_id is not None
        assert revised_finalization.reanalysis_operation_id != (
            finalized.reanalysis_operation_id
        )
        repeated_finalization = NightFinalizationService(worker_uow).finalize(
            final_scope,
            night_episode_id=episode_id,
            evaluated_at=NOW + timedelta(hours=1, minutes=11),
        )
        assert repeated_finalization == revised_finalization

        repeated_late = late_webhook.accept(
            late_payload,
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert repeated_late.duplicate is True
        with psycopg.connect(admin_dsn) as connection:
            assert connection.execute(
                "SELECT current_revision_number, "
                "(SELECT count(*) FROM "
                "sleep_domain_night_finalization_revisions "
                "WHERE night_episode_id = %s) "
                "FROM sleep_domain_night_episodes "
                "WHERE night_episode_id = %s",
                (episode_id, episode_id),
            ).fetchone() == (episode_after_late[0], 2)

        out_of_window_at = NOW + timedelta(hours=2, minutes=7, seconds=1)
        out_of_window = PerceptorWebhookService(
            _api_settings(),
            api_uow,
            client_secret=SECRET,
            cipher=cipher,
            now_factory=lambda: out_of_window_at,
        ).accept(
            _push_raw(
                message_id="p4d2-b2-late-out-of-window",
                report_at=NOW + timedelta(minutes=4, seconds=45),
                signed_at=out_of_window_at,
                local_datetime="2026-08-23T11:04:45.000",
            ),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert out_of_window.disposition == "accepted"
        out_of_window_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="p4d2-b2-late-out-of-window-worker",
        )
        assert out_of_window_result.canonical_created_count == 4

        no_match_received_at = NOW + timedelta(hours=1, minutes=12)
        no_match = PerceptorWebhookService(
            _api_settings(),
            api_uow,
            client_secret=SECRET,
            cipher=cipher,
            now_factory=lambda: no_match_received_at,
        ).accept(
            _push_raw(
                message_id="p4d2-b2-late-no-match",
                report_at=NOW - timedelta(days=1),
                signed_at=no_match_received_at,
                local_datetime="2026-08-22T11:00:00.000",
            ),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert no_match.disposition == "accepted"
        no_match_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="p4d2-b2-late-no-match-worker",
        )
        assert no_match_result.canonical_created_count == 4
        with psycopg.connect(admin_dsn) as connection:
            quarantined = connection.execute(
                "SELECT reason_code, count(*) FROM "
                "sleep_domain_pending_episode_associations "
                "WHERE namespace_id = %s AND source_resource_id = ANY(%s) "
                "GROUP BY reason_code ORDER BY reason_code",
                (
                    NAMESPACE,
                    list(
                        out_of_window_result.canonical_observation_ids
                        + no_match_result.canonical_observation_ids
                    ),
                ),
            ).fetchall()
            unchanged_after_quarantine = connection.execute(
                "SELECT current_revision_number, "
                "(SELECT count(*) FROM sleep_domain_night_episodes "
                "WHERE namespace_id = %s) "
                "FROM sleep_domain_night_episodes "
                "WHERE night_episode_id = %s",
                (NAMESPACE, episode_id),
            ).fetchone()
        assert quarantined == [
            ("LATE_ASSOCIATION_NO_MATCH", 4),
            ("LATE_ASSOCIATION_OUT_OF_WINDOW", 4),
        ]
        assert unchanged_after_quarantine == (episode_after_late[0], 1)

        # M1/M3 composed authority proof. Night B is active while an old Night-A
        # batch (including OUT_OF_BED) arrives far beyond A's lateness window.
        # It must be quarantined without changing B. Then one inside-window
        # report may complete B, while an outside-window report for Night C may
        # persist source metadata but cannot claim complete coverage.
        night_b_open_at = NOW + timedelta(days=4, hours=10)
        night_b_close_at = night_b_open_at + timedelta(hours=1)
        night_b_webhook = PerceptorWebhookService(
            _api_settings(),
            api_uow,
            client_secret=SECRET,
            cipher=cipher,
            now_factory=lambda: night_b_open_at,
        )
        night_b_open = night_b_webhook.accept(
            _push_raw(
                message_id="fsa-night-b-open",
                report_at=night_b_open_at,
                signed_at=night_b_open_at,
                local_datetime="2026-08-27T21:00:00.000",
            ),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert night_b_open.disposition == "accepted"
        assert _process_next(
            store,
            worker_uow,
            cipher,
            worker="fsa-night-b-open-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        ).canonical_created_count == 4
        with psycopg.connect(admin_dsn) as connection:
            night_b_before_late = connection.execute(
                "SELECT episode.night_episode_id, "
                "episode.current_revision_number, "
                "(SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships AS member "
                "WHERE member.night_episode_id = episode.night_episode_id) "
                "FROM backend_monitoring_snapshots_v2 AS snapshot "
                "JOIN sleep_domain_night_episodes AS episode "
                "ON episode.night_episode_id = snapshot.active_night_episode_id "
                "WHERE snapshot.namespace_id = %s "
                "AND snapshot.subject_id = %s "
                "AND snapshot.namespace_generation = 1",
                (NAMESPACE, SUBJECT),
            ).fetchone()
        assert night_b_before_late is not None
        assert night_b_before_late[1:] == (4, 4)
        night_b_episode_id = str(night_b_before_late[0])

        delayed_night_a_received_at = night_b_open_at + timedelta(minutes=10)
        delayed_night_a = PerceptorWebhookService(
            _api_settings(),
            api_uow,
            client_secret=SECRET,
            cipher=cipher,
            now_factory=lambda: delayed_night_a_received_at,
        ).accept(
            _push_raw(
                message_id="fsa-night-a-delayed-while-b-active",
                heart_rate=73,
                on_bed=0,
                report_at=NOW + timedelta(minutes=5, seconds=30),
                signed_at=delayed_night_a_received_at,
                local_datetime="2026-08-23T11:05:30.000",
            ),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert delayed_night_a.disposition == "accepted"
        delayed_night_a_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="fsa-night-a-delayed-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        )
        assert delayed_night_a_result.canonical_created_count == 4
        with psycopg.connect(admin_dsn) as connection:
            assert connection.execute(
                "SELECT episode.state, episode.current_revision_number, "
                "(SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships AS member "
                "WHERE member.night_episode_id = episode.night_episode_id), "
                "snapshot.state, snapshot.active_night_episode_id "
                "FROM sleep_domain_night_episodes AS episode "
                "JOIN backend_monitoring_snapshots_v2 AS snapshot "
                "ON snapshot.active_night_episode_id = episode.night_episode_id "
                "WHERE episode.night_episode_id = %s",
                (night_b_episode_id,),
            ).fetchone() == (
                "collecting",
                night_b_before_late[1],
                night_b_before_late[2],
                "active",
                night_b_episode_id,
            )
            assert connection.execute(
                "SELECT count(*), "
                "bool_and(status = 'quarantined'), "
                "bool_and(reason_code = 'LATE_ASSOCIATION_OUT_OF_WINDOW') "
                "FROM sleep_domain_pending_episode_associations "
                "WHERE namespace_id = %s AND source_resource_id = ANY(%s)",
                (
                    NAMESPACE,
                    list(delayed_night_a_result.canonical_observation_ids),
                ),
            ).fetchone() == (4, True, True)
            assert connection.execute(
                "SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships "
                "WHERE observation_id = ANY(%s)",
                (list(delayed_night_a_result.canonical_observation_ids),),
            ).fetchone() == (0,)

        night_b_close = PerceptorWebhookService(
            _api_settings(),
            api_uow,
            client_secret=SECRET,
            cipher=cipher,
            now_factory=lambda: night_b_close_at,
        ).accept(
            _push_raw(
                message_id="fsa-night-b-close",
                on_bed=0,
                report_at=night_b_close_at,
                signed_at=night_b_close_at,
                local_datetime="2026-08-27T22:00:00.000",
            ),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert night_b_close.disposition == "accepted"
        assert _process_next(
            store,
            worker_uow,
            cipher,
            worker="fsa-night-b-close-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        ).canonical_created_count == 4

        inside_report_received_at = night_b_close_at + timedelta(hours=1)
        inside_report = ingress.accept(
            _read(
                SLEEP_REPORT_ENDPOINT,
                _single_fact_sleep_report(
                    night_b_open_at + timedelta(minutes=30)
                ),
                requested_at=inside_report_received_at - timedelta(seconds=1),
                received_at=inside_report_received_at,
                envelope_nonce="fsa-report-inside-lateness",
            ),
            binding=binding,
            coordinates=PullRequestCoordinates(
                endpoint=SLEEP_REPORT_ENDPOINT,
                report_date=date(2026, 8, 27),
            ),
        )
        assert inside_report.disposition == "accepted"
        inside_report_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="fsa-report-inside-lateness-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        )
        assert inside_report_result.canonical_created_count == 1
        inside_finalization = NightFinalizationService(worker_uow).finalize(
            final_scope,
            night_episode_id=night_b_episode_id,
            evaluated_at=inside_report_received_at + timedelta(seconds=1),
        )
        assert inside_finalization.state is NightFinalizationState.HARD_FINALIZED
        assert inside_finalization.coverage_status == "complete"
        assert inside_finalization.source_report_version_id is not None
        with psycopg.connect(admin_dsn) as connection:
            assert connection.execute(
                "SELECT revision.revision_json -> 'observation_ids' ? %s, "
                "EXISTS (SELECT 1 FROM "
                "sleep_domain_episode_observation_memberships AS member "
                "WHERE member.night_episode_id = %s "
                "AND member.observation_id = %s) "
                "FROM sleep_domain_night_episode_revisions AS revision "
                "WHERE revision.night_episode_revision_id = %s",
                (
                    inside_report_result.canonical_observation_ids[0],
                    night_b_episode_id,
                    inside_report_result.canonical_observation_ids[0],
                    inside_finalization.source_night_episode_revision_id,
                ),
            ).fetchone() == (True, True)

        night_c_open_at = night_b_open_at + timedelta(days=1)
        night_c_close_at = night_c_open_at + timedelta(hours=1)
        night_c_open = PerceptorWebhookService(
            _api_settings(),
            api_uow,
            client_secret=SECRET,
            cipher=cipher,
            now_factory=lambda: night_c_open_at,
        ).accept(
            _push_raw(
                message_id="fsa-night-c-open",
                report_at=night_c_open_at,
                signed_at=night_c_open_at,
                local_datetime="2026-08-28T21:00:00.000",
            ),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert night_c_open.disposition == "accepted"
        assert _process_next(
            store,
            worker_uow,
            cipher,
            worker="fsa-night-c-open-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        ).canonical_created_count == 4
        with psycopg.connect(admin_dsn) as connection:
            night_c_episode_id = str(
                connection.execute(
                    "SELECT active_night_episode_id FROM "
                    "backend_monitoring_snapshots_v2 "
                    "WHERE namespace_id = %s AND subject_id = %s "
                    "AND namespace_generation = 1",
                    (NAMESPACE, SUBJECT),
                ).fetchone()[0]
            )
        night_c_close = PerceptorWebhookService(
            _api_settings(),
            api_uow,
            client_secret=SECRET,
            cipher=cipher,
            now_factory=lambda: night_c_close_at,
        ).accept(
            _push_raw(
                message_id="fsa-night-c-close",
                on_bed=0,
                report_at=night_c_close_at,
                signed_at=night_c_close_at,
                local_datetime="2026-08-28T22:00:00.000",
            ),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert night_c_close.disposition == "accepted"
        assert _process_next(
            store,
            worker_uow,
            cipher,
            worker="fsa-night-c-close-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        ).canonical_created_count == 4

        outside_report_received_at = (
            night_c_close_at
            + timedelta(
                seconds=(
                    default_sleep_slice_policy().boundary.allowed_lateness_seconds
                    + 1
                )
            )
        )
        outside_report = ingress.accept(
            _read(
                SLEEP_REPORT_ENDPOINT,
                _single_fact_sleep_report(
                    night_c_open_at + timedelta(minutes=30)
                ),
                requested_at=outside_report_received_at - timedelta(seconds=1),
                received_at=outside_report_received_at,
                envelope_nonce="fsa-report-outside-lateness",
            ),
            binding=binding,
            coordinates=PullRequestCoordinates(
                endpoint=SLEEP_REPORT_ENDPOINT,
                report_date=date(2026, 8, 28),
            ),
        )
        assert outside_report.disposition == "accepted"
        outside_report_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="fsa-report-outside-lateness-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        )
        assert outside_report_result.canonical_created_count == 1
        with pytest.raises(NightFinalizationPending):
            NightFinalizationService(worker_uow).finalize(
                final_scope,
                night_episode_id=night_c_episode_id,
                evaluated_at=outside_report_received_at,
            )
        with psycopg.connect(admin_dsn) as connection:
            assert connection.execute(
                "SELECT count(*) FROM sleep_domain_source_reports "
                "WHERE namespace_id = %s AND raw_ingress_record_id = %s",
                (NAMESPACE, outside_report.raw_ingress_record_id),
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT count(*) FROM "
                "sleep_domain_episode_observation_memberships "
                "WHERE night_episode_id = %s AND observation_id = %s",
                (
                    night_c_episode_id,
                    outside_report_result.canonical_observation_ids[0],
                ),
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT status, reason_code FROM "
                "sleep_domain_pending_episode_associations "
                "WHERE namespace_id = %s AND source_resource_id = %s",
                (
                    NAMESPACE,
                    outside_report_result.canonical_observation_ids[0],
                ),
            ).fetchone() == (
                "quarantined",
                "LATE_ASSOCIATION_OUT_OF_WINDOW",
            )

        night_d_open_at = night_c_open_at + timedelta(days=1)
        night_d_close_at = night_d_open_at + timedelta(hours=1)
        night_d_open = PerceptorWebhookService(
            _api_settings(),
            api_uow,
            client_secret=SECRET,
            cipher=cipher,
            now_factory=lambda: night_d_open_at,
        ).accept(
            _push_raw(
                message_id="fsa-night-d-open",
                report_at=night_d_open_at,
                signed_at=night_d_open_at,
                local_datetime="2026-08-29T21:00:00.000",
            ),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert night_d_open.disposition == "accepted"
        assert _process_next(
            store,
            worker_uow,
            cipher,
            worker="fsa-night-d-open-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        ).canonical_created_count == 4
        with psycopg.connect(admin_dsn) as connection:
            night_d_episode_id = str(
                connection.execute(
                    "SELECT active_night_episode_id FROM "
                    "backend_monitoring_snapshots_v2 "
                    "WHERE namespace_id = %s AND subject_id = %s "
                    "AND namespace_generation = 1",
                    (NAMESPACE, SUBJECT),
                ).fetchone()[0]
            )
        night_d_close = PerceptorWebhookService(
            _api_settings(),
            api_uow,
            client_secret=SECRET,
            cipher=cipher,
            now_factory=lambda: night_d_close_at,
        ).accept(
            _push_raw(
                message_id="fsa-night-d-close",
                on_bed=0,
                report_at=night_d_close_at,
                signed_at=night_d_close_at,
                local_datetime="2026-08-29T22:00:00.000",
            ),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert night_d_close.disposition == "accepted"
        assert _process_next(
            store,
            worker_uow,
            cipher,
            worker="fsa-night-d-close-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        ).canonical_created_count == 4

        schedule_scope = UowScope(
            namespace_id=NAMESPACE,
            namespace_generation=1,
            data_mode="live",
            process_role="api",
            purpose="device_binding_management",
            service_principal_id=_dsn(
                "SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL"
            ),
            subject_id=SUBJECT,
            actor_id="p4d2-b2-local-proof",
            actor_role="elder",
            authorization_epoch=1,
            privacy_epoch=1,
            retrieval_policy_epoch=1,
        )
        schedule_service = AcquisitionScheduleService(api_uow)
        finalization_schedule = schedule_service.create(
            schedule_scope,
            binding=ManagedDeviceBinding(binding=binding, cas_version=0),
            job_type=AcquisitionJobType.NIGHT_FINALIZATION_SCAN,
            next_run_at=datetime.now(tz=UTC) - timedelta(minutes=5),
            cadence_seconds=300,
            jitter_seconds=0,
        )
        scheduler = PostgresAcquisitionScheduler(
            worker_uow,
            data_mode="live",
            service_principal_id=_dsn(
                "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL"
            ),
            worker_instance="fsa-combined-scheduler",
            enabled=True,
        )
        first_combined_fire = next(
            item
            for item in scheduler.fire_due(limit=10)
            if item.schedule_id == finalization_schedule.schedule_id
        )
        combined_claim = store.claim(
            queue=AcquisitionJobType.NIGHT_FINALIZATION_SCAN.value,
            worker_instance="fsa-combined-finalization-worker",
            lease_seconds=60,
        )
        assert combined_claim is not None
        combined_settings = SimpleNamespace(
            process_role=ProcessRole.WORKER,
            data_mode=DataMode.LIVE,
            provider_mode=ProviderMode.LIVE,
            acquisition_scheduler_enabled=True,
            worker_queues=tuple(item.value for item in AcquisitionJobType),
        )
        combined_handlers = build_acquisition_worker_handlers(combined_settings)
        finalization_handler = combined_handlers[
            AcquisitionJobType.NIGHT_FINALIZATION_SCAN.value
        ]
        combined_result = finalization_handler(
            WorkContext(combined_claim, store, threading.Event())
        )
        assert combined_result.disposition is WorkDisposition.SUCCEEDED
        assert combined_result.result["night_episode_ids"] == [
            night_c_episode_id,
            night_d_episode_id,
        ]
        assert combined_result.result["processed_count"] == 2
        assert datetime.fromisoformat(
            str(combined_result.result["evaluated_at"])
        ) > first_combined_fire.scheduled_for
        assert store.finalize(combined_claim, combined_result) is True

        # A material late revision makes the already HARD Night C due again.
        # Re-fire the same production schedule and prove the combined handler
        # follows the revision-mismatch recovery path with current time.
        late_night_c_received_at = night_c_close_at + timedelta(hours=1)
        late_night_c = PerceptorWebhookService(
            _api_settings(),
            api_uow,
            client_secret=SECRET,
            cipher=cipher,
            now_factory=lambda: late_night_c_received_at,
        ).accept(
            _push_raw(
                message_id="fsa-night-c-late-revision",
                heart_rate=74,
                report_at=night_c_open_at + timedelta(minutes=45),
                signed_at=late_night_c_received_at,
                local_datetime="2026-08-28T21:45:00.000",
            ),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert late_night_c.disposition == "accepted"
        late_night_c_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="fsa-night-c-late-revision-worker",
            observation_semantics_version=ObservationSemanticsVersion.V2,
        )
        assert late_night_c_result.canonical_created_count == 4
        current_schedule = schedule_service.show(
            schedule_scope,
            schedule_id=finalization_schedule.schedule_id,
        )
        paused_schedule = schedule_service.pause(
            schedule_scope,
            schedule_id=current_schedule.schedule_id,
            expected_cas=current_schedule.cas_version,
        )
        resumed_schedule = schedule_service.resume(
            schedule_scope,
            schedule_id=paused_schedule.schedule_id,
            expected_cas=paused_schedule.cas_version,
            next_run_at=datetime.now(tz=UTC) - timedelta(minutes=1),
        )
        second_combined_fire = next(
            item
            for item in scheduler.fire_due(limit=10)
            if item.schedule_id == resumed_schedule.schedule_id
        )
        retry_claim = store.claim(
            queue=AcquisitionJobType.NIGHT_FINALIZATION_SCAN.value,
            worker_instance="fsa-combined-finalization-retry-worker",
            lease_seconds=60,
        )
        assert retry_claim is not None
        retry_result = finalization_handler(
            WorkContext(retry_claim, store, threading.Event())
        )
        assert retry_result.disposition is WorkDisposition.SUCCEEDED
        assert retry_result.result["night_episode_ids"] == [night_c_episode_id]
        assert datetime.fromisoformat(
            str(retry_result.result["evaluated_at"])
        ) > second_combined_fire.scheduled_for
        assert store.finalize(retry_claim, retry_result) is True

        # Persist a schema-valid adversarial overlap that the ordinary Episode
        # lifecycle cannot create: a second closed Episode with a different
        # canonical wake date, the same pinned binding, and an overlapping
        # collection window. The production boundary must quarantine rather
        # than guess when both candidates are eligible.
        ambiguity_received_at = NOW + timedelta(hours=1, minutes=20)
        uuid7 = UUID7Generator()
        ambiguous_episode_id = uuid7(ambiguity_received_at)
        ambiguous_revision_id = uuid7(ambiguity_received_at)
        ambiguous_opening_identity = "c1b-controlled-ambiguous-opening"
        ambiguous_anchor = episode_anchor_key(
            namespace_id=NAMESPACE,
            namespace_generation=1,
            subject_id=SUBJECT,
            opening_source_idempotency_identity=ambiguous_opening_identity,
        )
        ambiguity_binding_observation_id = str(
            no_match_result.canonical_observation_ids[0]
        )
        with psycopg.connect(admin_dsn) as connection:
            original_episode_row = connection.execute(
                "SELECT episode_json, state, current_revision_number, "
                "cas_version FROM sleep_domain_night_episodes "
                "WHERE night_episode_id = %s",
                (episode_id,),
            ).fetchone()
            assert original_episode_row is not None
            original_episode = NightEpisodeV2.model_validate(
                original_episode_row[0]
            )
            ambiguous_episode = NightEpisodeV2.model_validate(
                {
                    **original_episode.model_dump(mode="python"),
                    "night_episode_id": ambiguous_episode_id,
                    "episode_anchor_key": ambiguous_anchor,
                    "opening_source_idempotency_identity": (
                        ambiguous_opening_identity
                    ),
                    "vendor_wake_local_date": date(2026, 8, 24),
                    "episode_local_date": date(2026, 8, 24),
                    "assignment_basis": EpisodeAssignmentBasis.VENDOR_WAKE_DATE,
                    "date_confidence": EpisodeDateConfidence.VENDOR_ASSERTED,
                    "current_revision": 1,
                    "created_at": ambiguity_received_at,
                    "updated_at": ambiguity_received_at,
                }
            )
            ambiguous_revision_json = {
                "schema_version": "night_episode_revision.v2",
                "night_episode_revision_id": ambiguous_revision_id,
                "night_episode_id": ambiguous_episode_id,
                "subject_id": SUBJECT,
                "revision_number": 1,
                "parent_revision_id": None,
                "revision_cause": "controlled_ambiguity_fixture",
                "observation_ids": [ambiguity_binding_observation_id],
                "policy_versions": default_sleep_slice_policy().policy_versions,
                "episode": ambiguous_episode.model_dump(mode="json"),
            }
            connection.execute(
                """
                INSERT INTO sleep_domain_night_episodes (
                  night_episode_id, namespace_id, data_mode, subject_id,
                  night_key, state, current_revision_id,
                  current_revision_number, cas_version, episode_json,
                  created_at, updated_at, protocol_version, id_scheme,
                  namespace_generation, run_id, arm_id, episode_anchor_key,
                  opening_source_idempotency_identity, timezone_name,
                  boundary_policy_version, collection_start_at,
                  deterministic_close_deadline_at, bed_at, wake_at,
                  bed_local_date, wake_local_date, vendor_wake_local_date,
                  episode_local_date, assignment_basis, date_confidence,
                  assignment_estimated, date_state, date_finalized_at,
                  bed_utc_offset_seconds, wake_utc_offset_seconds,
                  bed_fold, wake_fold, date_conflict, reconciliation_status
                )
                SELECT %s, namespace_id, data_mode, subject_id, %s, state, %s,
                       1, 1, %s::jsonb, %s, %s, protocol_version, id_scheme,
                       namespace_generation, run_id, arm_id, %s, %s,
                       timezone_name, boundary_policy_version,
                       collection_start_at, deterministic_close_deadline_at,
                       bed_at, wake_at, bed_local_date, wake_local_date, %s,
                       %s, 'vendor_wake_date', 'vendor_asserted', FALSE,
                       'finalized', %s, bed_utc_offset_seconds,
                       wake_utc_offset_seconds, bed_fold, wake_fold, FALSE, NULL
                FROM sleep_domain_night_episodes
                WHERE night_episode_id = %s
                """,
                (
                    ambiguous_episode_id,
                    f"v2:{ambiguous_anchor}",
                    ambiguous_revision_id,
                    ambiguous_episode.model_dump_json(),
                    ambiguity_received_at,
                    ambiguity_received_at,
                    ambiguous_anchor,
                    ambiguous_opening_identity,
                    date(2026, 8, 24),
                    date(2026, 8, 24),
                    ambiguity_received_at,
                    episode_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO sleep_domain_night_episode_revisions (
                  night_episode_revision_id, namespace_id, data_mode,
                  night_episode_id, subject_id, revision_number,
                  parent_revision_id, revision_json, created_at,
                  protocol_version, id_scheme, namespace_generation, run_id,
                  arm_id, episode_anchor_key, timezone_name,
                  boundary_policy_version, bed_local_date, wake_local_date,
                  vendor_wake_local_date, episode_local_date, assignment_basis,
                  date_confidence, assignment_estimated, date_state,
                  date_conflict, episode_schema_version
                )
                SELECT %s, namespace_id, data_mode, %s, subject_id, 1, NULL,
                       %s::jsonb, %s, protocol_version, id_scheme,
                       namespace_generation, run_id, arm_id, %s,
                       timezone_name, boundary_policy_version, bed_local_date,
                       wake_local_date, %s, %s, 'vendor_wake_date',
                       'vendor_asserted', FALSE, 'finalized', FALSE,
                       episode_schema_version
                FROM sleep_domain_night_episode_revisions
                WHERE night_episode_revision_id = %s
                """,
                (
                    ambiguous_revision_id,
                    ambiguous_episode_id,
                    json.dumps(ambiguous_revision_json, sort_keys=True),
                    ambiguity_received_at,
                    ambiguous_anchor,
                    date(2026, 8, 24),
                    date(2026, 8, 24),
                    str(episode_after_late[1]),
                ),
            )
            connection.execute(
                """
                INSERT INTO sleep_domain_episode_observation_memberships (
                  membership_id, namespace_id, data_mode, night_episode_id,
                  observation_id, subject_id, device_binding_id,
                  binding_version, event_at, received_at,
                  lateness_watermark_at, late_after_watermark,
                  membership_json, associated_at
                )
                SELECT %s, namespace_id, data_mode, %s, observation_id,
                       subject_id, device_binding_id, binding_version,
                       COALESCE(measurement_at, event_occurred_at), received_at,
                       NULL, FALSE, %s::jsonb, %s
                FROM sleep_domain_canonical_observations
                WHERE observation_id = %s
                """,
                (
                    "c1b-controlled-ambiguity-membership",
                    ambiguous_episode_id,
                    json.dumps(
                        {
                            "schema_version": "controlled_ambiguity_fixture.v1",
                            "night_episode_id": ambiguous_episode_id,
                            "observation_id": ambiguity_binding_observation_id,
                        },
                        sort_keys=True,
                    ),
                    ambiguity_received_at,
                    ambiguity_binding_observation_id,
                ),
            )

        ambiguous_late = PerceptorWebhookService(
            _api_settings(),
            api_uow,
            client_secret=SECRET,
            cipher=cipher,
            now_factory=lambda: ambiguity_received_at,
        ).accept(
            _push_raw(
                message_id="p4d2-b2-late-ambiguous",
                report_at=NOW + timedelta(minutes=6, seconds=30),
                signed_at=ambiguity_received_at,
                local_datetime="2026-08-23T11:06:30.000",
            ),
            content_type="application/json",
            request_path=WEBHOOK_PATH,
        )
        assert ambiguous_late.disposition == "accepted"
        ambiguous_late_result = _process_next(
            store,
            worker_uow,
            cipher,
            worker="p4d2-b2-late-ambiguous-worker",
        )
        assert ambiguous_late_result.canonical_created_count == 4
        with psycopg.connect(admin_dsn) as connection:
            ambiguous_evidence = connection.execute(
                "SELECT count(*), bool_and(status = 'quarantined'), "
                "bool_and(reason_code = "
                "'LATE_ASSOCIATION_RECONCILIATION_REQUIRED'), "
                "bool_and(jsonb_array_length("
                "association_json -> 'candidate_night_episode_ids') = 2) "
                "FROM sleep_domain_pending_episode_associations "
                "WHERE namespace_id = %s AND source_resource_id = ANY(%s)",
                (
                    NAMESPACE,
                    list(ambiguous_late_result.canonical_observation_ids),
                ),
            ).fetchone()
            ambiguity_episode_state = connection.execute(
                "SELECT night_episode_id, current_revision_number, cas_version "
                "FROM sleep_domain_night_episodes "
                "WHERE night_episode_id = ANY(%s) ORDER BY night_episode_id",
                ([episode_id, ambiguous_episode_id],),
            ).fetchall()
            ambiguity_revision_count = connection.execute(
                "SELECT count(*) FROM sleep_domain_night_episode_revisions "
                "WHERE night_episode_id = %s",
                (ambiguous_episode_id,),
            ).fetchone()
        assert ambiguous_evidence == (4, True, True, True)
        assert ambiguity_episode_state == sorted(
            [
                (
                    episode_id,
                    original_episode_row[2],
                    original_episode_row[3],
                ),
                (ambiguous_episode_id, 1, 1),
            ]
        )
        assert ambiguity_revision_count == (1,)

        before_no_data = _count(admin_dsn, "sleep_domain_canonical_observations")
        no_data_ingress = ingress.accept(
            _read(
                SLEEP_REPORT_ENDPOINT,
                _sleep_no_data(),
                requested_at=night_d_close_at + timedelta(minutes=7),
                received_at=night_d_close_at + timedelta(
                    minutes=7, seconds=1
                ),
                envelope_nonce="sleep-no-data-1",
            ),
            binding=binding,
            coordinates=PullRequestCoordinates(
                endpoint=SLEEP_REPORT_ENDPOINT,
                report_date=date(2026, 8, 29),
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
        # The empty report adds neither canonical evidence nor another Episode;
        # the five rows are Nights A/B/C/D plus the controlled ambiguity sentinel.
        assert _count(admin_dsn, "sleep_domain_night_episodes") == 5
        with psycopg.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*), bool_and(is_empty) FROM "
                    "sleep_domain_source_reports WHERE namespace_id = %s",
                    (NAMESPACE,),
                )
                assert cursor.fetchone() == (4, False)
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
                requested_at=NOW + timedelta(days=1, minutes=7),
                received_at=NOW + timedelta(days=1, minutes=7, seconds=1),
                envelope_nonce="sleep-no-data-1",
            ),
            binding=binding,
            coordinates=PullRequestCoordinates(
                endpoint=SLEEP_REPORT_ENDPOINT,
                report_date=date(2026, 8, 23),
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
                assert cursor.fetchone() == (5, 2, 5)

                cursor.execute(
                    "SELECT count(*) FROM sleep_domain_raw_inbox "
                    "WHERE namespace_id = %s",
                    (NAMESPACE,),
                )
                raw_before_binding_period_rejections = int(cursor.fetchone()[0])

        reassignment_at = night_d_close_at + timedelta(hours=3)
        pre_binding_event_at = reassignment_at - timedelta(seconds=30)
        pre_binding_local_text = pre_binding_event_at.astimezone(
            timezone(timedelta(hours=8))
        ).strftime("%Y-%m-%dT%H:%M:%S")
        reassigned_binding = _reassign_binding(
            admin_dsn,
            binding,
            effective_at=reassignment_at,
        )
        pre_binding_coordinates = PullRequestCoordinates(
            endpoint=HISTORY_ENDPOINT,
            window_start_at=reassignment_at - timedelta(minutes=1),
            window_end_at=reassignment_at + timedelta(minutes=1),
        )
        with pytest.raises(
            ValueError,
            match="precedes DeviceBinding effective interval",
        ):
            generation_two_ingress.accept(
                _read(
                    HISTORY_ENDPOINT,
                    _history_data(local_send_time=pre_binding_local_text),
                    requested_at=reassignment_at + timedelta(minutes=30),
                    received_at=reassignment_at + timedelta(minutes=30, seconds=1),
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
                    _history_data(local_send_time=pre_binding_local_text),
                    requested_at=reassignment_at - timedelta(seconds=20),
                    received_at=reassignment_at - timedelta(seconds=19),
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
