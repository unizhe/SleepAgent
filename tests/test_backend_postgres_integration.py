from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sleepagent.backend_persistence import (
    PostgresAuthorityStore,
    PostgresProductBackend,
)
from sleepagent.backend_runtime import (
    RuntimeServices,
    build_backend_runtime,
    reset_active_runtime_for_tests,
)
from sleepagent.backend_settings import (
    DataMode,
    DeploymentMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.product_api.contracts import ProductRole
from sleepagent.product_api.service import ProductApiError, ProductRequestContext
from sleepagent.radar_agent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
)


pytestmark = pytest.mark.postgres


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} is required for PostgreSQL integration tests")
    return value


def test_non_owner_api_runtime_can_attest_the_migration_ledger() -> None:
    psycopg = pytest.importorskip("psycopg")
    api_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    principal_id = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL",
        "sleepagent-api-test",
    )
    with psycopg.connect(api_dsn) as connection:
        database_identity, database_role = connection.execute(
            "SELECT current_database(), current_user"
        ).fetchone()
    settings = SleepBackendSettings(
        profile="postgres-integration",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.API,
        data_mode=DataMode.LIVE,
        database_dsn=api_dsn,
        database_identity=str(database_identity),
        database_role=str(database_role),
        service_principal_id=principal_id,
        database_scope=DataMode.LIVE,
        namespace_prefixes=("live:",),
        enabled_surfaces=frozenset(),
        signing_key_ref="test:postgres-signing",
        encryption_key_ref="test:postgres-encryption",
        pool_min_size=1,
        pool_max_size=1,
    )
    reset_active_runtime_for_tests()
    runtime = build_backend_runtime(settings, services=RuntimeServices())
    try:
        asyncio.run(runtime.start())
        assert runtime.readiness()["ready"] is True
        assert runtime.attestation is not None
        assert runtime.attestation.schema_version >= 25
    finally:
        asyncio.run(runtime.close())
        reset_active_runtime_for_tests()


def test_non_owner_authority_and_product_command_are_rls_scoped_and_atomic() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    principal_id = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL",
        "sleepagent-api-test",
    )
    suffix = uuid4().hex
    namespace_id = f"live:postgres-{suffix}"
    actor_id = f"actor-{suffix}"
    subject_id = f"subject-{suffix}"
    binding_id = f"binding-{suffix}"
    grant_id = f"grant-{suffix}"
    idempotency_key = f"command-{suffix}"
    scopes = [
        "product:sleep:today:read",
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
                    json.dumps(scopes),
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
                (grant_id, principal_id, namespace_id, json.dumps(scopes)),
            )

    provider = PsycopgPoolProvider.from_dsn(
        api_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=2),
        application_name="sleepagent-postgres-integration",
    )
    provider.open()
    try:
        factory = UnitOfWorkFactory(provider)
        settings = SimpleNamespace(
            data_mode=SimpleNamespace(value="live"),
            service_principal_id=principal_id,
        )
        authority = PostgresAuthorityStore(settings, factory)
        resolved = authority.resolve(
            actor_id=actor_id,
            subject_id=subject_id,
            role=ProductRole.ELDER,
            purpose="sleep_care",
        )
        assert resolved.namespace_id == namespace_id
        assert resolved.effective_scopes == frozenset(scopes)

        context = ProductRequestContext(
            service_principal_id=principal_id,
            actor_id=actor_id,
            binding_id=binding_id,
            subject_id=subject_id,
            role=ProductRole.ELDER,
            effective_scopes=resolved.effective_scopes,
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
        backend = PostgresProductBackend(factory, cursor_key=b"c" * 32)
        operation_id = backend.reserve_command(
            context,
            route_template="/product/sleep/interactions/start",
            command_type="interaction.start",
            idempotency_key=idempotency_key,
            body_sha256="b" * 64,
            payload={"message": "integration"},
            target_id=None,
        )
        repeated = backend.reserve_command(
            context,
            route_template="/product/sleep/interactions/start",
            command_type="interaction.start",
            idempotency_key=idempotency_key,
            body_sha256="b" * 64,
            payload={"message": "integration"},
            target_id=None,
        )
        assert repeated == operation_id
        equivalent = backend.reserve_command(
            context,
            route_template="/product/sleep/interactions/start",
            command_type="interaction.start",
            idempotency_key=f"{idempotency_key}-equivalent",
            body_sha256="b" * 64,
            payload={"message": "integration"},
            target_id=None,
        )
        assert equivalent == operation_id

        def submit_equivalent(index: int) -> str:
            return backend.reserve_command(
                context,
                route_template="/product/sleep/interactions/ask",
                command_type="interaction.ask",
                idempotency_key=f"{idempotency_key}-concurrent-{index}",
                body_sha256="e" * 64,
                payload={"message": "same concurrent semantic command"},
                target_id="interaction-concurrent",
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            concurrent_operations = tuple(executor.map(submit_equivalent, range(4)))
        assert len(set(concurrent_operations)) == 1
        concurrent_operation_id = concurrent_operations[0]

        with pytest.raises(ProductApiError, match="another body") as conflict:
            backend.reserve_command(
                context,
                route_template="/product/sleep/interactions/start",
                command_type="interaction.start",
                idempotency_key=idempotency_key,
                body_sha256="d" * 64,
                payload={"message": "changed"},
                target_id=None,
            )
        assert conflict.value.status_code == 409
    finally:
        provider.close()

    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                  (SELECT count(*) FROM public.backend_command_receipts
                   WHERE operation_id = %s),
                  (SELECT count(*) FROM public.sleep_domain_operations
                   WHERE operation_id = %s),
                  (SELECT count(*) FROM public.sleep_domain_domain_outbox
                   WHERE operation_id = %s)
                """,
                (operation_id, operation_id, operation_id),
            )
            assert cursor.fetchone() == (2, 1, 1)
            cursor.execute(
                """
                SELECT
                  (SELECT count(*) FROM public.backend_command_receipts
                   WHERE operation_id = %s),
                  (SELECT count(*) FROM public.sleep_domain_operations
                   WHERE operation_id = %s),
                  (SELECT count(*) FROM public.sleep_domain_domain_outbox
                   WHERE operation_id = %s)
                """,
                (
                    concurrent_operation_id,
                    concurrent_operation_id,
                    concurrent_operation_id,
                ),
            )
            assert cursor.fetchone() == (4, 1, 1)
            cursor.execute(
                "UPDATE public.backend_actor_subject_bindings "
                "SET status = 'revoked' WHERE binding_id = %s",
                (binding_id,),
            )

    provider = PsycopgPoolProvider.from_dsn(
        api_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=1),
    )
    provider.open()
    try:
        authority = PostgresAuthorityStore(
            SimpleNamespace(
                data_mode=SimpleNamespace(value="live"),
                service_principal_id=principal_id,
            ),
            UnitOfWorkFactory(provider),
        )
        with pytest.raises(ProductApiError) as revoked:
            authority.resolve(
                actor_id=actor_id,
                subject_id=subject_id,
                role=ProductRole.ELDER,
                purpose="sleep_care",
            )
        assert revoked.value.status_code == 403
    finally:
        provider.close()
