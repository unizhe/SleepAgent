from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Literal
from uuid import uuid4

import pytest

from sleepagent.api.postgres import (
    PostgresAuthorityStore,
    PostgresProductBackend,
)
from sleepagent.process import (
    RuntimeServices,
    build_backend_runtime,
)
from sleepagent.config import (
    ApiSurface,
    DataMode,
    DeploymentMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.api.product_contracts import (
    HabitAnswerCommand,
    HabitChangeRequest,
    HabitQuestionSelectionRequest,
    L2ConfirmationRequest,
    MemoryChangeRequest,
    MemoryQueryRequest,
    ProductRole,
)
from sleepagent.api.product import ProductApiError, ProductRequestContext
from sleepagent.persistence.migrations import LATEST_SCHEMA_VERSION
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
)
from sleepagent.runtime.memory import (
    GovernedMemoryItemV2,
    GovernedMemoryState,
)
from tests.support.runtime_fixtures import (
    reset_backend_runtime_state as reset_active_runtime_for_tests,
)


pytestmark = pytest.mark.postgres
UTC = timezone.utc


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
        enabled_surfaces=frozenset(
            {ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT}
        ),
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
        assert runtime.attestation.schema_version == LATEST_SCHEMA_VERSION
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


def test_l2_habit_and_memory_are_governed_append_only_and_durable() -> None:
    """Exercise the canonical L2 backend through a non-owner API role."""

    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _required_environment("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    principal_id = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL",
        "sleepagent-api-test",
    )
    suffix = uuid4().hex
    namespace_id = f"live:l2-{suffix}"
    subject_id = f"subject-{suffix}"
    elder_actor = f"elder-{suffix}"
    family_actor = f"family-{suffix}"
    policy_sha256 = hashlib.sha256(f"policy:{suffix}".encode()).hexdigest()
    scopes = [
        "product:sleep:today:read",
        "product:sleep:interaction:write",
        "product:sleep:care:confirm",
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
            for actor_id, role in (
                (elder_actor, "elder"),
                (family_actor, "family"),
            ):
                cursor.execute(
                    "INSERT INTO public.backend_actors "
                    "(actor_id, actor_kind, status) "
                    "VALUES (%s, 'human', 'active')",
                    (actor_id,),
                )
                cursor.execute(
                    """
                    INSERT INTO public.backend_actor_subject_bindings (
                      binding_id, namespace_id, data_mode, actor_id, subject_id,
                      role, status, purpose_json, scopes_json,
                      authorization_epoch, valid_from
                    ) VALUES (
                      %s, %s, 'live', %s, %s, %s, 'active',
                      '["sleep_care"]'::jsonb, %s::jsonb, 1,
                      clock_timestamp() - interval '1 minute'
                    )
                    """,
                    (
                        f"binding-{role}-{suffix}",
                        namespace_id,
                        actor_id,
                        subject_id,
                        role,
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
                  %s, %s, %s, 'live', 'sleep_care', %s::jsonb,
                  '[]'::jsonb, 1, 'active',
                  clock_timestamp() - interval '1 minute'
                )
                """,
                (
                    f"grant-{suffix}",
                    principal_id,
                    namespace_id,
                    json.dumps(scopes),
                ),
            )

    def context(actor_id: str, role: ProductRole) -> ProductRequestContext:
        return ProductRequestContext(
            service_principal_id=principal_id,
            actor_id=actor_id,
            binding_id=f"binding-{role.value}-{suffix}",
            subject_id=subject_id,
            role=role,
            effective_scopes=frozenset(scopes),
            namespace_id=namespace_id,
            namespace_generation=1,
            data_mode="live",
            run_id=None,
            arm_id=None,
            purpose="sleep_care",
            authorization_epoch=1,
            privacy_epoch=1,
            retrieval_epoch=1,
            policy_sha256=policy_sha256,
        )

    elder = context(elder_actor, ProductRole.ELDER)
    family = context(family_actor, ProductRole.FAMILY)
    clock = [datetime.now(tz=UTC)]
    provider = PsycopgPoolProvider.from_dsn(
        api_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=2),
        application_name="sleepagent-l2-postgres-integration",
    )
    provider.open()
    try:
        backend = PostgresProductBackend(
            UnitOfWorkFactory(provider),
            cursor_key=b"l" * 32,
            now_factory=lambda: clock[0],
        )

        # Case A: empty L2 remains readable and Cold Start can select one question.
        assert backend.get_habit_profile(elder).profile_version == 0
        empty_memory = backend.query_memory(
            elder,
            MemoryQueryRequest(concept_ids=("sleep.preference.care_delivery",)),
        )
        assert empty_memory.receipt["items"] == []

        def propose_habit(
            caller: ProductRequestContext,
            *,
            concept_id: str,
            value: str,
            operation: Literal["remember", "correct"] = "remember",
            target_fact_id: str | None = None,
            direct_observation: bool = False,
        ) -> Any:
            selection = backend.select_habit_questions(
                caller,
                HabitQuestionSelectionRequest(
                    episode_id=f"episode-{uuid4().hex}",
                    candidate_concept_ids=(concept_id,),
                    remaining_episode_budget=1,
                ),
            )
            assert selection.selection["selected_concepts"] == [
                [concept_id, "1.0.0"]
            ]
            answer: dict[str, Any] = {
                "concept_id": concept_id,
                "concept_version": "1.0.0",
                "disposition": "answered",
                "value": value,
            }
            if direct_observation:
                answer.update(
                    direct_observation=True,
                    observation_description="本人昨夜在床边直接观察到该行为",
                    observation_confidence=0.9,
                )
            proposal = backend.propose_habit_changes(
                caller,
                HabitChangeRequest(
                    selection_id=str(selection.selection["selection_id"]),
                    answers=(
                        HabitAnswerCommand(
                            operation=operation,
                            answer=answer,
                            target_fact_id=target_fact_id,
                        ),
                    ),
                    confirmation_actor_id=elder_actor,
                ),
            )
            assert len(proposal.pending_changes) == 1
            return proposal.pending_changes[0]

        def confirm(
            change: Any,
            capability: Literal["habit", "memory"],
        ) -> Any:
            return backend.confirm_l2_change(
                elder,
                L2ConfirmationRequest(
                    change_id=change.change_id,
                    change_hash=change.change_hash,
                    confirmation_handle=change.confirmation_handle,
                ),
                capability=capability,
            )

        primary = propose_habit(
            elder,
            concept_id="habit.primary_goal",
            value="更规律",
        )
        with pytest.raises(ProductApiError) as tampered:
            backend.confirm_l2_change(
                elder,
                L2ConfirmationRequest(
                    change_id=primary.change_id,
                    change_hash="0" * 64,
                    confirmation_handle=primary.confirmation_handle,
                ),
                capability="habit",
            )
        assert tampered.value.status_code == 409
        confirm(primary, "habit")

        elder_observation = propose_habit(
            elder,
            concept_id="habit.nap_pattern",
            value="通常不午睡",
        )
        confirm(elder_observation, "habit")
        before_memory = backend.get_habit_profile(elder)
        assert before_memory.profile_version == 2

        # Cases C/D: correction is append-only; family conflict needs elder HITL.
        clock[0] += timedelta(days=8)
        primary_fact = next(
            item
            for item in before_memory.current_facts
            if item["concept_id"] == "habit.primary_goal"
        )
        corrected = propose_habit(
            elder,
            concept_id="habit.primary_goal",
            value="白天更有精神",
            operation="correct",
            target_fact_id=str(primary_fact["fact_id"]),
        )
        confirm(corrected, "habit")
        family_conflict = propose_habit(
            family,
            concept_id="habit.nap_pattern",
            value="多数天午睡",
            direct_observation=True,
        )
        confirm(family_conflict, "habit")
        habit_profile = backend.get_habit_profile(elder)
        assert habit_profile.profile_version == 4
        assert habit_profile.disputed_concept_ids == ("habit.nap_pattern",)
        corrected_primary = next(
            item
            for item in habit_profile.current_facts
            if item["concept_id"] == "habit.primary_goal"
        )
        assert corrected_primary["value"] == "白天更有精神"

        # Cases E/F: governed remember/correct and durable ReadReceipt.
        remembered = backend.propose_memory_change(
            elder,
            MemoryChangeRequest(
                operation="remember",
                memory_id="memory:delivery",
                concept_id="sleep.preference.care_delivery",
                memory_type="communication_preference",
                value_schema_id="enum.v1",
                typed_value="morning_voice",
                sensitivity_class="personal",
                allowed_roles=("sleep_care", "care_strategy"),
                allowed_purposes=(
                    "care_preference_context",
                    "explicit_memory_review",
                ),
                source_text="请在早晨使用语音提醒",
            ),
        )
        memory_v1 = confirm(remembered, "memory")
        retrieved_v1 = backend.query_memory(
            elder,
            MemoryQueryRequest(concept_ids=("sleep.preference.care_delivery",)),
        )
        assert retrieved_v1.receipt["items"][0]["typed_value"] == "morning_voice"
        assert retrieved_v1.receipt["items"][0]["verified_medical_fact"] is False

        memory_corrected = backend.propose_memory_change(
            elder,
            MemoryChangeRequest(
                operation="correct",
                memory_id="memory:delivery",
                concept_id="sleep.preference.care_delivery",
                memory_type="communication_preference",
                value_schema_id="enum.v1",
                typed_value="evening_light",
                sensitivity_class="personal",
                allowed_roles=("sleep_care", "care_strategy"),
                allowed_purposes=(
                    "care_preference_context",
                    "explicit_memory_review",
                ),
                source_text="改为晚间灯光提醒",
                target_revision_ref=memory_v1.revision_ref,
                target_revision_hash=memory_v1.revision_hash,
            ),
        )
        memory_v2 = confirm(memory_corrected, "memory")
        retrieved_v2 = backend.query_memory(
            elder,
            MemoryQueryRequest(concept_ids=("sleep.preference.care_delivery",)),
        )
        assert [item["typed_value"] for item in retrieved_v2.receipt["items"]] == [
            "evening_light"
        ]

        # Case G plus durable expire: logical forget suppresses retrieval.
        forgotten = backend.propose_memory_change(
            elder,
            MemoryChangeRequest(
                operation="forget",
                memory_id="memory:delivery",
                target_revision_ref=memory_v2.revision_ref,
                target_revision_hash=memory_v2.revision_hash,
            ),
        )
        confirm(forgotten, "memory")
        after_forget = backend.query_memory(
            elder,
            MemoryQueryRequest(concept_ids=("sleep.preference.care_delivery",)),
        )
        assert after_forget.receipt["items"] == []

        environment = backend.propose_memory_change(
            elder,
            MemoryChangeRequest(
                operation="remember",
                memory_id="memory:environment",
                concept_id="sleep.context.environment",
                memory_type="environment",
                value_schema_id="bounded_string.v1",
                typed_value="quiet_room",
                sensitivity_class="personal",
                allowed_roles=("sleep_care", "evidence_reasoning"),
                allowed_purposes=(
                    "personal_evidence_context",
                    "explicit_memory_review",
                ),
                source_text="卧室通常很安静",
            ),
        )
        environment_v1 = confirm(environment, "memory")
        expired = backend.propose_memory_change(
            elder,
            MemoryChangeRequest(
                operation="expire",
                memory_id="memory:environment",
                target_revision_ref=environment_v1.revision_ref,
                target_revision_hash=environment_v1.revision_hash,
            ),
        )
        confirm(expired, "memory")
    finally:
        provider.close()

    # Cross-pool reads prove process-memory is not the source of truth.
    provider = PsycopgPoolProvider.from_dsn(
        api_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=1),
        application_name="sleepagent-l2-postgres-reopen",
    )
    provider.open()
    try:
        reopened = PostgresProductBackend(
            UnitOfWorkFactory(provider),
            cursor_key=b"l" * 32,
            now_factory=lambda: clock[0],
        )
        assert reopened.get_habit_profile(elder).profile_version == 4
        assert reopened.query_memory(
            elder,
            MemoryQueryRequest(
                concept_ids=(
                    "sleep.preference.care_delivery",
                    "sleep.context.environment",
                )
            ),
        ).receipt["items"] == []
    finally:
        provider.close()

    with psycopg.connect(admin_dsn) as admin:
        with admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT revision_json
                FROM public.backend_governed_memory_revisions_v2
                WHERE namespace_id = %s AND data_mode = 'live'
                  AND subject_id = %s
                ORDER BY state_version
                """,
                (namespace_id, subject_id),
            )
            memory_revisions = tuple(
                GovernedMemoryItemV2.model_validate(row[0])
                for row in cursor.fetchall()
            )
            cursor.execute(
                """
                SELECT
                  (SELECT count(*)
                   FROM public.backend_habit_profile_revisions_v2
                   WHERE namespace_id = %s AND subject_id = %s),
                  (SELECT count(*)
                   FROM public.backend_governed_memory_revisions_v2
                   WHERE namespace_id = %s AND subject_id = %s),
                  (SELECT count(*)
                   FROM public.backend_memory_read_receipts_v2
                   WHERE namespace_id = %s AND subject_id = %s)
                """,
                (
                    namespace_id,
                    subject_id,
                    namespace_id,
                    subject_id,
                    namespace_id,
                    subject_id,
                ),
            )
            assert cursor.fetchone() == (4, 5, 5)

    state = GovernedMemoryState(
        subject_id=subject_id,
        version=5,
        revisions=memory_revisions,
    )
    forgotten_audit = tuple(
        item
        for item in state.audit_projection()
        if item["memory_id"] == "memory:delivery"
    )
    assert [item["effective_status"] for item in forgotten_audit] == [
        "superseded",
        "superseded",
        "forgotten",
    ]
    assert all("typed_value" not in item for item in forgotten_audit)

    with psycopg.connect(admin_dsn) as admin:
        with pytest.raises(psycopg.DatabaseError, match="append-only"):
            admin.execute(
                "UPDATE public.backend_habit_profile_revisions_v2 "
                "SET concept_id = concept_id WHERE namespace_id = %s",
                (namespace_id,),
            )
        admin.rollback()
