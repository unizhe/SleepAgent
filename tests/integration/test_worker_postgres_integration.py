from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Iterator
from uuid import uuid4

import pytest

from sleepagent.api.postgres import PostgresProductBackend
from sleepagent.config import (
    DataMode,
    DeploymentMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.api.product_contracts import ProductRole
from sleepagent.api.product import ProductRequestContext
from sleepagent.domain.episodes import UUID7Generator
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
)
from sleepagent.workers.runtime import (
    InvocationDispatcher,
    OutcomeUnknownError,
    PostgresDurableWorkStore,
    WorkDisposition,
    WorkResult,
)


pytestmark = pytest.mark.postgres


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} is required for PostgreSQL integration tests")
    return value


@dataclass(frozen=True)
class _AttemptBudgetHarness:
    psycopg: Any
    admin_dsn: str
    store: PostgresDurableWorkStore
    suffix: str
    namespace_id: str
    subject_id: str
    worker_principal: str
    provider_account_id: str
    operation_queue: str
    delivery_destination: str
    delivery_handler: str


@pytest.fixture
def attempt_budget_harness() -> Iterator[_AttemptBudgetHarness]:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    suffix = uuid4().hex
    namespace_id = f"live:attempt-budget-{suffix}"
    subject_id = f"subject-{suffix}"
    provider_account_id = f"provider-account-{suffix}"
    operation_queue = f"attempt-operation-{suffix}"
    delivery_destination = f"attempt-destination-{suffix}"
    delivery_handler = f"attempt-delivery-{suffix}"
    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.backend_namespaces (
                  namespace_id, data_mode, current_generation, status,
                  synthetic_non_release
                ) VALUES (%s, 'live', 1, 'active', FALSE)
                """,
                (namespace_id,),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_namespace_generations (
                  namespace_id, data_mode, generation, status,
                  configuration_sha256
                ) VALUES (%s, 'live', 1, 'active', %s)
                """,
                (namespace_id, hashlib.sha256(namespace_id.encode()).hexdigest()),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_subjects (
                  namespace_id, data_mode, subject_id, timezone_name, status
                ) VALUES (%s, 'live', %s, 'Asia/Shanghai', 'active')
                """,
                (namespace_id, subject_id),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_subject_epochs (
                  namespace_id, data_mode, subject_id, authorization_epoch,
                  privacy_epoch, retrieval_policy_epoch
                ) VALUES (%s, 'live', %s, 1, 1, 1)
                """,
                (namespace_id, subject_id),
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_provider_accounts (
                  namespace_id, data_mode, provider_account_id, provider_id,
                  configuration_fingerprint, status, account_metadata_json,
                  created_at
                ) VALUES (
                  %s, 'live', %s, 'attempt-provider', %s, 'active',
                  '{}'::jsonb, clock_timestamp()
                )
                """,
                (
                    namespace_id,
                    provider_account_id,
                    hashlib.sha256(provider_account_id.encode()).hexdigest(),
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_principal_grants (
                  grant_id, principal_id, namespace_id, data_mode, purpose,
                  scopes_json, allowed_handlers_json, authorization_epoch,
                  status, valid_from
                ) VALUES (
                  %s, %s, %s, 'live', 'worker', '[]'::jsonb, %s::jsonb,
                  1, 'active', clock_timestamp() - interval '1 minute'
                )
                """,
                (
                    f"worker-grant-{suffix}",
                    worker_principal,
                    namespace_id,
                    json.dumps(
                        [
                            "normalization",
                            operation_queue,
                            delivery_handler,
                            "retention",
                        ]
                    ),
                ),
            )

    settings = SleepBackendSettings(
        profile="attempt-budget-postgres-integration",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.WORKER,
        data_mode=DataMode.LIVE,
        database_dsn=worker_dsn,
        database_identity="integration-database",
        database_role="integration-worker",
        service_principal_id=worker_principal,
        database_scope=DataMode.LIVE,
        namespace_prefixes=("live:",),
        worker_queues=(
            "ingestion",
            operation_queue,
            f"delivery:{delivery_destination}",
            "retention",
        ),
        service_credential_ref="test:worker-service",
        signing_key_ref="test:worker-signing",
        encryption_key_ref="test:worker-encryption",
        pool_min_size=1,
        pool_max_size=3,
    )
    provider = PsycopgPoolProvider.from_dsn(
        worker_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=3),
    )
    provider.open()
    try:
        yield _AttemptBudgetHarness(
            psycopg=psycopg,
            admin_dsn=admin_dsn,
            store=PostgresDurableWorkStore(
                settings,
                UnitOfWorkFactory(provider),
            ),
            suffix=suffix,
            namespace_id=namespace_id,
            subject_id=subject_id,
            worker_principal=worker_principal,
            provider_account_id=provider_account_id,
            operation_queue=operation_queue,
            delivery_destination=delivery_destination,
            delivery_handler=delivery_handler,
        )
    finally:
        provider.close()


def _authorization_snapshot() -> str:
    return json.dumps(
        {
            "authorization_epoch": 1,
            "privacy_epoch": 1,
            "retrieval_policy_epoch": 1,
        }
    )


def _insert_operation(
    harness: _AttemptBudgetHarness,
    *,
    max_attempts: int,
    status: str = "pending",
    queue_name: str | None = None,
) -> str:
    operation_id = UUID7Generator()()
    selected_queue = queue_name or harness.operation_queue
    with harness.psycopg.connect(harness.admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_operations (
                  operation_id, namespace_id, data_mode, operation_type,
                  subject_id, service_principal_id, actor_id,
                  target_resource_id, target_resource_key, idempotency_key,
                  request_sha256, status, attempt_count, cas_version,
                  operation_json, created_at, updated_at, protocol_version,
                  namespace_generation, id_scheme, origin_kind, semantic_key,
                  queue_name, priority, available_at, max_attempts,
                  lease_generation, workload_authorization_snapshot_json,
                  policy_sha256
                ) VALUES (
                  %s, %s, 'live', %s, %s, %s, NULL, NULL, %s, %s, %s,
                  %s, %s, 0, '{}'::jsonb, clock_timestamp(),
                  clock_timestamp(), 2, 1, 'uuidv7', 'system', %s, %s, 0,
                  clock_timestamp(), %s, 0, %s::jsonb, %s
                )
                """,
                (
                    operation_id,
                    harness.namespace_id,
                    selected_queue,
                    harness.subject_id,
                    harness.worker_principal,
                    f"target:{operation_id}",
                    f"idempotency:{operation_id}",
                    hashlib.sha256(operation_id.encode()).hexdigest(),
                    status,
                    1 if status == "succeeded" else 0,
                    f"semantic:{operation_id}",
                    selected_queue,
                    max_attempts,
                    _authorization_snapshot(),
                    hashlib.sha256(f"policy:{operation_id}".encode()).hexdigest(),
                ),
            )
    return operation_id


def _insert_normalization(
    harness: _AttemptBudgetHarness,
    *,
    max_attempts: int,
) -> str:
    raw_id = f"raw-{uuid4().hex}"
    work_id = f"normalization-{uuid4().hex}"
    with harness.psycopg.connect(harness.admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_raw_inbox (
                  raw_ingress_record_id, namespace_id, data_mode, provider_id,
                  provider_account_id, event_type, received_at,
                  signature_verification, idempotency_identity,
                  idempotency_version, pre_normalization_payload_sha256,
                  encrypted_payload, encryption_key_id, encrypted_at,
                  content_type, payload_size_bytes, retention_until,
                  raw_metadata_json, scope_protocol_version,
                  namespace_generation, subject_id
                ) VALUES (
                  %s, %s, 'live', 'attempt-provider', %s, 'attempt.event',
                  clock_timestamp(), 'verified', %s, 'v1', %s, %s,
                  'attempt-key', clock_timestamp(), 'application/json', 2,
                  clock_timestamp() + interval '1 day', '{}'::jsonb, 2, 1, %s
                )
                """,
                (
                    raw_id,
                    harness.namespace_id,
                    harness.provider_account_id,
                    f"identity:{raw_id}",
                    hashlib.sha256(raw_id.encode()).hexdigest(),
                    b"{}",
                    harness.subject_id,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_normalization_work (
                  work_id, namespace_id, data_mode, raw_ingress_record_id,
                  work_generation, status, attempt_count, available_at,
                  work_json, created_at, updated_at, protocol_version,
                  namespace_generation, subject_id,
                  authorization_snapshot_json, max_attempts, lease_generation
                ) VALUES (
                  %s, %s, 'live', %s, 1, 'pending', 0, clock_timestamp(),
                  '{}'::jsonb, clock_timestamp(), clock_timestamp(), 2, 1,
                  %s, %s::jsonb, %s, 0
                )
                """,
                (
                    work_id,
                    harness.namespace_id,
                    raw_id,
                    harness.subject_id,
                    _authorization_snapshot(),
                    max_attempts,
                ),
            )
    return work_id


def _insert_delivery(
    harness: _AttemptBudgetHarness,
    *,
    max_attempts: int,
) -> str:
    origin_id = _insert_operation(
        harness,
        max_attempts=1,
        status="succeeded",
        queue_name=f"delivery-origin-{uuid4().hex}",
    )
    event_id = f"event-{uuid4().hex}"
    intent_id = f"delivery-{uuid4().hex}"
    aggregate_id = f"aggregate-{uuid4().hex}"
    with harness.psycopg.connect(harness.admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_domain_outbox (
                  event_id, namespace_id, data_mode, event_type,
                  aggregate_type, aggregate_id, aggregate_version,
                  per_aggregate_sequence, subject_id, operation_id, status,
                  available_at, event_json, created_at, protocol_version,
                  namespace_generation
                ) VALUES (
                  %s, %s, 'live', 'attempt.event', 'attempt', %s, 1, 1, %s,
                  %s, 'pending', clock_timestamp(), '{}'::jsonb,
                  clock_timestamp(), 2, 1
                )
                """,
                (
                    event_id,
                    harness.namespace_id,
                    aggregate_id,
                    harness.subject_id,
                    origin_id,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_delivery_intents (
                  delivery_intent_id, namespace_id, data_mode,
                  namespace_generation, subject_id, source_event_id,
                  destination, handler_name, semantic_effect_key,
                  aggregate_type, aggregate_id, aggregate_sequence, status,
                  attempt_count, max_attempts, available_at,
                  lease_generation, authorization_snapshot_json,
                  payload_sha256, intent_json
                ) VALUES (
                  %s, %s, 'live', 1, %s, %s, %s, %s, %s, 'attempt', %s,
                  1, 'pending', 0, %s, clock_timestamp(), 0, %s::jsonb, %s,
                  '{}'::jsonb
                )
                """,
                (
                    intent_id,
                    harness.namespace_id,
                    harness.subject_id,
                    event_id,
                    harness.delivery_destination,
                    harness.delivery_handler,
                    f"effect:{intent_id}",
                    aggregate_id,
                    max_attempts,
                    _authorization_snapshot(),
                    hashlib.sha256(intent_id.encode()).hexdigest(),
                ),
            )
    return intent_id


def _insert_retention(
    harness: _AttemptBudgetHarness,
    *,
    max_attempts: int,
) -> str:
    retention_domain = "export_cache"
    job_id = f"retention-{uuid4().hex}"
    with harness.psycopg.connect(harness.admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.backend_retention_deks (
                  namespace_id, data_mode, namespace_generation, subject_id,
                  retention_domain, generation, kek_key_id,
                  wrapping_algorithm, wrapped_dek, dek_sha256, status
                ) VALUES (
                  %s, 'live', 1, %s, %s, 1, 'attempt-kek', 'test-wrap', %s,
                  %s, 'active'
                )
                """,
                (
                    harness.namespace_id,
                    harness.subject_id,
                    retention_domain,
                    b"wrapped-test-key",
                    hashlib.sha256(job_id.encode()).hexdigest(),
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_retention_jobs (
                  retention_job_id, namespace_id, data_mode,
                  namespace_generation, subject_id, retention_domain,
                  dek_generation, semantic_key, job_kind, status,
                  attempt_count, max_attempts, available_at, lease_generation,
                  authorization_snapshot_json, job_json
                ) VALUES (
                  %s, %s, 'live', 1, %s, %s, 1, %s, 'key_rotation',
                  'pending', 0, %s, clock_timestamp(), 0, %s::jsonb,
                  '{}'::jsonb
                )
                """,
                (
                    job_id,
                    harness.namespace_id,
                    harness.subject_id,
                    retention_domain,
                    f"semantic:{job_id}",
                    max_attempts,
                    _authorization_snapshot(),
                ),
            )
    return job_id


def _queue_and_work_id(
    harness: _AttemptBudgetHarness,
    kind: str,
    *,
    max_attempts: int,
) -> tuple[str, str]:
    if kind == "normalization":
        return "ingestion", _insert_normalization(
            harness, max_attempts=max_attempts
        )
    if kind == "operation":
        return harness.operation_queue, _insert_operation(
            harness, max_attempts=max_attempts
        )
    if kind == "delivery":
        return f"delivery:{harness.delivery_destination}", _insert_delivery(
            harness, max_attempts=max_attempts
        )
    return "retention", _insert_retention(harness, max_attempts=max_attempts)


def _expire_work(
    harness: _AttemptBudgetHarness,
    *,
    kind: str,
    work_id: str,
) -> None:
    table, identifier = {
        "normalization": ("sleep_domain_normalization_work", "work_id"),
        "operation": ("sleep_domain_operations", "operation_id"),
        "delivery": ("backend_delivery_intents", "delivery_intent_id"),
        "retention": ("backend_retention_jobs", "retention_job_id"),
    }[kind]
    with harness.psycopg.connect(harness.admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                f"UPDATE public.{table} "
                "SET lease_expires_at = clock_timestamp() - interval '1 second' "
                f"WHERE {identifier} = %s",
                (work_id,),
            )
            assert cursor.rowcount == 1


def test_worker_role_enforces_fence_and_persists_unknown_invocation() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    worker_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    api_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL",
        "sleepagent-api-test",
    )
    worker_principal = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL",
        "sleepagent-worker-test",
    )
    suffix = uuid4().hex
    namespace_id = f"live:worker-{suffix}"
    actor_id = f"actor-{suffix}"
    subject_id = f"subject-{suffix}"
    binding_id = f"binding-{suffix}"
    api_scopes = [
        "product:sleep:interaction:write",
        "product:sleep:operation:read",
    ]

    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.backend_namespaces (
                  namespace_id, data_mode, current_generation, status,
                  synthetic_non_release
                ) VALUES (%s, 'live', 1, 'active', FALSE)
                """,
                (namespace_id,),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_namespace_generations (
                  namespace_id, data_mode, generation, status,
                  configuration_sha256
                ) VALUES (%s, 'live', 1, 'active', %s)
                """,
                (namespace_id, hashlib.sha256(namespace_id.encode()).hexdigest()),
            )
            cursor.execute(
                "INSERT INTO public.backend_actors "
                "(actor_id, actor_kind, status) VALUES (%s, 'human', 'active')",
                (actor_id,),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_subjects (
                  namespace_id, data_mode, subject_id, timezone_name, status
                ) VALUES (%s, 'live', %s, 'Asia/Shanghai', 'active')
                """,
                (namespace_id, subject_id),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_subject_epochs (
                  namespace_id, data_mode, subject_id, authorization_epoch,
                  privacy_epoch, retrieval_policy_epoch
                ) VALUES (%s, 'live', %s, 1, 1, 1)
                """,
                (namespace_id, subject_id),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_actor_subject_bindings (
                  binding_id, namespace_id, data_mode, actor_id, subject_id,
                  role, status, purpose_json, scopes_json,
                  authorization_epoch, valid_from
                ) VALUES (
                  %s, %s, 'live', %s, %s, 'elder', 'active',
                  '["sleep_care"]'::jsonb, %s::jsonb, 1,
                  clock_timestamp() - interval '1 minute'
                )
                """,
                (
                    binding_id,
                    namespace_id,
                    actor_id,
                    subject_id,
                    json.dumps(api_scopes),
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_principal_grants (
                  grant_id, principal_id, namespace_id, data_mode, purpose,
                  scopes_json, allowed_handlers_json, authorization_epoch,
                  status, valid_from
                ) VALUES (
                  %s, %s, %s, 'live', 'sleep_care', %s::jsonb, '[]'::jsonb,
                  1, 'active', clock_timestamp() - interval '1 minute'
                )
                """,
                (
                    f"api-grant-{suffix}",
                    api_principal,
                    namespace_id,
                    json.dumps(api_scopes),
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.backend_principal_grants (
                  grant_id, principal_id, namespace_id, data_mode, purpose,
                  scopes_json, allowed_handlers_json, authorization_epoch,
                  status, valid_from
                ) VALUES (
                  %s, %s, %s, 'live', 'worker', '[]'::jsonb,
                      '["product_interaction"]'::jsonb,
                  1, 'active', clock_timestamp() - interval '1 minute'
                )
                """,
                (f"worker-grant-{suffix}", worker_principal, namespace_id),
            )

    api_provider = PsycopgPoolProvider.from_dsn(
        api_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=1),
    )
    api_provider.open()
    try:
        context = ProductRequestContext(
            service_principal_id=api_principal,
            actor_id=actor_id,
            binding_id=binding_id,
            subject_id=subject_id,
            role=ProductRole.ELDER,
            effective_scopes=frozenset(api_scopes),
            namespace_id=namespace_id,
            namespace_generation=1,
            data_mode="live",
            run_id=None,
            arm_id=None,
            purpose="sleep_care",
            authorization_epoch=1,
            privacy_epoch=1,
            retrieval_epoch=1,
            policy_sha256="a" * 64,
        )
        backend = PostgresProductBackend(
            UnitOfWorkFactory(api_provider), cursor_key=b"w" * 32
        )
        first_operation = backend.reserve_command(
            context,
            route_template="/product/sleep/interactions/start",
            command_type="interaction.start",
            idempotency_key=f"first-{suffix}",
            body_sha256="b" * 64,
            payload={"message": "checkpoint proof"},
            target_id=None,
        )
        second_operation = backend.reserve_command(
            context,
            route_template="/product/sleep/interactions/ask",
            command_type="interaction.ask",
            idempotency_key=f"second-{suffix}",
            body_sha256="c" * 64,
            payload={"message": "unknown outcome proof"},
            target_id=f"interaction-{suffix}",
        )
    finally:
        api_provider.close()

    worker_settings = SleepBackendSettings(
        profile="postgres-worker-integration",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.WORKER,
        data_mode=DataMode.LIVE,
        database_dsn=worker_dsn,
        database_identity="integration-database",
        database_role="integration-worker",
        service_principal_id=worker_principal,
        database_scope=DataMode.LIVE,
        namespace_prefixes=("live:",),
        worker_queues=("product_interaction",),
        service_credential_ref="test:worker-service",
        signing_key_ref="test:worker-signing",
        encryption_key_ref="test:worker-encryption",
        pool_min_size=1,
        pool_max_size=3,
    )
    worker_provider = PsycopgPoolProvider.from_dsn(
        worker_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=3),
    )
    worker_provider.open()
    try:
        store = PostgresDurableWorkStore(
            worker_settings,
            UnitOfWorkFactory(worker_provider),
        )
        assert store.healthcheck(worker_instance=f"health-{suffix}") is True
        first_claim = store.claim(
            queue="product_interaction",
            worker_instance=f"worker-{suffix}",
            lease_seconds=30,
        )
        assert first_claim is not None
        assert first_claim.work_id == first_operation
        assert first_claim.metadata["lease_seconds"] == 30
        assert store.checkpoint(
            first_claim,
            checkpoint_type="postgres_integration",
            payload={"opaque": True},
        ) is True
        stale = first_claim.model_copy(
            update={"fencing_token": "00000000-0000-4000-8000-000000000000"}
        )
        assert store.checkpoint(
            stale,
            checkpoint_type="stale_must_not_commit",
            payload={"opaque": False},
        ) is False
        assert store.finalize(
            first_claim,
            WorkResult(disposition=WorkDisposition.SUCCEEDED),
        ) is True

        second_claim = store.claim(
            queue="product_interaction",
            worker_instance=f"worker-{suffix}",
            lease_seconds=30,
        )
        assert second_claim is not None
        assert second_claim.work_id == second_operation
        dispatcher = InvocationDispatcher(
            store=store,
            claim=second_claim,
            lease_lost=threading.Event(),
        )

        def ambiguous_sender():
            raise ConnectionError("connection lost after possible send")

        with pytest.raises(OutcomeUnknownError) as unknown:
            dispatcher.dispatch(
                invocation_key=f"model:{suffix}",
                request={"opaque_prompt_hash": "d" * 64},
                sender=ambiguous_sender,
            )
        assert store.finalize(
            second_claim,
            WorkResult(
                disposition=WorkDisposition.OUTCOME_UNKNOWN,
                error_code=unknown.value.code,
            ),
        ) is True
    finally:
        worker_provider.close()

    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM public.sleep_domain_operations "
                "WHERE operation_id = %s",
                (first_operation,),
            )
            assert cursor.fetchone() == ("succeeded",)
            cursor.execute(
                "SELECT array_agg(checkpoint_kind ORDER BY checkpoint_sequence) "
                "FROM public.backend_operation_checkpoints "
                "WHERE operation_id = %s",
                (first_operation,),
            )
            checkpoint_kinds = cursor.fetchone()[0]
            assert checkpoint_kinds == [
                "postgres_integration",
                "handler_finalize",
            ]
            cursor.execute(
                """
                SELECT operation.status, invocation.current_state,
                       array_agg(journal.to_state ORDER BY journal.sequence)
                FROM public.sleep_domain_operations AS operation
                JOIN public.backend_invocations AS invocation
                  ON invocation.operation_id = operation.operation_id
                JOIN public.backend_invocation_journal AS journal
                  ON journal.invocation_id = invocation.invocation_id
                WHERE operation.operation_id = %s
                GROUP BY operation.status, invocation.current_state
                """,
                (second_operation,),
            )
            assert cursor.fetchone() == (
                "outcome_unknown",
                "outcome_unknown",
                ["reserved", "send_started", "outcome_unknown"],
            )


@pytest.mark.parametrize(
    "kind",
    ["normalization", "operation", "delivery", "retention"],
)
def test_expired_claim_reuses_the_same_business_attempt(
    attempt_budget_harness: _AttemptBudgetHarness,
    kind: str,
) -> None:
    queue, work_id = _queue_and_work_id(
        attempt_budget_harness,
        kind,
        max_attempts=1,
    )

    first = attempt_budget_harness.store.claim(
        queue=queue,
        worker_instance=f"{kind}-worker-1",
        lease_seconds=30,
    )
    assert first is not None
    assert first.work_id == work_id
    assert (first.attempt, first.max_attempts, first.lease_generation) == (1, 1, 1)

    _expire_work(attempt_budget_harness, kind=kind, work_id=work_id)
    second = attempt_budget_harness.store.claim(
        queue=queue,
        worker_instance=f"{kind}-worker-2",
        lease_seconds=30,
    )
    assert second is not None
    assert second.work_id == work_id
    assert second.attempt == first.attempt == 1
    assert second.lease_generation == first.lease_generation + 1
    assert second.fencing_token != first.fencing_token

    assert attempt_budget_harness.store.finalize(
        first,
        WorkResult(disposition=WorkDisposition.SUCCEEDED),
    ) is False
    assert attempt_budget_harness.store.finalize(
        second,
        WorkResult(disposition=WorkDisposition.SUCCEEDED),
    ) is True


class _SimulatedProcessCrash(BaseException):
    pass


def test_delivery_reclaim_preserves_send_started_reconciliation_boundary(
    attempt_budget_harness: _AttemptBudgetHarness,
) -> None:
    queue, work_id = _queue_and_work_id(
        attempt_budget_harness,
        "delivery",
        max_attempts=1,
    )
    first = attempt_budget_harness.store.claim(
        queue=queue,
        worker_instance="delivery-crash-worker-1",
        lease_seconds=30,
    )
    assert first is not None
    first_dispatcher = InvocationDispatcher(
        store=attempt_budget_harness.store,
        claim=first,
        lease_lost=threading.Event(),
    )

    def crash_after_send_started():
        raise _SimulatedProcessCrash

    with pytest.raises(_SimulatedProcessCrash):
        first_dispatcher.dispatch(
            invocation_key=f"delivery:{attempt_budget_harness.suffix}",
            request={"payload_sha256": "a" * 64},
            sender=crash_after_send_started,
        )

    _expire_work(
        attempt_budget_harness,
        kind="delivery",
        work_id=work_id,
    )
    second = attempt_budget_harness.store.claim(
        queue=queue,
        worker_instance="delivery-crash-worker-2",
        lease_seconds=30,
    )
    assert second is not None
    assert second.attempt == first.attempt == 1
    assert second.lease_generation == 2
    assert second.fencing_token != first.fencing_token
    assert attempt_budget_harness.store.finalize(
        first,
        WorkResult(disposition=WorkDisposition.SUCCEEDED),
    ) is False

    resend_calls = 0

    def forbidden_blind_resend():
        nonlocal resend_calls
        resend_calls += 1
        return {"delivered": True}, None

    second_dispatcher = InvocationDispatcher(
        store=attempt_budget_harness.store,
        claim=second,
        lease_lost=threading.Event(),
    )
    with pytest.raises(
        OutcomeUnknownError,
        match="prior_send_requires_reconciliation",
    ):
        second_dispatcher.dispatch(
            invocation_key=f"delivery:{attempt_budget_harness.suffix}",
            request={"payload_sha256": "a" * 64},
            sender=forbidden_blind_resend,
        )
    assert resend_calls == 0
    assert attempt_budget_harness.store.finalize(
        second,
        WorkResult(disposition=WorkDisposition.OUTCOME_UNKNOWN),
    ) is True
    assert attempt_budget_harness.store.claim(
        queue=queue,
        worker_instance="delivery-must-not-reclaim-outcome-unknown",
        lease_seconds=30,
    ) is None

    with attempt_budget_harness.psycopg.connect(
        attempt_budget_harness.admin_dsn
    ) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT intent.status, invocation.current_state,
                       array_agg(journal.to_state ORDER BY journal.sequence)
                FROM public.backend_delivery_intents AS intent
                JOIN public.backend_invocations AS invocation
                  ON invocation.operation_id = (
                    SELECT operation_id
                    FROM public.sleep_domain_domain_outbox
                    WHERE event_id = intent.source_event_id
                  )
                JOIN public.backend_invocation_journal AS journal
                  ON journal.invocation_id = invocation.invocation_id
                WHERE intent.delivery_intent_id = %s
                GROUP BY intent.status, invocation.current_state
                """,
                (work_id,),
            )
            assert cursor.fetchone() == (
                "outcome_unknown",
                "send_started",
                ["reserved", "send_started"],
            )


def test_business_retry_advances_attempt_and_last_attempt_can_be_reclaimed(
    attempt_budget_harness: _AttemptBudgetHarness,
) -> None:
    queue, work_id = _queue_and_work_id(
        attempt_budget_harness,
        "operation",
        max_attempts=2,
    )
    first = attempt_budget_harness.store.claim(
        queue=queue,
        worker_instance="business-attempt-worker-1",
        lease_seconds=30,
    )
    assert first is not None
    assert (first.attempt, first.lease_generation) == (1, 1)
    assert attempt_budget_harness.store.finalize(
        first,
        WorkResult(
            disposition=WorkDisposition.RETRYABLE,
            error_code="retryable_business_failure",
            retry_after_seconds=0,
        ),
    ) is True

    second = attempt_budget_harness.store.claim(
        queue=queue,
        worker_instance="business-attempt-worker-2",
        lease_seconds=30,
    )
    assert second is not None
    assert second.work_id == work_id
    assert (second.attempt, second.lease_generation) == (2, 2)

    _expire_work(
        attempt_budget_harness,
        kind="operation",
        work_id=work_id,
    )
    reclaimed = attempt_budget_harness.store.claim(
        queue=queue,
        worker_instance="business-attempt-worker-3",
        lease_seconds=30,
    )
    assert reclaimed is not None
    assert reclaimed.attempt == second.attempt == 2
    assert reclaimed.lease_generation == second.lease_generation + 1
    assert reclaimed.fencing_token != second.fencing_token
    assert attempt_budget_harness.store.finalize(
        second,
        WorkResult(disposition=WorkDisposition.SUCCEEDED),
    ) is False
    assert attempt_budget_harness.store.finalize(
        reclaimed,
        WorkResult(disposition=WorkDisposition.SUCCEEDED),
    ) is True


def test_repeated_reclaim_only_advances_execution_generation(
    attempt_budget_harness: _AttemptBudgetHarness,
) -> None:
    queue, work_id = _queue_and_work_id(
        attempt_budget_harness,
        "operation",
        max_attempts=1,
    )
    claims = []
    for generation in (1, 2, 3):
        claim = attempt_budget_harness.store.claim(
            queue=queue,
            worker_instance=f"reclaim-worker-{generation}",
            lease_seconds=30,
        )
        assert claim is not None
        assert claim.work_id == work_id
        assert claim.attempt == 1
        assert claim.lease_generation == generation
        claims.append(claim)
        if generation < 3:
            _expire_work(
                attempt_budget_harness,
                kind="operation",
                work_id=work_id,
            )

    assert len({claim.fencing_token for claim in claims}) == 3
    for stale in claims[:-1]:
        assert attempt_budget_harness.store.finalize(
            stale,
            WorkResult(disposition=WorkDisposition.SUCCEEDED),
        ) is False
    assert attempt_budget_harness.store.finalize(
        claims[-1],
        WorkResult(disposition=WorkDisposition.SUCCEEDED),
    ) is True
