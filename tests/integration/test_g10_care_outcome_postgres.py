from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sleepagent.api.postgres import PostgresProductBackend
from sleepagent.app import PostgresInternalStatus
from sleepagent.application.care_execution import (
    CarePlanApplicationService,
)
from sleepagent.config import (
    DataMode,
    DeploymentMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.domain.care_outcomes import CARE_OUTCOME_QUEUE
from sleepagent.infrastructure.postgres_care_execution import (
    PostgresCarePlanRepository,
)
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
    UowScope,
)
from sleepagent.persistence.migrate import PostgresMigrationRunner
from sleepagent.persistence.migrations import LATEST_SCHEMA_VERSION
from sleepagent.workers.kernel import WorkContext, WorkDisposition
from sleepagent.workers.outcomes import CareOutcomeWorkHandler
from sleepagent.workers.runtime import PostgresDurableWorkStore
from tests.integration.test_g9_care_execution_postgres import (
    _contexts,
    _seed_subject_and_proposal,
    _terminal_process,
)
from tests.integration.test_observation_semantics_v2_postgres import (
    _apply_prefix,
    _drop_database,
    _temporary_database,
)


pytestmark = pytest.mark.postgres
UTC = timezone.utc


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} is required for PostgreSQL integration tests")
    return value


def _worker_settings(dsn: str, principal: str) -> SleepBackendSettings:
    return SleepBackendSettings(
        profile="g10-outcome-worker",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.WORKER,
        data_mode=DataMode.LIVE,
        database_dsn=dsn,
        database_identity="sleepagent_replay_test",
        database_role="sleepagent_test_worker",
        service_principal_id=principal,
        database_scope=DataMode.LIVE,
        namespace_prefixes=("live:",),
        worker_queues=(CARE_OUTCOME_QUEUE,),
        service_credential_ref="test:g10-worker",
        signing_key_ref="test:g10-signing",
        encryption_key_ref="test:g10-encryption",
        pool_min_size=1,
        pool_max_size=3,
    )


def _run_one(store: PostgresDurableWorkStore) -> str | None:
    claim = store.claim(
        queue=CARE_OUTCOME_QUEUE,
        worker_instance=f"g10-worker-{uuid4().hex}",
        lease_seconds=30,
    )
    if claim is None:
        return None
    result = CareOutcomeWorkHandler()(
        WorkContext(claim, store, threading.Event())
    )
    assert result.disposition is WorkDisposition.SUCCEEDED
    assert store.finalize(claim, result)
    return str(result.result["lifecycle_state"])


def _insert_night(
    connection: object,
    *,
    namespace_id: str,
    subject_id: str,
    suffix: str,
    wake_at: datetime,
    finalization_revision_number: int = 1,
    parent_finalization_revision_id: str | None = None,
    data_mode: str = "live",
    run_id: str | None = None,
    arm_id: str | None = None,
) -> tuple[str, str]:
    episode_id = f"g10-night-{suffix}"
    episode_revision_id = f"g10-episode-revision-{suffix}"
    finalization_id = f"g10-finalization-{suffix}"
    finalization_revision_id = f"g10-finalization-revision-{suffix}"
    bed_at = wake_at - timedelta(hours=8)
    episode_payload = {
        "episode": {
            "wake_at": wake_at.isoformat(),
            "bed_at": bed_at.isoformat(),
            "timezone_name": "Asia/Shanghai",
            "assignment_basis": "observed_wake",
        }
    }
    cursor = connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_episodes (
              night_episode_id, namespace_id, data_mode, subject_id, night_key,
              state, current_revision_id, current_revision_number, cas_version,
              episode_json, created_at, updated_at, namespace_generation,
              run_id, arm_id, timezone_name, episode_local_date, bed_at, wake_at
            ) VALUES (%s,%s,%s,%s,%s,'closed',%s,1,1,%s::jsonb,%s,%s,1,
              %s,%s,'Asia/Shanghai',%s,%s,%s)
            """,
            (
                episode_id,
                namespace_id,
                data_mode,
                subject_id,
                wake_at.date().isoformat(),
                episode_revision_id,
                json.dumps(episode_payload["episode"]),
                wake_at,
                wake_at,
                run_id,
                arm_id,
                wake_at.date(),
                bed_at,
                wake_at,
            ),
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_episode_revisions (
              night_episode_revision_id, namespace_id, data_mode,
              night_episode_id, subject_id, revision_number, revision_json,
              created_at, namespace_generation, timezone_name,
              episode_local_date, assignment_basis, date_confidence,
              assignment_estimated, date_state, date_conflict,
              episode_schema_version, run_id, arm_id
            ) VALUES (%s,%s,%s,%s,%s,1,%s::jsonb,%s,1,'Asia/Shanghai',%s,
              'observed_wake','observed',FALSE,'finalized',FALSE,'night_episode.v2',
              %s,%s)
            """,
            (
                episode_revision_id,
                namespace_id,
                data_mode,
                episode_id,
                subject_id,
                json.dumps(episode_payload),
                wake_at,
                wake_at.date(),
                run_id,
                arm_id,
            ),
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_finalizations (
              night_finalization_id, namespace_id, data_mode,
              namespace_generation, subject_id, night_episode_id, state,
              policy_version, policy_sha256, created_at, updated_at,
              run_id, arm_id
            ) VALUES (%s,%s,%s,1,%s,%s,'open','night-finalization.v1',
              %s,%s,%s,%s,%s)
            """,
            (
                finalization_id,
                namespace_id,
                data_mode,
                subject_id,
                episode_id,
                hashlib.sha256(b"night-finalization.v1").hexdigest(),
                wake_at,
                wake_at,
                run_id,
                arm_id,
            ),
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_finalization_revisions (
              night_finalization_revision_id, night_finalization_id,
              namespace_id, data_mode, namespace_generation, subject_id,
              night_episode_id, finalization_revision_number,
              parent_finalization_revision_id,
              source_night_episode_revision_id, state, provisional,
              coverage_status, revision_cause, material_sha256,
              finalization_json, created_at, run_id, arm_id
            ) VALUES (%s,%s,%s,%s,1,%s,%s,%s,%s,%s,'hard_finalized',FALSE,
              'complete',%s,%s,'{}'::jsonb,%s,%s,%s)
            """,
            (
                finalization_revision_id,
                finalization_id,
                namespace_id,
                data_mode,
                subject_id,
                episode_id,
                finalization_revision_number,
                parent_finalization_revision_id,
                episode_revision_id,
                (
                    "late_material_evidence"
                    if parent_finalization_revision_id
                    else "wake_grace_elapsed"
                ),
                hashlib.sha256(finalization_revision_id.encode()).hexdigest(),
                wake_at,
                run_id,
                arm_id,
            ),
        )
        cursor.execute(
            """
            UPDATE public.sleep_domain_night_finalizations
            SET state='hard_finalized',current_finalization_revision_id=%s,
                current_revision_number=%s,cas_version=cas_version+1,
                updated_at=%s
            WHERE night_finalization_id=%s
            """,
            (
                finalization_revision_id,
                finalization_revision_number,
                wake_at,
                finalization_id,
            ),
        )
    finally:
        cursor.close()
    return episode_id, finalization_revision_id


def _revise_night(
    connection: object,
    *,
    namespace_id: str,
    subject_id: str,
    episode_id: str,
    parent_finalization_revision_id: str,
    suffix: str,
    wake_at: datetime,
) -> str:
    episode_revision_id = f"g10-episode-revision-{suffix}"
    finalization_revision_id = f"g10-finalization-revision-{suffix}"
    bed_at = wake_at - timedelta(hours=8)
    episode_payload = {
        "episode": {
            "wake_at": wake_at.isoformat(),
            "bed_at": bed_at.isoformat(),
            "timezone_name": "Asia/Shanghai",
            "assignment_basis": "observed_wake",
        }
    }
    cursor = connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute(
            """
            SELECT night_finalization_id
            FROM public.sleep_domain_night_finalizations
            WHERE namespace_id=%s AND data_mode='live'
              AND subject_id=%s AND night_episode_id=%s
            """,
            (namespace_id, subject_id, episode_id),
        )
        finalization_id = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_episode_revisions (
              night_episode_revision_id, namespace_id, data_mode,
              night_episode_id, subject_id, revision_number,
              parent_revision_id, revision_json, created_at,
              namespace_generation, timezone_name, episode_local_date,
              assignment_basis, date_confidence, assignment_estimated,
              date_state, date_conflict, episode_schema_version
            ) VALUES (%s,%s,'live',%s,%s,2,
              (SELECT current_revision_id
               FROM public.sleep_domain_night_episodes
               WHERE night_episode_id=%s AND namespace_id=%s
                 AND data_mode='live'),
              %s::jsonb,%s,1,'Asia/Shanghai',%s,'observed_wake','observed',
              FALSE,'finalized',FALSE,'night_episode.v2')
            """,
            (
                episode_revision_id,
                namespace_id,
                episode_id,
                subject_id,
                episode_id,
                namespace_id,
                json.dumps(episode_payload),
                wake_at,
                wake_at.date(),
            ),
        )
        cursor.execute(
            """
            UPDATE public.sleep_domain_night_episodes
            SET current_revision_id=%s,current_revision_number=2,
                cas_version=cas_version+1,episode_json=%s::jsonb,
                episode_local_date=%s,bed_at=%s,wake_at=%s,updated_at=%s
            WHERE night_episode_id=%s AND namespace_id=%s AND data_mode='live'
            """,
            (
                episode_revision_id,
                json.dumps(episode_payload["episode"]),
                wake_at.date(),
                bed_at,
                wake_at,
                wake_at,
                episode_id,
                namespace_id,
            ),
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_finalization_revisions (
              night_finalization_revision_id, night_finalization_id,
              namespace_id, data_mode, namespace_generation, subject_id,
              night_episode_id, finalization_revision_number,
              parent_finalization_revision_id,
              source_night_episode_revision_id, state, provisional,
              coverage_status, revision_cause, material_sha256,
              finalization_json, created_at
            ) VALUES (%s,%s,%s,'live',1,%s,%s,2,%s,%s,'hard_finalized',FALSE,
              'complete','late_material_evidence',%s,'{}'::jsonb,%s)
            """,
            (
                finalization_revision_id,
                finalization_id,
                namespace_id,
                subject_id,
                episode_id,
                parent_finalization_revision_id,
                episode_revision_id,
                hashlib.sha256(finalization_revision_id.encode()).hexdigest(),
                wake_at,
            ),
        )
        cursor.execute(
            """
            UPDATE public.sleep_domain_night_finalizations
            SET current_finalization_revision_id=%s,current_revision_number=2,
                cas_version=cas_version+1,updated_at=%s
            WHERE night_finalization_id=%s
            """,
            (finalization_revision_id, wake_at, finalization_id),
        )
    finally:
        cursor.close()
    return finalization_revision_id


def test_g10_completed_execution_waits_then_evaluates_once_and_proposes_candidate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sleepagent.observability")
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _required("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    worker_dsn = _required("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    api_principal = _required("SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL")
    worker_principal = _required("SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL")
    suffix = uuid4().hex
    base = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(minutes=10)
    with psycopg.connect(admin_dsn) as admin:
        namespace, subject, actor, proposal = _seed_subject_and_proposal(
            admin, suffix=suffix, principal_id=api_principal, now=base
        )
        admin.execute(
            """
            INSERT INTO public.backend_principal_grants (
              grant_id, principal_id, namespace_id, data_mode, purpose,
              scopes_json, allowed_handlers_json, authorization_epoch,
              status, valid_from
            ) VALUES (%s,%s,%s,'live','worker','[]'::jsonb,%s::jsonb,1,
              'active',%s)
            """,
            (
                f"g10-worker-grant-{suffix}",
                worker_principal,
                namespace,
                json.dumps([CARE_OUTCOME_QUEUE]),
                base - timedelta(minutes=1),
            ),
        )
        # Two exact HARD_FINALIZED baseline nights with observed wake authority.
        _insert_night(
            admin,
            namespace_id=namespace,
            subject_id=subject,
            suffix=f"{suffix}-baseline-a",
            wake_at=base - timedelta(days=3, minutes=30),
        )
        _insert_night(
            admin,
            namespace_id=namespace,
            subject_id=subject,
            suffix=f"{suffix}-baseline-b",
            wake_at=base - timedelta(days=2) + timedelta(minutes=30),
        )
        admin.commit()

    product_context, principal = _contexts(
        namespace_id=namespace,
        subject_id=subject,
        actor_id=actor,
        binding_id=f"binding-{suffix}",
        principal_id=api_principal,
    )
    api_pool = PsycopgPoolProvider.from_dsn(
        api_dsn, configuration=PoolConfiguration(min_size=1, max_size=2)
    )
    api_pool.open()
    try:
        factory = UnitOfWorkFactory(api_pool)
        approved = PostgresProductBackend(
            factory, cursor_key=b"g" * 32,
            now_factory=lambda: base + timedelta(minutes=1),
        ).decide_care_proposal(
            product_context,
            proposal_id=proposal.proposal_id,
            expected_version=proposal.version,
            choice="approve",
            idempotency_key="g10-approve",
            reason_code="human_approved",
            reason=None,
        )
        service = CarePlanApplicationService(
            PostgresCarePlanRepository(factory),
            now_factory=lambda: base + timedelta(minutes=2),
        )
        plan_id = service.list(principal)[0].plan.care_plan_id
        service.start(principal, care_plan_id=plan_id, idempotency_key="g10-start")
        service.complete(
            principal,
            care_plan_id=plan_id,
            idempotency_key="g10-complete",
            occurred_at=base + timedelta(minutes=3),
        )
        assert approved.grant_id is not None
    finally:
        api_pool.close()

    worker_pool = PsycopgPoolProvider.from_dsn(
        worker_dsn, configuration=PoolConfiguration(min_size=1, max_size=2)
    )
    worker_pool.open()
    try:
        store = PostgresDurableWorkStore(
            _worker_settings(worker_dsn, worker_principal),
            UnitOfWorkFactory(worker_pool),
        )
        assert _run_one(store) == "waiting_for_followup"
        assert _run_one(store) is None
    finally:
        worker_pool.close()

    with psycopg.connect(admin_dsn) as admin:
        waiting = admin.execute(
            "SELECT state,current_care_outcome_id FROM "
            "public.backend_care_outcome_evaluations_v1 WHERE care_plan_id=%s",
            (plan_id,),
        ).fetchone()
        assert waiting == ("waiting_for_followup", None)
        waiting_terminal = _terminal_process(
            actor_id=actor,
            subject_id=subject,
            command=("outcome", plan_id),
        )
        assert waiting_terminal["lifecycle_state"] == "waiting_for_followup"
        assert waiting_terminal["outcome_category"] is None
        assert waiting_terminal["causal_claim"] is False
        _insert_night(
            admin,
            namespace_id=namespace,
            subject_id=subject,
            suffix=f"{suffix}-follow-a",
            wake_at=base + timedelta(days=1),
        )
        admin.commit()

    # A partial follow-up survives a fresh Worker pool and remains waiting.
    worker_pool = PsycopgPoolProvider.from_dsn(
        worker_dsn, configuration=PoolConfiguration(min_size=1, max_size=2)
    )
    worker_pool.open()
    try:
        store = PostgresDurableWorkStore(
            _worker_settings(worker_dsn, worker_principal),
            UnitOfWorkFactory(worker_pool),
        )
        assert _run_one(store) == "waiting_for_followup"
        assert _run_one(store) is None
    finally:
        worker_pool.close()

    with psycopg.connect(admin_dsn) as admin:
        follow_b_episode_id, follow_b_finalization_id = _insert_night(
            admin,
            namespace_id=namespace,
            subject_id=subject,
            suffix=f"{suffix}-follow-b",
            wake_at=base + timedelta(days=2, minutes=10),
        )
        admin.commit()

    ready_terminal = _terminal_process(
        actor_id=actor,
        subject_id=subject,
        command=("outcome", plan_id),
    )
    assert ready_terminal["lifecycle_state"] == "ready_for_evaluation"
    assert ready_terminal["outcome_category"] is None

    # Persist the business outcome but deliberately lose the Worker response
    # before durable operation finalization.
    worker_pool = PsycopgPoolProvider.from_dsn(
        worker_dsn, configuration=PoolConfiguration(min_size=1, max_size=2)
    )
    worker_pool.open()
    try:
        store = PostgresDurableWorkStore(
            _worker_settings(worker_dsn, worker_principal),
            UnitOfWorkFactory(worker_pool),
        )
        lost_claim = store.claim(
            queue=CARE_OUTCOME_QUEUE,
            worker_instance=f"g10-lost-response-{suffix}",
            lease_seconds=30,
        )
        assert lost_claim is not None
        lost_result = CareOutcomeWorkHandler()(
            WorkContext(lost_claim, store, threading.Event())
        )
        assert lost_result.disposition is WorkDisposition.SUCCEEDED
        assert lost_result.result["lifecycle_state"] == "evaluated"
        lost_operation_id = lost_claim.operation_id
    finally:
        worker_pool.close()

    with psycopg.connect(admin_dsn) as admin:
        assert admin.execute(
            "SELECT count(*) FROM public.backend_care_outcomes_v1 "
            "WHERE care_plan_id=%s",
            (plan_id,),
        ).fetchone() == (1,)
        admin.execute(
            "UPDATE public.sleep_domain_operations SET lease_expires_at = "
            "clock_timestamp() - interval '1 second' WHERE operation_id=%s",
            (lost_operation_id,),
        )
        admin.commit()

    # A new Worker reclaims the lost response and converges on the same
    # business authority before finalizing the durable operation.
    worker_pool = PsycopgPoolProvider.from_dsn(
        worker_dsn, configuration=PoolConfiguration(min_size=1, max_size=2)
    )
    worker_pool.open()
    try:
        store = PostgresDurableWorkStore(
            _worker_settings(worker_dsn, worker_principal),
            UnitOfWorkFactory(worker_pool),
        )
        assert _run_one(store) == "evaluated"
    finally:
        worker_pool.close()

    with psycopg.connect(admin_dsn) as admin:
        registration_id = admin.execute(
            "SELECT evaluation_registration_id FROM "
            "public.backend_care_outcome_evaluations_v1 WHERE care_plan_id=%s",
            (plan_id,),
        ).fetchone()[0]
        # Replaying the same finalization trigger is semantically idempotent.
        admin.execute(
            "SELECT public.sleepagent_queue_care_outcome_v1(%s,%s)",
            (registration_id, follow_b_finalization_id),
        )
        admin.execute(
            "SELECT public.sleepagent_queue_care_outcome_v1(%s,%s)",
            (registration_id, follow_b_finalization_id),
        )
        duplicate_count = admin.execute(
            """
            SELECT count(*) FROM public.sleep_domain_operations
            WHERE operation_type='care.outcome.evaluate.v1'
              AND operation_json ->> 'evaluation_registration_id'=%s
              AND operation_json ->> 'source_finalization_revision_id'=%s
            """,
            (registration_id, follow_b_finalization_id),
        ).fetchone()[0]
        assert duplicate_count == 1
        first_outcome_id, first_receipt_id = admin.execute(
            """
            SELECT outcome.care_outcome_id, receipt.receipt_id
            FROM public.backend_care_outcomes_v1 AS outcome
            JOIN public.backend_personalization_effect_receipts_v1 AS receipt
              ON receipt.care_outcome_id=outcome.care_outcome_id
            WHERE outcome.care_plan_id=%s AND outcome.evaluation_revision=1
            """,
            (plan_id,),
        ).fetchone()
        late_finalization_id = _revise_night(
            admin,
            namespace_id=namespace,
            subject_id=subject,
            episode_id=follow_b_episode_id,
            parent_finalization_revision_id=follow_b_finalization_id,
            suffix=f"{suffix}-follow-b-late",
            wake_at=base + timedelta(days=2, minutes=90),
        )
        admin.commit()

    # Late authoritative evidence races only with an idempotent retry and
    # creates exactly one correct superseding outcome/receipt chain.
    worker_pool = PsycopgPoolProvider.from_dsn(
        worker_dsn, configuration=PoolConfiguration(min_size=1, max_size=2)
    )
    worker_pool.open()
    try:
        store = PostgresDurableWorkStore(
            _worker_settings(worker_dsn, worker_principal),
            UnitOfWorkFactory(worker_pool),
        )
        assert _run_one(store) == "evaluated"
    finally:
        worker_pool.close()

    with psycopg.connect(admin_dsn) as admin:
        outcome_rows = admin.execute(
            "SELECT care_outcome_id,outcome_category,causal_claim,"
            "evaluation_revision,supersedes_care_outcome_id "
            "FROM public.backend_care_outcomes_v1 WHERE care_plan_id=%s "
            "ORDER BY evaluation_revision",
            (plan_id,),
        ).fetchall()
        receipt_rows = admin.execute(
            "SELECT receipt_id,supersedes_receipt_id,state,direct_memory_write,"
            "candidate_type "
            "FROM public.backend_personalization_effect_receipts_v1 "
            "WHERE evaluation_registration_id IN (SELECT "
            "evaluation_registration_id FROM "
            "public.backend_care_outcome_evaluations_v1 WHERE care_plan_id=%s) "
            "ORDER BY created_at,receipt_id",
            (plan_id,),
        ).fetchall()
        current = admin.execute(
            "SELECT current_care_outcome_id,current_evaluation_revision "
            "FROM public.backend_care_outcome_evaluations_v1 "
            "WHERE care_plan_id=%s",
            (plan_id,),
        ).fetchone()
        assert admin.execute(
            """
            SELECT count(*) FROM public.sleep_domain_operations
            WHERE operation_type='care.outcome.evaluate.v1'
              AND operation_json ->> 'source_finalization_revision_id'=%s
            """,
            (late_finalization_id,),
        ).fetchone() == (1,)
        direct_habit = admin.execute(
            "SELECT count(*) FROM public.backend_habit_profile_revisions_v2 "
            "WHERE subject_id=%s",
            (subject,),
        ).fetchone()[0]
        direct_memory = admin.execute(
            "SELECT count(*) FROM public.backend_governed_memory_revisions_v2 "
            "WHERE subject_id=%s",
            (subject,),
        ).fetchone()[0]
        external_effects = admin.execute(
            """
            SELECT
              (SELECT count(*) FROM public.backend_delivery_intents
               WHERE namespace_id=%s),
              (SELECT count(*) FROM public.backend_delivery_journal
               WHERE namespace_id=%s)
            """,
            (namespace, namespace),
        ).fetchone()
        rls = admin.execute(
            """
            SELECT relname,relrowsecurity,relforcerowsecurity
            FROM pg_catalog.pg_class WHERE relname IN (
              'backend_care_outcome_evaluations_v1',
              'backend_care_outcomes_v1',
              'backend_personalization_effect_receipts_v1'
            ) ORDER BY relname
            """
        ).fetchall()
        public_privileges = admin.execute(
            """
            SELECT count(*) FROM information_schema.table_privileges
            WHERE table_schema='public' AND grantee='PUBLIC'
              AND table_name IN (
                'backend_care_outcome_evaluations_v1',
                'backend_care_outcomes_v1',
                'backend_personalization_effect_receipts_v1'
              )
            """
        ).fetchone()[0]
        admin.execute("SAVEPOINT g10_append_only")
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            admin.execute(
                "UPDATE public.backend_personalization_effect_receipts_v1 "
                "SET state='superseded' WHERE receipt_id=%s",
                (first_receipt_id,),
            )
        admin.execute("ROLLBACK TO SAVEPOINT g10_append_only")
        wrong_subject = f"subject-wrong-{suffix}"
        admin.execute(
            "INSERT INTO public.backend_subjects "
            "(namespace_id,data_mode,subject_id,timezone_name,status) "
            "VALUES (%s,'live',%s,'Asia/Shanghai','active')",
            (namespace, wrong_subject),
        )
        admin.execute(
            "INSERT INTO public.backend_subject_epochs "
            "(namespace_id,data_mode,subject_id,authorization_epoch,"
            "privacy_epoch,retrieval_policy_epoch) "
            "VALUES (%s,'live',%s,1,1,1)",
            (namespace, wrong_subject),
        )
        admin.commit()
    assert len(outcome_rows) == 2
    assert outcome_rows[0][0] == first_outcome_id
    assert outcome_rows[0][1] == "improved"
    assert outcome_rows[0][2] is False and outcome_rows[0][3] == 1
    assert outcome_rows[1][1] == "worsened"
    assert outcome_rows[1][2] is False and outcome_rows[1][3] == 2
    assert outcome_rows[1][4] == first_outcome_id
    assert current == (outcome_rows[1][0], 2)
    assert len(receipt_rows) == 2
    assert receipt_rows[0] == (
        first_receipt_id, None, "candidate_proposed", False, "governed_memory"
    )
    assert receipt_rows[1][1:] == (
        first_receipt_id, "candidate_proposed", False, "governed_memory"
    )
    assert direct_habit == 0
    assert direct_memory == 0
    assert external_effects == (0, 0)
    assert len(rls) == 3 and all(row[1:] == (True, True) for row in rls)
    assert public_privileges == 0
    terminal_outcome = _terminal_process(
        actor_id=actor,
        subject_id=subject,
        command=("outcome", plan_id, "--trace"),
    )
    assert terminal_outcome["outcome_category"] == "worsened"
    assert terminal_outcome["causal_claim"] is False
    assert len(terminal_outcome["trace"]["outcome_hash"]) == 64
    assert terminal_outcome["trace"]["followup_revision_ids"][-1] == (
        late_finalization_id
    )
    assert terminal_outcome["trace"]["personalization_receipt_id"] == (
        receipt_rows[1][0]
    )
    api_pool = PsycopgPoolProvider.from_dsn(
        api_dsn, configuration=PoolConfiguration(min_size=1, max_size=2)
    )
    api_pool.open()
    try:
        factory = UnitOfWorkFactory(api_pool)
        scoped_counts: list[int] = []
        for scoped_subject in (subject, wrong_subject):
            with factory.begin(
                UowScope(
                    namespace_id=namespace,
                    data_mode="live",
                    process_role="api",
                    purpose="sleep_care",
                    service_principal_id=api_principal,
                    namespace_generation=1,
                    subject_id=scoped_subject,
                    actor_id=actor,
                    actor_role="elder",
                    authorization_epoch=1,
                    privacy_epoch=1,
                    retrieval_policy_epoch=1,
                )
            ) as uow:
                scoped_counts.append(
                    uow.connection.execute(
                        "SELECT count(*) FROM public.backend_care_outcomes_v1 "
                        "WHERE care_plan_id=%s",
                        (plan_id,),
                    ).fetchone()[0]
                )
                uow.commit()
        assert scoped_counts == [2, 0]
        metrics = PostgresInternalStatus(
            type(
                "Settings",
                (),
                {
                    "data_mode": type("Mode", (), {"value": "live"})(),
                    "service_principal_id": api_principal,
                },
            )(),
            factory,
        ).operational_metrics()["care_outcome"]
        expected_metrics = {
            "pending_outcome_evaluations",
            "oldest_waiting_evaluation_age_seconds",
            "ready_evaluations",
            "evaluated_outcomes",
            "insufficient_data_count",
            "not_comparable_count",
            "superseded_outcome_count",
            "personalization_candidates_proposed",
            "personalization_candidates_accepted",
            "personalization_candidates_rejected",
        }
        assert expected_metrics == set(metrics)
        assert all(metrics[key] >= 0 for key in expected_metrics)
        assert "subject" not in json.dumps(metrics, sort_keys=True)
    finally:
        api_pool.close()
    with psycopg.connect(worker_dsn) as worker:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            worker.execute(
                "INSERT INTO public.backend_governed_memory_revisions_v2 "
                "DEFAULT VALUES"
            )
        worker.rollback()
    structured_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert structured_logs.count(
        '"event": "personalization_effect_receipt_created"'
    ) == 2
    assert structured_logs.count(
        '"event": "personalization_candidate_proposed"'
    ) == 2
    assert structured_logs.count('"event": "care_outcome_ready"') == 1
    assert structured_logs.count('"event": "care_outcome_superseded"') == 1
    g10_logs = "\n".join(
        line for line in structured_logs.splitlines()
        if '"event": "care_outcome_' in line
        or '"event": "personalization_' in line
    )
    assert subject not in g10_logs
    assert "wake_at" not in g10_logs


def test_g10_upgrade_021_backfills_completed_execution_and_queues_evaluation() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    database_name, upgrade_dsn = _temporary_database(
        admin_dsn, "sleepagent_g10_upgrade"
    )
    try:
        suffix = uuid4().hex
        api_principal = f"g10-upgrade-api-{suffix}"
        worker_principal = f"g10-upgrade-worker-{suffix}"
        base = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(minutes=10)
        with psycopg.connect(upgrade_dsn) as admin:
            _apply_prefix(admin, 21)
            admin.execute(
                """
                INSERT INTO public.backend_service_principals (
                  principal_id, database_role_name, principal_kind, status,
                  credential_generation, metadata_json
                ) VALUES
                  (%s,'sleepagent_test_migration','bff','active',1,
                    '{"g10_upgrade":true}'::jsonb),
                  (%s,'sleepagent_test_worker','worker','active',1,
                    '{"g10_upgrade":true}'::jsonb)
                """,
                (api_principal, worker_principal),
            )
            namespace, subject, actor, proposal = _seed_subject_and_proposal(
                admin, suffix=suffix, principal_id=api_principal, now=base
            )
            admin.execute(
                """
                INSERT INTO public.backend_principal_grants (
                  grant_id, principal_id, namespace_id, data_mode, purpose,
                  scopes_json, allowed_handlers_json, authorization_epoch,
                  status, valid_from
                ) VALUES (%s,%s,%s,'live','worker','[]'::jsonb,
                  '["night.finalization_scan"]'::jsonb,1,'active',%s)
                """,
                (
                    f"g10-upgrade-worker-grant-{suffix}",
                    worker_principal,
                    namespace,
                    base - timedelta(minutes=1),
                ),
            )
            admin.commit()

        product_context, principal = _contexts(
            namespace_id=namespace,
            subject_id=subject,
            actor_id=actor,
            binding_id=f"binding-{suffix}",
            principal_id=api_principal,
        )
        provider = PsycopgPoolProvider.from_dsn(
            upgrade_dsn,
            configuration=PoolConfiguration(min_size=1, max_size=2),
        )
        provider.open()
        try:
            factory = UnitOfWorkFactory(provider)
            PostgresProductBackend(
                factory,
                cursor_key=b"u" * 32,
                now_factory=lambda: base + timedelta(minutes=1),
            ).decide_care_proposal(
                product_context,
                proposal_id=proposal.proposal_id,
                expected_version=proposal.version,
                choice="approve",
                idempotency_key="g10-upgrade-approve",
                reason_code="human_approved",
                reason=None,
            )
            service = CarePlanApplicationService(
                PostgresCarePlanRepository(factory),
                now_factory=lambda: base + timedelta(minutes=2),
            )
            plan_id = service.list(principal)[0].plan.care_plan_id
            service.start(
                principal,
                care_plan_id=plan_id,
                idempotency_key="g10-upgrade-start",
            )
            service.complete(
                principal,
                care_plan_id=plan_id,
                idempotency_key="g10-upgrade-complete",
                occurred_at=base + timedelta(minutes=3),
            )
        finally:
            provider.close()

        with psycopg.connect(upgrade_dsn) as admin:
            assert admin.execute(
                "SELECT to_regclass('public.backend_care_outcomes_v1')"
            ).fetchone() == (None,)
            runner = PostgresMigrationRunner(
                admin, applied_by="g10-upgrade-proof"
            )
            assert runner.apply() == LATEST_SCHEMA_VERSION
            registration = admin.execute(
                """
                SELECT state, execution_authority, current_evaluation_revision
                FROM public.backend_care_outcome_evaluations_v1
                WHERE care_plan_id=%s
                """,
                (plan_id,),
            ).fetchone()
            operations = admin.execute(
                """
                SELECT operation_json ->> 'source_finalization_revision_id',
                  status, available_at
                FROM public.sleep_domain_operations
                WHERE operation_type='care.outcome.evaluate.v1'
                  AND target_resource_id IN (
                    SELECT evaluation_registration_id
                    FROM public.backend_care_outcome_evaluations_v1
                    WHERE care_plan_id=%s
                  )
                ORDER BY available_at
                """,
                (plan_id,),
            ).fetchall()
            worker_handlers = admin.execute(
                """
                SELECT allowed_handlers_json
                FROM public.backend_principal_grants
                WHERE principal_id=%s AND purpose='worker'
                """,
                (worker_principal,),
            ).fetchone()[0]
        assert registration == ("waiting_for_followup", "human_attested", 0)
        assert len(operations) == 2
        assert operations[0][0] is None and operations[0][1] == "pending"
        assert operations[1][0] == "window_expiry"
        assert operations[1][1] == "pending"
        assert operations[1][2] > operations[0][2]
        assert CARE_OUTCOME_QUEUE in worker_handlers
    finally:
        _drop_database(admin_dsn, database_name)
