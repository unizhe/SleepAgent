from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sleepagent.application.acquisition import (
    AcquisitionJobType,
    AcquisitionScheduleConflict,
    AcquisitionScheduleService,
    PostgresAcquisitionScheduler,
)
from sleepagent.application.device_bindings import (
    DeviceBindingConflict,
    DeviceBindingNotFound,
    DeviceBindingService,
)
from sleepagent.application.night_finalization import (
    NightFinalizationPolicy,
    NightFinalizationService,
    NightFinalizationState,
)
from sleepagent.config import DataMode, ProcessRole
from sleepagent.domain.contracts import ProviderDeviceIdentity
from sleepagent.domain.episodes import UUID7Generator
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
    UowScope,
)
from sleepagent.workers.kernel import WorkContext, WorkDisposition, WorkResult
from sleepagent.workers.runtime import PostgresDurableWorkStore, exact_worker_scope


pytestmark = pytest.mark.postgres
UTC = timezone.utc
API_PRINCIPAL = os.environ.get(
    "SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL", "sleepagent-bff-test"
).strip()
WORKER_PRINCIPAL = os.environ.get(
    "SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL", "sleepagent-worker-test"
).strip()


def _dsn(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} is required for PostgreSQL integration tests")
    return value


def _scope(
    namespace_id: str,
    subject_id: str,
    *,
    process_role: str,
    purpose: str,
    actor_id: str | None = None,
    worker_instance: str | None = None,
) -> UowScope:
    return UowScope(
        namespace_id=namespace_id,
        data_mode="live",
        process_role=process_role,  # type: ignore[arg-type]
        purpose=purpose,
        service_principal_id=(
            API_PRINCIPAL if process_role == "api" else WORKER_PRINCIPAL
        ),
        namespace_generation=1,
        subject_id=subject_id,
        actor_id=actor_id,
        actor_role="elder" if actor_id is not None else None,
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance=worker_instance,
    )


def _provider(dsn: str, name: str, *, max_size: int = 4) -> PsycopgPoolProvider:
    provider = PsycopgPoolProvider.from_dsn(
        dsn,
        configuration=PoolConfiguration(min_size=1, max_size=max_size),
        application_name=name,
    )
    provider.open()
    return provider


def _seed_authority(
    psycopg: object,
    admin_dsn: str,
    *,
    namespace_id: str,
    actor_id: str,
    subject_ids: tuple[str, ...],
    provider_account_id: str,
) -> None:
    with psycopg.connect(admin_dsn) as connection:  # type: ignore[attr-defined]
        with connection.cursor() as cursor:
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
            for index, subject_id in enumerate(subject_ids):
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
                if index < 2:
                    cursor.execute(
                        """
                        INSERT INTO public.backend_actor_subject_bindings (
                          binding_id, namespace_id, data_mode, actor_id,
                          subject_id, role, status, purpose_json, scopes_json,
                          authorization_epoch, valid_from
                        ) VALUES (
                          %s, %s, 'live', %s, %s, 'elder', 'active',
                          '["device_binding_management"]'::jsonb,
                          '["device:binding:manage"]'::jsonb, 1,
                          clock_timestamp() - interval '1 minute'
                        )
                        """,
                        (
                            f"actor-binding-{index}-{uuid4().hex}",
                            namespace_id,
                            actor_id,
                            subject_id,
                        ),
                    )
            grants = (
                (
                    API_PRINCIPAL,
                    "device_binding_management",
                    ["device:binding:manage"],
                    [],
                ),
                (
                    WORKER_PRINCIPAL,
                    "acquisition_schedule",
                    [],
                    [item.value for item in AcquisitionJobType],
                ),
                (
                    WORKER_PRINCIPAL,
                    "worker",
                    [],
                    [*[item.value for item in AcquisitionJobType], "fast_path"],
                ),
            )
            for principal, purpose, scopes, handlers in grants:
                cursor.execute(
                    """
                    INSERT INTO public.backend_principal_grants (
                      grant_id, principal_id, namespace_id, data_mode, purpose,
                      scopes_json, allowed_handlers_json,
                      authorization_epoch, status, valid_from
                    ) VALUES (
                      %s, %s, %s, 'live', %s, %s::jsonb, %s::jsonb,
                      1, 'active', clock_timestamp() - interval '1 minute'
                    )
                    """,
                    (
                        f"grant-{purpose}-{uuid4().hex}",
                        principal,
                        namespace_id,
                        purpose,
                        json.dumps(scopes),
                        json.dumps(handlers),
                    ),
                )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_provider_accounts (
                  namespace_id, data_mode, provider_account_id, provider_id,
                  configuration_fingerprint, status, account_metadata_json,
                  created_at
                ) VALUES (
                  %s, 'live', %s, 'perceptor', %s, 'active', '{}'::jsonb,
                  clock_timestamp()
                )
                """,
                (
                    namespace_id,
                    provider_account_id,
                    hashlib.sha256(provider_account_id.encode()).hexdigest(),
                ),
            )


def _seed_episode(
    psycopg: object,
    admin_dsn: str,
    *,
    namespace_id: str,
    subject_id: str,
    local_date: date,
    date_conflict: bool = False,
) -> tuple[str, str, datetime]:
    episode_id = str(UUID7Generator()())
    revision_id = str(UUID7Generator()())
    bed_at = datetime.combine(local_date - timedelta(days=1), datetime.min.time(), UTC)
    bed_at += timedelta(hours=14)
    wake_at = bed_at + timedelta(hours=4)
    deadline = wake_at + timedelta(hours=1)
    date_state = "conflict" if date_conflict else "finalized"
    assignment_basis = "observed_wake"
    date_confidence = "observed"
    with psycopg.connect(admin_dsn) as connection:  # type: ignore[attr-defined]
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_night_episodes (
                  night_episode_id, namespace_id, data_mode, subject_id,
                  night_key, state, current_revision_id,
                  current_revision_number, cas_version, episode_json,
                  created_at, updated_at, protocol_version, id_scheme,
                  namespace_generation, episode_anchor_key,
                  opening_source_idempotency_identity, timezone_name,
                  boundary_policy_version, collection_start_at,
                  deterministic_close_deadline_at, bed_at, wake_at,
                  bed_local_date, wake_local_date, episode_local_date,
                  assignment_basis, date_confidence, assignment_estimated,
                  date_state, date_finalized_at, bed_utc_offset_seconds,
                  wake_utc_offset_seconds, bed_fold, wake_fold,
                  date_conflict, reconciliation_status
                ) VALUES (
                  %s, %s, 'live', %s, %s, 'closed', %s, 1, 1, '{}'::jsonb,
                  %s, %s, 2, 'uuidv7', 1, %s, %s, 'Asia/Shanghai',
                  'boundary-v1', %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, FALSE, %s, %s, 28800, 28800, 0, 0, %s, %s
                )
                """,
                (
                    episode_id,
                    namespace_id,
                    subject_id,
                    f"night:{episode_id}",
                    revision_id,
                    bed_at,
                    wake_at,
                    f"anchor:{episode_id}",
                    f"opening:{episode_id}",
                    bed_at,
                    deadline,
                    bed_at,
                    wake_at,
                    local_date - timedelta(days=1),
                    local_date,
                    local_date,
                    assignment_basis,
                    date_confidence,
                    date_state,
                    wake_at,
                    date_conflict,
                    "reconciliation_required" if date_conflict else None,
                ),
            )
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_night_episode_revisions (
                  night_episode_revision_id, namespace_id, data_mode,
                  night_episode_id, subject_id, revision_number,
                  revision_json, created_at, protocol_version, id_scheme,
                  namespace_generation, episode_anchor_key, timezone_name,
                  boundary_policy_version, bed_local_date, wake_local_date,
                  episode_local_date, assignment_basis, date_confidence,
                  assignment_estimated, date_state, date_conflict,
                  episode_schema_version
                ) VALUES (
                  %s, %s, 'live', %s, %s, 1, '{}'::jsonb, %s, 2, 'uuidv7',
                  1, %s, 'Asia/Shanghai', 'boundary-v1', %s, %s, %s,
                  %s, %s, FALSE, %s, %s, 'night_episode.v2'
                )
                """,
                (
                    revision_id,
                    namespace_id,
                    episode_id,
                    subject_id,
                    wake_at,
                    f"anchor:{episode_id}",
                    local_date - timedelta(days=1),
                    local_date,
                    local_date,
                    assignment_basis,
                    date_confidence,
                    date_state,
                    date_conflict,
                ),
            )
    return episode_id, revision_id, deadline


def _revise_episode(
    psycopg: object,
    admin_dsn: str,
    *,
    namespace_id: str,
    subject_id: str,
    episode_id: str,
    parent_revision_id: str,
    local_date: date,
) -> str:
    revision_id = str(UUID7Generator()())
    with psycopg.connect(admin_dsn) as connection:  # type: ignore[attr-defined]
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.sleep_domain_night_episode_revisions (
                  night_episode_revision_id, namespace_id, data_mode,
                  night_episode_id, subject_id, revision_number,
                  parent_revision_id, revision_json, created_at,
                  protocol_version, id_scheme, namespace_generation,
                  episode_anchor_key, timezone_name, boundary_policy_version,
                  bed_local_date, wake_local_date, episode_local_date,
                  assignment_basis, date_confidence, assignment_estimated,
                  date_state, date_conflict, episode_schema_version
                ) VALUES (
                  %s, %s, 'live', %s, %s, 2, %s,
                  '{"late_material":true}'::jsonb, clock_timestamp(),
                  2, 'uuidv7', 1, %s, 'Asia/Shanghai', 'boundary-v1',
                  %s, %s, %s, 'observed_wake', 'observed', FALSE,
                  'finalized', FALSE, 'night_episode.v2'
                )
                """,
                (
                    revision_id,
                    namespace_id,
                    episode_id,
                    subject_id,
                    parent_revision_id,
                    f"anchor:{episode_id}",
                    local_date - timedelta(days=1),
                    local_date,
                    local_date,
                ),
            )
            cursor.execute(
                """
                UPDATE public.sleep_domain_night_episodes
                SET current_revision_id = %s, current_revision_number = 2,
                    cas_version = cas_version + 1, updated_at = clock_timestamp()
                WHERE night_episode_id = %s
                """,
                (revision_id, episode_id),
            )
    return revision_id


def test_g7_device_scheduler_and_finalization_authorities() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _dsn("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _dsn("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    worker_dsn = _dsn("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    suffix = uuid4().hex
    namespace_id = f"live:g7-{suffix}"
    actor_id = f"actor-{suffix}"
    subject_one = f"subject-one-{suffix}"
    subject_two = f"subject-two-{suffix}"
    unauthorized_subject = f"subject-denied-{suffix}"
    provider_account_id = f"perceptor-account-{suffix}"
    _seed_authority(
        psycopg,
        admin_dsn,
        namespace_id=namespace_id,
        actor_id=actor_id,
        subject_ids=(subject_one, subject_two, unauthorized_subject),
        provider_account_id=provider_account_id,
    )

    api_provider = _provider(api_dsn, "sleepagent-g7-api")
    worker_provider = _provider(worker_dsn, "sleepagent-g7-worker", max_size=6)
    try:
        api_factory = UnitOfWorkFactory(api_provider)
        worker_factory = UnitOfWorkFactory(worker_provider)
        bindings = DeviceBindingService(api_factory)
        scope_one = _scope(
            namespace_id,
            subject_one,
            process_role="api",
            purpose="device_binding_management",
            actor_id=actor_id,
        )
        scope_two = _scope(
            namespace_id,
            subject_two,
            process_role="api",
            purpose="device_binding_management",
            actor_id=actor_id,
        )
        effective = datetime(2026, 8, 28, 12, tzinfo=UTC)
        identity = ProviderDeviceIdentity(
            provider_device_id=f"device-native-{suffix}",
            provider_device_name="G7 test mat",
            home_id=f"home-{suffix}",
        )
        first = bindings.bind(
            scope_one,
            command_id=f"bind-{suffix}",
            device_id=f"device-{suffix}",
            provider_id="perceptor",
            provider_account_id=provider_account_id,
            provider_device=identity,
            subject_id=subject_one,
            timezone_name="Asia/Shanghai",
            effective_at=effective,
            actor_id=actor_id,
            authorization_id=f"authorization-{suffix}",
            reason="initial_assignment",
        )
        replayed = bindings.bind(
            scope_one,
            command_id=f"bind-{suffix}",
            device_id=f"device-{suffix}",
            provider_id="perceptor",
            provider_account_id=provider_account_id,
            provider_device=identity,
            subject_id=subject_one,
            timezone_name="Asia/Shanghai",
            effective_at=effective,
            actor_id=actor_id,
            authorization_id=f"authorization-{suffix}",
            reason="initial_assignment",
        )
        assert replayed == first
        with pytest.raises(DeviceBindingConflict):
            bindings.bind(
                scope_one,
                command_id=f"bind-{suffix}",
                device_id=f"device-{suffix}",
                provider_id="perceptor",
                provider_account_id=provider_account_id,
                provider_device=identity,
                subject_id=subject_one,
                timezone_name="Asia/Shanghai",
                effective_at=effective,
                actor_id=actor_id,
                authorization_id=f"authorization-{suffix}",
                reason="conflicting_replay",
            )
        assert bindings.validate(
            scope_one, device_binding_id=first.binding.device_binding_id
        ).valid
        with pytest.raises(ValueError, match="IANA"):
            bindings.bind(
                scope_one,
                command_id=f"bad-timezone-{suffix}",
                device_id=f"other-device-{suffix}",
                provider_id="perceptor",
                provider_account_id=provider_account_id,
                provider_device=ProviderDeviceIdentity(
                    provider_device_id=f"other-native-{suffix}"
                ),
                subject_id=subject_one,
                timezone_name="Mars/Olympus_Mons",
                effective_at=effective,
                actor_id=actor_id,
                authorization_id=f"authorization-{suffix}",
                reason="invalid_timezone",
            )
        with pytest.raises(DeviceBindingConflict):
            bindings.bind(
                scope_one,
                command_id=f"overlap-{suffix}",
                device_id=f"device-{suffix}",
                provider_id="perceptor",
                provider_account_id=provider_account_id,
                provider_device=identity,
                subject_id=subject_one,
                timezone_name="Asia/Shanghai",
                effective_at=effective + timedelta(hours=1),
                actor_id=actor_id,
                authorization_id=f"authorization-{suffix}",
                reason="overlap_probe",
            )

        second = bindings.rebind(
            scope_one,
            current=first,
            command_id=f"rebind-same-subject-{suffix}",
            subject_id=subject_one,
            timezone_name="Asia/Shanghai",
            effective_at=effective + timedelta(days=1),
            actor_id=actor_id,
            authorization_id=f"authorization-{suffix}",
            reason="timezone_revalidation",
        )
        assert second.binding.binding_version == 2
        replayed_second = bindings.rebind(
            scope_one,
            current=first,
            command_id=f"rebind-same-subject-{suffix}",
            subject_id=subject_one,
            timezone_name="Asia/Shanghai",
            effective_at=effective + timedelta(days=1),
            actor_id=actor_id,
            authorization_id=f"authorization-{suffix}",
            reason="timezone_revalidation",
        )
        assert replayed_second == second
        assert bindings.show(
            scope_one, device_binding_id=first.binding.device_binding_id
        ).binding.status.value == "ended"
        with pytest.raises(DeviceBindingConflict):
            bindings.rebind(
                scope_one,
                current=first,
                command_id=f"stale-cas-{suffix}",
                subject_id=subject_one,
                timezone_name="Asia/Shanghai",
                effective_at=effective + timedelta(days=2),
                actor_id=actor_id,
                authorization_id=f"authorization-{suffix}",
                reason="stale_cas_probe",
            )

        transferred = bindings.rebind(
            scope_two,
            current=second,
            command_id=f"transfer-{suffix}",
            subject_id=subject_two,
            timezone_name="Asia/Shanghai",
            effective_at=effective + timedelta(days=2),
            actor_id=actor_id,
            authorization_id=f"authorization-{suffix}",
            reason="audited_subject_transfer",
        )
        assert transferred.binding.binding_version == 3
        denied_scope = _scope(
            namespace_id,
            unauthorized_subject,
            process_role="api",
            purpose="device_binding_management",
            actor_id=actor_id,
        )
        with pytest.raises(DeviceBindingNotFound):
            bindings.show(
                denied_scope,
                device_binding_id=transferred.binding.device_binding_id,
            )
        with psycopg.connect(admin_dsn) as admin:
            transfer_audit = admin.execute(
                """
                SELECT action, subject_id, previous_device_binding_id,
                  new_device_binding_id, reason
                FROM public.sleep_domain_device_binding_audit
                WHERE namespace_id = %s AND command_id = %s
                """,
                (namespace_id, f"transfer-{suffix}"),
            ).fetchone()
            historical_json = admin.execute(
                """
                SELECT binding_json ->> 'device_binding_id',
                  binding_json ->> 'subject_id', binding_json ->> 'status'
                FROM public.sleep_domain_device_bindings
                WHERE device_binding_id = %s
                """,
                (second.binding.device_binding_id,),
            ).fetchone()
        assert transfer_audit == (
            "rebound",
            subject_two,
            second.binding.device_binding_id,
            transferred.binding.device_binding_id,
            "audited_subject_transfer",
        )
        assert historical_json == (
            second.binding.device_binding_id,
            subject_one,
            "ended",
        )

        schedules = AcquisitionScheduleService(api_factory)
        due_at = datetime.now(tz=UTC) - timedelta(minutes=1)
        schedule = schedules.create(
            scope_two,
            binding=transferred,
            job_type=AcquisitionJobType.HISTORY_OVERLAP_PULL,
            next_run_at=due_at,
            cadence_seconds=300,
            jitter_seconds=15,
        )
        assert schedules.create(
            scope_two,
            binding=transferred,
            job_type=AcquisitionJobType.HISTORY_OVERLAP_PULL,
            next_run_at=due_at,
            cadence_seconds=300,
            jitter_seconds=15,
        ).schedule_id == schedule.schedule_id
        paused = schedules.pause(
            scope_two, schedule_id=schedule.schedule_id, expected_cas=0
        )
        with pytest.raises(AcquisitionScheduleConflict):
            schedules.pause(
                scope_two, schedule_id=schedule.schedule_id, expected_cas=0
            )
        schedule = schedules.resume(
            scope_two,
            schedule_id=schedule.schedule_id,
            expected_cas=paused.cas_version,
            next_run_at=due_at,
        )
        assert schedule.enabled is True

        schedulers = (
            PostgresAcquisitionScheduler(
                worker_factory,
                data_mode="live",
                service_principal_id=WORKER_PRINCIPAL,
                worker_instance="g7-scheduler-a",
                enabled=True,
            ),
            PostgresAcquisitionScheduler(
                worker_factory,
                data_mode="live",
                service_principal_id=WORKER_PRINCIPAL,
                worker_instance="g7-scheduler-b",
                enabled=True,
            ),
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_results = tuple(
                executor.map(lambda item: item.fire_due(limit=1), schedulers)
            )
        fires = tuple(fire for result in concurrent_results for fire in result)
        assert len(fires) == 1
        fire = fires[0]
        with psycopg.connect(admin_dsn) as admin:
            assert admin.execute(
                "SELECT count(*) FROM public.backend_acquisition_schedule_fires "
                "WHERE schedule_id = %s",
                (schedule.schedule_id,),
            ).fetchone()[0] == 1
            assert admin.execute(
                "SELECT count(*) FROM public.sleep_domain_operations "
                "WHERE operation_id = %s",
                (fire.operation_id,),
            ).fetchone()[0] == 1

        worker_settings = SimpleNamespace(
            process_role=ProcessRole.WORKER,
            data_mode=DataMode.LIVE,
            service_principal_id=WORKER_PRINCIPAL,
        )
        store = PostgresDurableWorkStore(
            worker_settings,
            worker_factory,
            purpose="worker",
        )
        first_claim = store.claim(
            queue=AcquisitionJobType.HISTORY_OVERLAP_PULL.value,
            worker_instance="g7-worker-a",
            lease_seconds=30,
        )
        assert first_claim is not None
        with psycopg.connect(admin_dsn) as admin:
            admin.execute(
                "UPDATE public.sleep_domain_operations "
                "SET lease_expires_at = clock_timestamp() - interval '1 second' "
                "WHERE operation_id = %s",
                (fire.operation_id,),
            )
        recovered_claim = store.claim(
            queue=AcquisitionJobType.HISTORY_OVERLAP_PULL.value,
            worker_instance="g7-worker-b",
            lease_seconds=30,
        )
        assert recovered_claim is not None
        assert recovered_claim.work_id == first_claim.work_id == fire.operation_id
        assert recovered_claim.lease_generation == first_claim.lease_generation + 1
        assert recovered_claim.attempt == first_claim.attempt
        assert exact_worker_scope(
            WorkContext(recovered_claim, store, threading.Event()),
            allowed_handler=AcquisitionJobType.HISTORY_OVERLAP_PULL.value,
        ).purpose == "worker"

        result_scope = store.uow_scope_for_claim(recovered_claim)
        AcquisitionScheduleService(worker_factory).record_result(
            result_scope,
            fire_id=fire.fire_id,
            succeeded=False,
            error_code="transient_provider_timeout",
        )
        assert store.finalize(
            recovered_claim,
            WorkResult(
                disposition=WorkDisposition.RETRYABLE,
                error_code="transient_provider_timeout",
                retry_after_seconds=60,
            ),
        )
        with psycopg.connect(admin_dsn) as admin:
            failed_schedule = admin.execute(
                """
                SELECT consecutive_failures, last_error_code,
                  next_run_at > clock_timestamp()
                FROM public.backend_acquisition_schedules
                WHERE schedule_id = %s
                """,
                (schedule.schedule_id,),
            ).fetchone()
            assert failed_schedule == (1, "transient_provider_timeout", True)
            admin.execute(
                "UPDATE public.backend_acquisition_schedules "
                "SET next_run_at = %s WHERE schedule_id = %s",
                (fire.scheduled_for, schedule.schedule_id),
            )
        duplicate = schedulers[0].fire_due(limit=1)
        assert len(duplicate) == 1
        with psycopg.connect(admin_dsn) as admin:
            assert admin.execute(
                "SELECT count(*) FROM public.backend_acquisition_schedule_fires "
                "WHERE schedule_id = %s AND scheduled_for = %s",
                (schedule.schedule_id, fire.scheduled_for),
            ).fetchone()[0] == 1
            assert admin.execute(
                "SELECT count(*) FROM public.sleep_domain_operations "
                "WHERE operation_id = %s",
                (fire.operation_id,),
            ).fetchone()[0] == 1

        final_scope = _scope(
            namespace_id,
            subject_one,
            process_role="worker",
            purpose="worker",
            worker_instance="g7-finalizer",
        )
        finalizer = NightFinalizationService(
            worker_factory,
            policy=NightFinalizationPolicy(minimum_observation_count=0),
        )
        crossing_date = date(2026, 8, 30)
        episode, episode_revision, deadline = _seed_episode(
            psycopg,
            admin_dsn,
            namespace_id=namespace_id,
            subject_id=subject_one,
            local_date=crossing_date,
        )
        soft = finalizer.finalize(
            final_scope,
            night_episode_id=episode,
            evaluated_at=deadline + timedelta(hours=3),
        )
        assert soft.state is NightFinalizationState.SOFT_FINALIZED
        assert soft.provisional is True
        assert soft.coverage_caveat is not None
        assert soft.reanalysis_operation_id is not None
        initial_claim = store.claim(
            queue="fast_path",
            worker_instance="g7-initial-report-worker",
            lease_seconds=30,
        )
        assert initial_claim is not None
        assert initial_claim.work_id == soft.reanalysis_operation_id
        assert initial_claim.payload["finalization_handoff_kind"] == "initial_report"
        assert store.finalize(
            initial_claim,
            WorkResult(disposition=WorkDisposition.SUCCEEDED),
        ) is True
        late_episode_revision = _revise_episode(
            psycopg,
            admin_dsn,
            namespace_id=namespace_id,
            subject_id=subject_one,
            episode_id=episode,
            parent_revision_id=episode_revision,
            local_date=crossing_date,
        )
        hard = finalizer.finalize(
            final_scope,
            night_episode_id=episode,
            evaluated_at=deadline + timedelta(hours=25),
        )
        assert hard.state is NightFinalizationState.HARD_FINALIZED
        assert (
            hard.parent_finalization_revision_id
            == soft.night_finalization_revision_id
        )
        assert hard.source_night_episode_revision_id == late_episode_revision
        assert hard.revision_cause == "late_material_evidence"
        assert hard.reanalysis_operation_id is not None
        reanalysis_claim = store.claim(
            queue="fast_path",
            worker_instance="g7-reanalysis-worker",
            lease_seconds=30,
        )
        assert reanalysis_claim is not None
        assert reanalysis_claim.work_id == hard.reanalysis_operation_id
        assert reanalysis_claim.payload["finalization_handoff_kind"] == (
            "late_reanalysis"
        )
        assert exact_worker_scope(
            WorkContext(reanalysis_claim, store, threading.Event()),
            allowed_handler="fast_path",
        ).subject_id == subject_one

        direct_episode, _, direct_deadline = _seed_episode(
            psycopg,
            admin_dsn,
            namespace_id=namespace_id,
            subject_id=subject_one,
            local_date=date(2026, 8, 31),
        )
        direct_hard = finalizer.finalize(
            final_scope,
            night_episode_id=direct_episode,
            evaluated_at=direct_deadline + timedelta(hours=25),
        )
        assert direct_hard.state is NightFinalizationState.HARD_FINALIZED
        assert direct_hard.parent_finalization_revision_id is None
        assert direct_hard.reanalysis_operation_id is not None
        direct_replay = finalizer.finalize(
            final_scope,
            night_episode_id=direct_episode,
            evaluated_at=direct_deadline + timedelta(hours=25),
        )
        assert direct_replay == direct_hard
        with psycopg.connect(admin_dsn) as admin:
            assert admin.execute(
                "SELECT count(*) FROM public.sleep_domain_operations "
                "WHERE operation_id = %s AND operation_type = 'fast_path'",
                (direct_hard.reanalysis_operation_id,),
            ).fetchone() == (1,)

        conflict_episode, _, conflict_deadline = _seed_episode(
            psycopg,
            admin_dsn,
            namespace_id=namespace_id,
            subject_id=subject_one,
            local_date=date(2026, 9, 1),
            date_conflict=True,
        )
        reconciliation = finalizer.finalize(
            final_scope,
            night_episode_id=conflict_episode,
            evaluated_at=conflict_deadline,
        )
        assert reconciliation.state is NightFinalizationState.RECONCILIATION_REQUIRED
        with psycopg.connect(admin_dsn) as admin:
            persisted = admin.execute(
                """
                SELECT count(*), min(finalization_revision_number),
                  max(finalization_revision_number)
                FROM public.sleep_domain_night_finalization_revisions
                WHERE night_episode_id = %s
                """,
                (episode,),
            ).fetchone()
            assert persisted == (2, 1, 2)
            assert admin.execute(
                """
                SELECT timezone_name, episode_local_date,
                  (wake_at AT TIME ZONE 'UTC')::date, wake_local_date
                FROM public.sleep_domain_night_episodes
                WHERE night_episode_id = %s
                """,
                (episode,),
            ).fetchone() == (
                "Asia/Shanghai",
                crossing_date,
                date(2026, 8, 29),
                crossing_date,
            )
            admin.execute(
                "UPDATE public.backend_principal_grants SET status = 'revoked' "
                "WHERE namespace_id = %s",
                (namespace_id,),
            )
    finally:
        worker_provider.close()
        api_provider.close()
