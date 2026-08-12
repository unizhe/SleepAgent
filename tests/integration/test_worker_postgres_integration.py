from __future__ import annotations

import hashlib
import json
import os
import threading
from uuid import uuid4

import pytest

from sleepagent.backend_persistence import PostgresProductBackend
from sleepagent.backend_settings import (
    DataMode,
    DeploymentMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.product_api.contracts import ProductRole
from sleepagent.product_api.service import ProductRequestContext
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
)
from sleepagent.worker_runtime import (
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
