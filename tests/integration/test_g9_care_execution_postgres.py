from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sleepagent.api.postgres import PostgresProductBackend
from sleepagent.api.product import ProductRequestContext
from sleepagent.api.product_contracts import ProductRole
from sleepagent.app import PostgresInternalStatus
from sleepagent.application.care_actions import persist_care_action_proposal
from sleepagent.application.care_execution import (
    CARE_READ_SCOPE,
    CareExecutionPrincipal,
    CarePlanApplicationService,
    CarePlanFilter,
)
from sleepagent.domain.care_actions import (
    CareActionCandidateV2,
    CareActionProposal,
    CareActionType,
    CareAudience,
    DeterministicCareActionPolicy,
)
from sleepagent.domain.care_execution import (
    CARE_EXECUTION_SCOPE,
    CareExecutionState,
)
from sleepagent.infrastructure.postgres_care_execution import (
    PostgresCarePlanRepository,
)
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
    UowScope,
)


pytestmark = pytest.mark.postgres
UTC = timezone.utc


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} is required for PostgreSQL integration tests")
    return value


def _seed_subject_and_proposal(
    admin: object,
    *,
    suffix: str,
    principal_id: str,
    now: datetime,
    action_type: CareActionType = CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME,
) -> tuple[str, str, str, CareActionProposal]:
    namespace_id = f"live:g9-{suffix}"
    subject_id = f"subject-{suffix}"
    actor_id = f"actor-{suffix}"
    binding_id = f"binding-{suffix}"
    episode_id = f"night-{suffix}"
    episode_revision_id = f"night-revision-{suffix}"
    finalization_id = f"finalization-{suffix}"
    finalization_revision_id = f"finalization-revision-{suffix}"
    analysis_revision_id = f"analysis-{suffix}"
    cursor = admin.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute(
            "INSERT INTO public.backend_namespaces "
            "(namespace_id,data_mode,current_generation,status,synthetic_non_release) "
            "VALUES (%s,'live',1,'active',FALSE)",
            (namespace_id,),
        )
        cursor.execute(
            "INSERT INTO public.backend_namespace_generations "
            "(namespace_id,data_mode,generation,status,configuration_sha256) "
            "VALUES (%s,'live',1,'active',%s)",
            (namespace_id, hashlib.sha256(namespace_id.encode()).hexdigest()),
        )
        cursor.execute(
            "INSERT INTO public.backend_actors (actor_id,actor_kind,status) "
            "VALUES (%s,'human','active')",
            (actor_id,),
        )
        cursor.execute(
            "INSERT INTO public.backend_subjects "
            "(namespace_id,data_mode,subject_id,timezone_name,status) "
            "VALUES (%s,'live',%s,'Asia/Shanghai','active')",
            (namespace_id, subject_id),
        )
        cursor.execute(
            "INSERT INTO public.backend_subject_epochs "
            "(namespace_id,data_mode,subject_id,authorization_epoch,"
            "privacy_epoch,retrieval_policy_epoch) VALUES (%s,'live',%s,1,1,1)",
            (namespace_id, subject_id),
        )
        scopes = [
            CARE_READ_SCOPE,
            "product:sleep:care:confirm",
            CARE_EXECUTION_SCOPE,
        ]
        executor_role = (
            CareAudience.FAMILY
            if action_type in {
                CareActionType.REQUEST_MANUAL_FOLLOW_UP,
                CareActionType.REQUEST_MORNING_REVIEW_FEEDBACK,
            }
            else CareAudience.ELDER
        )
        cursor.execute(
            """
            INSERT INTO public.backend_actor_subject_bindings (
              binding_id, namespace_id, data_mode, actor_id, subject_id, role,
              status, purpose_json, scopes_json, authorization_epoch, valid_from
            ) VALUES (%s,%s,'live',%s,%s,%s,'active',
              '["sleep_care"]'::jsonb,%s::jsonb,1,%s)
            """,
            (
                binding_id,
                namespace_id,
                actor_id,
                subject_id,
                executor_role.value,
                json.dumps(scopes),
                now - timedelta(minutes=1),
            ),
        )
        cursor.execute(
            """
            INSERT INTO public.backend_principal_grants (
              grant_id, principal_id, namespace_id, data_mode, purpose,
              scopes_json, authorization_epoch, status, valid_from
            ) VALUES (%s,%s,%s,'live','sleep_care',%s::jsonb,1,'active',%s)
            """,
            (
                f"principal-grant-{suffix}",
                principal_id,
                namespace_id,
                json.dumps(scopes),
                now - timedelta(minutes=1),
            ),
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_episodes (
              night_episode_id, namespace_id, data_mode, subject_id, night_key,
              state, current_revision_id, current_revision_number, cas_version,
              episode_json, created_at, updated_at
            ) VALUES (%s,%s,'live',%s,'2026-08-31','closed',%s,1,1,
              '{}'::jsonb,%s,%s)
            """,
            (episode_id, namespace_id, subject_id, episode_revision_id, now, now),
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_episode_revisions (
              night_episode_revision_id, namespace_id, data_mode,
              night_episode_id, subject_id, revision_number, revision_json,
              created_at
            ) VALUES (%s,%s,'live',%s,%s,1,'{}'::jsonb,%s)
            """,
            (episode_revision_id, namespace_id, episode_id, subject_id, now),
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_finalizations (
              night_finalization_id, namespace_id, data_mode,
              namespace_generation, subject_id, night_episode_id, state,
              policy_version, policy_sha256, created_at, updated_at
            ) VALUES (%s,%s,'live',1,%s,%s,'open','night-finalization.v1',
              %s,%s,%s)
            """,
            (
                finalization_id,
                namespace_id,
                subject_id,
                episode_id,
                hashlib.sha256(b"night-finalization.v1").hexdigest(),
                now,
                now,
            ),
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_finalization_revisions (
              night_finalization_revision_id, night_finalization_id,
              namespace_id, data_mode, namespace_generation, subject_id,
              night_episode_id, finalization_revision_number,
              source_night_episode_revision_id, state, provisional,
              coverage_status, revision_cause, material_sha256,
              finalization_json, created_at
            ) VALUES (%s,%s,%s,'live',1,%s,%s,1,%s,'hard_finalized',FALSE,
              'complete','wake_grace_elapsed',%s,'{}'::jsonb,%s)
            """,
            (
                finalization_revision_id,
                finalization_id,
                namespace_id,
                subject_id,
                episode_id,
                episode_revision_id,
                hashlib.sha256(suffix.encode()).hexdigest(),
                now,
            ),
        )
        cursor.execute(
            """
            UPDATE public.sleep_domain_night_finalizations
            SET state='hard_finalized',current_finalization_revision_id=%s,
                current_revision_number=1,cas_version=1,updated_at=%s
            WHERE night_finalization_id=%s
            """,
            (finalization_revision_id, now, finalization_id),
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_analysis_revisions (
              analysis_revision_id, namespace_id, data_mode, night_episode_id,
              night_episode_revision_id, subject_id, revision_number,
              analysis_json, created_at
            ) VALUES (%s,%s,'live',%s,%s,%s,1,'{}'::jsonb,%s)
            """,
            (
                analysis_revision_id,
                namespace_id,
                episode_id,
                episode_revision_id,
                subject_id,
                now,
            ),
        )
        catalog_id, intent, parameters = {
            CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME: (
                "consistent-wake-time", "routine_adjustment",
                {"tolerance_minutes": 30},
            ),
            CareActionType.RECOMMEND_MORNING_LIGHT: (
                "morning-light", "environment_adjustment", {"minutes": 20},
            ),
            CareActionType.REQUEST_MANUAL_FOLLOW_UP: (
                "nighttime-gentle-support", "manual_support", {},
            ),
            CareActionType.REQUEST_MORNING_REVIEW_FEEDBACK: (
                "morning-review-feedback", "review_feedback", {},
            ),
        }[action_type]
        candidate = CareActionCandidateV2.create(
            candidate_id=f"candidate-{suffix}",
            subject_id=subject_id,
            source_analysis_revision_id=analysis_revision_id,
            source_shared_analysis_sha256=hashlib.sha256(
                f"shared-{suffix}".encode()
            ).hexdigest(),
            source_night_finalization_revision_id=finalization_revision_id,
            source_care_strategy_invocation_id=f"invocation-{suffix}",
            source_care_strategy_version="care-strategy.v1",
            source_care_work_product_ref=f"care-work-{suffix}",
            action_type=action_type,
            catalog_action_id=catalog_id,
            catalog_action_version=1,
            intent=intent,
            rationale_evidence_refs=(f"claim-{suffix}",),
            urgency="normal",
            audience=executor_role,
            parameters=parameters,
            created_at=now,
            display_explanation="Display only.",
        )
        decision = DeterministicCareActionPolicy().evaluate(
            candidate,
            source_is_current=True,
            source_is_hard_finalized=True,
            evidence_is_sufficient=True,
        )
        proposal = CareActionProposal.create(candidate, decision)
        persist_care_action_proposal(
            cursor,
            UowScope(
                namespace_id=namespace_id,
                data_mode="live",
                process_role="worker",
                purpose="sleep_analysis",
                service_principal_id="integration-worker",
                namespace_generation=1,
                subject_id=subject_id,
                authorization_epoch=1,
                privacy_epoch=1,
                retrieval_policy_epoch=1,
                worker_instance="g9-proof",
            ),
            night_episode_id=episode_id,
            proposal=proposal,
            persisted_at=now,
        )
    finally:
        cursor.close()
    return namespace_id, subject_id, actor_id, proposal


def _contexts(
    *,
    namespace_id: str,
    subject_id: str,
    actor_id: str,
    binding_id: str,
    principal_id: str,
    role: CareAudience = CareAudience.ELDER,
) -> tuple[ProductRequestContext, CareExecutionPrincipal]:
    scopes = frozenset(
        {CARE_READ_SCOPE, "product:sleep:care:confirm", CARE_EXECUTION_SCOPE}
    )
    product = ProductRequestContext(
        service_principal_id=principal_id,
        actor_id=actor_id,
        binding_id=binding_id,
        subject_id=subject_id,
        role=ProductRole(role.value),
        effective_scopes=scopes,
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
    care = CareExecutionPrincipal(
        service_principal_id=principal_id,
        actor_id=actor_id,
        actor_role=role,
        actor_binding_id=binding_id,
        subject_id=subject_id,
        effective_scopes=scopes,
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        namespace_id=namespace_id,
        namespace_generation=1,
        data_mode="live",
        run_id=None,
        arm_id=None,
    )
    return product, care


def _persist_material_superseding_proposal(
    admin: object,
    *,
    namespace_id: str,
    subject_id: str,
    suffix: str,
    previous: CareActionProposal,
    now: datetime,
) -> CareActionProposal:
    """Persist Analysis N+1 through the real G8 supersession boundary."""

    analysis_revision_id = f"analysis-new-{suffix}"
    cursor = admin.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_analysis_revisions (
              analysis_revision_id, namespace_id, data_mode, night_episode_id,
              night_episode_revision_id, subject_id, revision_number,
              parent_analysis_revision_id, analysis_json, created_at
            ) VALUES (%s,%s,'live',%s,%s,%s,2,%s,'{}'::jsonb,%s)
            """,
            (
                analysis_revision_id,
                namespace_id,
                f"night-{suffix}",
                f"night-revision-{suffix}",
                subject_id,
                previous.candidate.source_analysis_revision_id,
                now,
            ),
        )
        old = previous.candidate
        candidate = CareActionCandidateV2.create(
            candidate_id=f"candidate-new-{suffix}",
            subject_id=subject_id,
            source_analysis_revision_id=analysis_revision_id,
            source_shared_analysis_sha256=hashlib.sha256(
                f"shared-new-{suffix}".encode()
            ).hexdigest(),
            source_night_finalization_revision_id=(
                old.source_night_finalization_revision_id
            ),
            source_care_strategy_invocation_id=f"invocation-new-{suffix}",
            source_care_strategy_version=old.source_care_strategy_version,
            source_care_work_product_ref=f"care-work-new-{suffix}",
            action_type=old.action_type,
            catalog_action_id=old.catalog_action_id,
            catalog_action_version=old.catalog_action_version,
            intent=old.intent,
            rationale_evidence_refs=(f"claim-new-{suffix}",),
            urgency=old.urgency,
            audience=old.audience,
            parameters=dict(old.parameters),
            created_at=now,
            display_explanation="Updated evidence, same bounded action.",
        )
        decision = DeterministicCareActionPolicy().evaluate(
            candidate,
            source_is_current=True,
            source_is_hard_finalized=True,
            evidence_is_sufficient=True,
        )
        replacement = CareActionProposal.create(candidate, decision)
        persist_care_action_proposal(
            cursor,
            UowScope(
                namespace_id=namespace_id,
                data_mode="live",
                process_role="worker",
                purpose="sleep_analysis",
                service_principal_id="integration-worker",
                namespace_generation=1,
                subject_id=subject_id,
                authorization_epoch=1,
                privacy_epoch=1,
                retrieval_policy_epoch=1,
                worker_instance="g9-supersession-proof",
            ),
            night_episode_id=f"night-{suffix}",
            proposal=replacement,
            persisted_at=now,
        )
        return replacement
    finally:
        cursor.close()


def _terminal_process(
    *,
    actor_id: str,
    subject_id: str,
    command: tuple[str, ...],
) -> dict[str, object]:
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "scripts.g9_terminal_care_process_probe",
            "--actor-id",
            actor_id,
            "--subject-id",
            subject_id,
            "--role",
            "elder",
            "--authorization-epoch",
            "1",
            "--privacy-epoch",
            "1",
            "--retrieval-policy-epoch",
            "1",
            *command,
            "--json",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _terminal_process_drop_response(
    *,
    actor_id: str,
    subject_id: str,
    command: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["SLEEPAGENT_G9_PROBE_DROP_RESPONSE"] = "1"
    return subprocess.run(
        (
            sys.executable,
            "-m",
            "scripts.g9_terminal_care_process_probe",
            "--actor-id",
            actor_id,
            "--subject-id",
            subject_id,
            "--role",
            "elder",
            "--authorization-epoch",
            "1",
            "--privacy-epoch",
            "1",
            "--retrieval-policy-epoch",
            "1",
            *command,
            "--json",
        ),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_g9_grant_plan_execution_restart_concurrency_and_isolation() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _required("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    principal_id = _required("SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL")
    suffix = uuid4().hex
    now = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(minutes=10)
    binding_id = f"binding-{suffix}"
    with psycopg.connect(admin_dsn) as admin:
        namespace_id, subject_id, actor_id, proposal = _seed_subject_and_proposal(
            admin, suffix=suffix, principal_id=principal_id, now=now
        )
        admin.commit()

    product_context, principal = _contexts(
        namespace_id=namespace_id,
        subject_id=subject_id,
        actor_id=actor_id,
        binding_id=binding_id,
        principal_id=principal_id,
    )
    provider = PsycopgPoolProvider.from_dsn(
        api_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=6),
        application_name="sleepagent-g9-postgres-proof",
    )
    provider.open()
    try:
        factory = UnitOfWorkFactory(provider)
        backend = PostgresProductBackend(
            factory,
            cursor_key=b"p" * 32,
            now_factory=lambda: now + timedelta(minutes=1),
        )
        approved = backend.decide_care_proposal(
            product_context,
            proposal_id=proposal.proposal_id,
            expected_version=proposal.version,
            choice="approve",
            idempotency_key="approve-g9",
            reason_code="human_approved",
            reason=None,
        )
        assert approved.outcome == "applied"
        assert approved.grant_id is not None
        service = CarePlanApplicationService(
            PostgresCarePlanRepository(factory),
            now_factory=lambda: now + timedelta(minutes=2),
        )
        plans = service.list(principal)
        assert len(plans) == 1
        plan_id = plans[0].plan.care_plan_id
        assert plans[0].execution.state is CareExecutionState.NOT_STARTED
        assert plans[0].plan.approval_grant_id == approved.grant_id
        product_care = backend.get_care(product_context, limit=20, cursor=None)
        product_plan = next(
            item for item in product_care.items
            if item.record_type == "care_plan"
        )
        assert product_plan.care_plan_id == plan_id
        assert product_plan.state == "not_started"
        assert product_plan.completion_semantics == (
            "human_attested_execution_only"
        )

        listed = _terminal_process(
            actor_id=actor_id,
            subject_id=subject_id,
            command=("list",),
        )
        assert [item["care_plan_id"] for item in listed["items"]] == [plan_id]
        started = _terminal_process(
            actor_id=actor_id,
            subject_id=subject_id,
            command=("start", plan_id, "--idempotency-key", "start-once"),
        )
        retry = _terminal_process(
            actor_id=actor_id,
            subject_id=subject_id,
            command=("start", plan_id, "--idempotency-key", "start-once"),
        )
        assert started["outcome"] == "applied"
        assert retry["outcome"] == "idempotent"
        assert len(service.history(principal, care_plan_id=plan_id)) == 1
    finally:
        provider.close()

    # A fresh pool/process view recovers the durable state without repair.
    provider = PsycopgPoolProvider.from_dsn(
        api_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=3),
        application_name="sleepagent-g9-postgres-proof-restarted",
    )
    provider.open()
    try:
        factory = UnitOfWorkFactory(provider)
        service = CarePlanApplicationService(
            PostgresCarePlanRepository(factory),
            now_factory=lambda: now + timedelta(minutes=3),
        )
        restarted = _terminal_process(
            actor_id=actor_id,
            subject_id=subject_id,
            command=("show", plan_id),
        )
        assert restarted["state"] == "in_progress"
        interrupted = _terminal_process_drop_response(
            actor_id=actor_id,
            subject_id=subject_id,
            command=(
                "complete",
                plan_id,
                "--idempotency-key",
                "complete-once",
            ),
        )
        assert interrupted.returncode == 75
        assert interrupted.stdout == ""
        completed = _terminal_process(
            actor_id=actor_id,
            subject_id=subject_id,
            command=(
                "complete",
                plan_id,
                "--idempotency-key",
                "complete-once",
            ),
        )
        assert completed["outcome"] == "idempotent"
        history = _terminal_process(
            actor_id=actor_id,
            subject_id=subject_id,
            command=("history", plan_id),
        )
        assert [item["event_type"] for item in history["events"]] == [
            "started",
            "completed",
        ]
        completed_product = PostgresProductBackend(
            factory,
            cursor_key=b"q" * 32,
            now_factory=lambda: now + timedelta(minutes=4),
        ).get_care(product_context, limit=20, cursor=None)
        completed_record = next(
            item
            for item in completed_product.items
            if item.record_type == "care_plan"
        )
        assert completed_record.state == "completed"
        assert completed_record.completion_semantics == (
            "human_attested_execution_only"
        )
        with psycopg.connect(admin_dsn) as admin:
            ensured = admin.execute(
                "SELECT public.sleepagent_ensure_care_plan_v1(%s,%s)",
                (approved.grant_id, now + timedelta(minutes=4)),
            ).fetchone()[0]
            counts = admin.execute(
                "SELECT count(*) FROM public.backend_care_plans_v1 "
                "WHERE approval_grant_id=%s",
                (approved.grant_id,),
            ).fetchone()[0]
        assert ensured == plan_id
        assert counts == 1
        with psycopg.connect(api_dsn) as api:
            with pytest.raises(psycopg.Error):
                api.execute(
                    "UPDATE public.backend_care_execution_states_v1 "
                    "SET state='completed' WHERE care_plan_id=%s",
                    (plan_id,),
                )
            api.rollback()
        with psycopg.connect(admin_dsn) as admin:
            rls = admin.execute(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_catalog.pg_class
                WHERE relname IN (
                  'backend_care_plans_v1',
                  'backend_care_execution_states_v1',
                  'backend_care_execution_events_v1'
                ) ORDER BY relname
                """
            ).fetchall()
            public_execute = admin.execute(
                "SELECT has_function_privilege('public', "
                "'public.sleepagent_execute_care_plan_v1(text,bigint,text,text,"
                "text,text,timestamptz)', 'EXECUTE')"
            ).fetchone()[0]
            outcome_table = admin.execute(
                "SELECT to_regclass('public.backend_care_outcome')"
            ).fetchone()[0]
        assert len(rls) == 3
        assert all(row[1:] == (True, True) for row in rls)
        assert public_execute is False
        assert outcome_table is None
        with psycopg.connect(admin_dsn) as admin:
            with pytest.raises(psycopg.Error):
                admin.execute(
                    "UPDATE public.backend_care_plans_v1 "
                    "SET plan_version=1 WHERE care_plan_id=%s",
                    (plan_id,),
                )
            admin.rollback()
        with psycopg.connect(admin_dsn) as admin:
            with pytest.raises(psycopg.Error):
                admin.execute(
                    "UPDATE public.backend_care_execution_events_v1 "
                    "SET note=note WHERE care_plan_id=%s",
                    (plan_id,),
                )
            admin.rollback()
    finally:
        provider.close()


def test_g9_postgres_fail_closed_authority_and_transition_matrix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _required("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    principal_id = _required("SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL")
    root = uuid4().hex
    base = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(minutes=10)
    provider = PsycopgPoolProvider.from_dsn(
        api_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=8),
        application_name="sleepagent-g9-fail-closed-matrix",
    )
    provider.open()
    try:
        caplog.set_level(logging.INFO, logger="sleepagent.observability")
        factory = UnitOfWorkFactory(provider)

        def seed_and_context(
            label: str,
            *,
            at: datetime = base,
            action_type: CareActionType = (
                CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME
            ),
        ) -> tuple[ProductRequestContext, CareExecutionPrincipal, CareActionProposal]:
            suffix = f"{root}{label}"
            with psycopg.connect(admin_dsn) as admin:
                namespace, subject, actor, proposal = _seed_subject_and_proposal(
                    admin,
                    suffix=suffix,
                    principal_id=principal_id,
                    now=at,
                    action_type=action_type,
                )
                admin.commit()
            product, principal = _contexts(
                namespace_id=namespace,
                subject_id=subject,
                actor_id=actor,
                binding_id=f"binding-{suffix}",
                principal_id=principal_id,
                role=(
                    CareAudience.FAMILY
                    if action_type in {
                        CareActionType.REQUEST_MANUAL_FOLLOW_UP,
                        CareActionType.REQUEST_MORNING_REVIEW_FEEDBACK,
                    }
                    else CareAudience.ELDER
                ),
            )
            return product, principal, proposal

        def approve(
            product: ProductRequestContext,
            proposal: CareActionProposal,
            key: str,
            *,
            at: datetime = base + timedelta(minutes=1),
            expected_outcome: str = "applied",
        ) -> object:
            result = PostgresProductBackend(
                factory,
                cursor_key=hashlib.sha256(key.encode()).digest(),
                now_factory=lambda: at,
            ).decide_care_proposal(
                product,
                proposal_id=proposal.proposal_id,
                expected_version=proposal.version,
                choice="approve",
                idempotency_key=key,
                reason_code="human_approved",
                reason=None,
            )
            assert result.outcome == expected_outcome
            return result

        # Unapproved and rejected proposals never create a plan.
        product0, principal0, proposal0 = seed_and_context("u")
        service = CarePlanApplicationService(
            PostgresCarePlanRepository(factory),
            now_factory=lambda: base + timedelta(minutes=2),
        )
        assert service.list(principal0) == ()
        rejected = PostgresProductBackend(
            factory, cursor_key=b"x" * 32,
            now_factory=lambda: base + timedelta(minutes=1),
        ).decide_care_proposal(
            product0,
            proposal_id=proposal0.proposal_id,
            expected_version=proposal0.version,
            choice="reject",
            idempotency_key="reject-no-plan",
            reason_code="human_rejected",
            reason=None,
        )
        assert rejected.outcome == "applied"
        assert service.list(principal0) == ()

        # One-shot action permits direct COMPLETE; terminal resurrection fails.
        product1, principal1, proposal1 = seed_and_context(
            "d", action_type=CareActionType.RECOMMEND_MORNING_LIGHT
        )
        approve(product1, proposal1, "approve-direct")
        approve(
            product1,
            proposal1,
            "approve-direct",
            expected_outcome="idempotent",
        )
        direct_plan = service.list(principal1)[0].plan.care_plan_id
        direct = service.complete(
            principal1,
            care_plan_id=direct_plan,
            idempotency_key="direct-complete",
        )
        assert direct.outcome == "applied"
        assert direct.view.execution.state is CareExecutionState.COMPLETED
        assert len(service.list(
            principal1, state=CarePlanFilter.COMPLETED
        )) == 1
        assert service.complete(
            principal1,
            care_plan_id=direct_plan,
            idempotency_key="direct-complete",
        ).outcome == "idempotent"
        assert service.cancel(
            principal1,
            care_plan_id=direct_plan,
            idempotency_key="cancel-terminal",
        ).outcome == "conflict"

        # Family-audience follow-up uses the family binding end to end; an
        # elder role string cannot substitute for that server-side authority.
        product9, principal9, proposal9 = seed_and_context(
            "f", action_type=CareActionType.REQUEST_MANUAL_FOLLOW_UP
        )
        approve(product9, proposal9, "approve-family-follow-up")
        family_plan = service.list(principal9)[0].plan.care_plan_id
        assert service.complete(
            principal9,
            care_plan_id=family_plan,
            idempotency_key="family-follow-up-complete",
        ).outcome == "applied"
        with pytest.raises(Exception):
            service.show(
                replace(principal9, actor_role=CareAudience.ELDER),
                care_plan_id=family_plan,
            )
        assert service.start(
            principal1,
            care_plan_id=direct_plan,
            idempotency_key="start-terminal",
        ).outcome == "conflict"
        assert service.cancel(
            principal1,
            care_plan_id=direct_plan,
            idempotency_key="direct-complete",
        ).outcome == "conflict"

        # Bounded-period action requires START before COMPLETE.
        product2, principal2, proposal2 = seed_and_context("b")
        approve(product2, proposal2, "approve-bounded")
        bounded_plan = service.list(principal2)[0].plan.care_plan_id
        assert service.complete(
            principal2,
            care_plan_id=bounded_plan,
            idempotency_key="complete-without-start",
        ).outcome == "conflict"
        assert service.cancel(
            principal2,
            care_plan_id=bounded_plan,
            idempotency_key="cancel-not-started",
        ).outcome == "applied"
        assert service.cancel(
            principal2,
            care_plan_id=bounded_plan,
            idempotency_key="cancel-not-started",
        ).outcome == "idempotent"
        assert len(service.list(
            principal2, state=CarePlanFilter.CANCELLED
        )) == 1
        assert service.start(
            principal2,
            care_plan_id=bounded_plan,
            idempotency_key="start-cancelled",
        ).outcome == "conflict"
        assert service.complete(
            principal2,
            care_plan_id=bounded_plan,
            idempotency_key="complete-cancelled",
        ).outcome == "conflict"

        # Expired grant/window is visible but never executable.
        old = datetime.now(tz=UTC).replace(microsecond=0) - timedelta(hours=37)
        product3, principal3, proposal3 = seed_and_context("e", at=old)
        approve(
            product3,
            proposal3,
            "approve-expired",
            at=old + timedelta(minutes=1),
        )
        expired_service = CarePlanApplicationService(
            PostgresCarePlanRepository(factory)
        )
        expired_view = expired_service.list(principal3)[0]
        assert expired_view.execution.state is CareExecutionState.EXPIRED
        assert len(expired_service.list(
            principal3, state=CarePlanFilter.EXPIRED
        )) == 1
        assert expired_service.start(
            principal3,
            care_plan_id=expired_view.plan.care_plan_id,
            idempotency_key="start-expired",
        ).outcome == "expired"

        # Wrong role, missing scope, and stale epoch cannot acquire authority.
        product4, principal4, proposal4 = seed_and_context("a")
        approve(product4, proposal4, "approve-authority")
        authority_plan = service.list(principal4)[0].plan.care_plan_id
        with pytest.raises(Exception):
            service.show(
                replace(principal4, actor_role=CareAudience.FAMILY),
                care_plan_id=authority_plan,
            )
        with pytest.raises(Exception):
            service.start(
                replace(principal4, effective_scopes=frozenset({CARE_READ_SCOPE})),
                care_plan_id=authority_plan,
                idempotency_key="missing-scope",
            )
        assert PostgresCarePlanRepository(factory).list_plans(
            replace(principal4, authorization_epoch=99),
            state=None,
            limit=50,
            now=base + timedelta(minutes=2),
        ) == ()
        assert PostgresCarePlanRepository(factory).list_plans(
            replace(principal4, subject_id="wrong-subject"),
            state=None,
            limit=50,
            now=base + timedelta(minutes=2),
        ) == ()

        # G8 source supersession invalidates future execution, preserving START.
        started = service.start(
            principal4,
            care_plan_id=authority_plan,
            idempotency_key="start-before-supersession",
        )
        assert started.outcome == "applied"
        with psycopg.connect(admin_dsn) as admin:
            replacement = _persist_material_superseding_proposal(
                admin,
                namespace_id=product4.namespace_id,
                subject_id=product4.subject_id,
                suffix=f"{root}a",
                previous=proposal4,
                now=base + timedelta(minutes=3),
            )
            admin.commit()
        assert replacement.proposal_id != proposal4.proposal_id
        superseded = service.show(principal4, care_plan_id=authority_plan)
        assert superseded.execution.state is CareExecutionState.SUPERSEDED
        assert service.complete(
            principal4,
            care_plan_id=authority_plan,
            idempotency_key="complete-superseded",
        ).outcome == "superseded"
        history = service.history(principal4, care_plan_id=authority_plan)
        assert [item.event_type.value for item in history] == ["started"]

        # IN_PROGRESS COMPLETE/CANCEL contention has one authoritative winner.
        product5, principal5, proposal5 = seed_and_context("c")
        approve(product5, proposal5, "approve-complete-race")
        race_plan = service.list(principal5)[0].plan.care_plan_id
        assert service.start(
            principal5, care_plan_id=race_plan, idempotency_key="race-start"
        ).outcome == "applied"
        with ThreadPoolExecutor(max_workers=2) as executor:
            complete_future = executor.submit(
                service.complete,
                principal5,
                care_plan_id=race_plan,
                idempotency_key="race-complete",
            )
            cancel_future = executor.submit(
                service.cancel,
                principal5,
                care_plan_id=race_plan,
                idempotency_key="race-cancel",
            )
            outcomes = sorted(
                (complete_future.result().outcome, cancel_future.result().outcome)
            )
        assert outcomes == ["applied", "conflict"]
        assert len(service.history(principal5, care_plan_id=race_plan)) == 2

        # NOT_STARTED START/CANCEL contention also has exactly one winner.
        product7, principal7, proposal7 = seed_and_context("s")
        approve(product7, proposal7, "approve-start-race")
        start_race_plan = service.list(principal7)[0].plan.care_plan_id
        with ThreadPoolExecutor(max_workers=2) as executor:
            start_future = executor.submit(
                service.start,
                principal7,
                care_plan_id=start_race_plan,
                idempotency_key="start-race-start",
            )
            cancel_future = executor.submit(
                service.cancel,
                principal7,
                care_plan_id=start_race_plan,
                idempotency_key="start-race-cancel",
            )
            start_race_outcomes = sorted(
                (start_future.result().outcome, cancel_future.result().outcome)
            )
        assert start_race_outcomes == ["applied", "conflict"]
        assert len(service.history(principal7, care_plan_id=start_race_plan)) == 1

        # Explicit G8 revocation invalidates an in-progress plan.
        product6, principal6, proposal6 = seed_and_context("r")
        approve(product6, proposal6, "approve-revocation")
        revoked_plan = service.list(principal6)[0].plan.care_plan_id
        assert service.start(
            principal6,
            care_plan_id=revoked_plan,
            idempotency_key="start-before-revoke",
        ).outcome == "applied"
        detail = PostgresProductBackend(
            factory, cursor_key=b"v" * 32,
            now_factory=lambda: base + timedelta(minutes=4),
        ).get_care_proposal(product6, proposal_id=proposal6.proposal_id)
        assert detail is not None
        revoked = PostgresProductBackend(
            factory, cursor_key=b"w" * 32,
            now_factory=lambda: base + timedelta(minutes=4),
        ).revoke_care_proposal(
            product6,
            proposal_id=proposal6.proposal_id,
            expected_version=detail.proposal.version,
            idempotency_key="revoke-authority",
            reason_code="human_revoked",
            reason=None,
        )
        assert revoked.outcome == "applied"
        assert service.show(
            principal6, care_plan_id=revoked_plan
        ).execution.state is CareExecutionState.INVALIDATED
        assert len(service.list(
            principal6, state=CarePlanFilter.INVALIDATED
        )) == 1
        assert service.complete(
            principal6,
            care_plan_id=revoked_plan,
            idempotency_key="complete-after-revoke",
        ).outcome == "invalidated"
        assert [item.event_type.value for item in service.history(
            principal6, care_plan_id=revoked_plan
        )] == ["started"]

        # Revocation before execution begins prevents START and creates no
        # human execution evidence.
        product11, principal11, proposal11 = seed_and_context("v")
        approve(product11, proposal11, "approve-before-start-revocation")
        revoked_before_start_plan = service.list(principal11)[0].plan.care_plan_id
        before_start_detail = PostgresProductBackend(
            factory,
            cursor_key=b"k" * 32,
            now_factory=lambda: base + timedelta(minutes=5),
        ).get_care_proposal(product11, proposal_id=proposal11.proposal_id)
        assert before_start_detail is not None
        assert PostgresProductBackend(
            factory,
            cursor_key=b"l" * 32,
            now_factory=lambda: base + timedelta(minutes=5),
        ).revoke_care_proposal(
            product11,
            proposal_id=proposal11.proposal_id,
            expected_version=before_start_detail.proposal.version,
            idempotency_key="revoke-before-start",
            reason_code="human_revoked",
            reason=None,
        ).outcome == "applied"
        assert service.show(
            principal11, care_plan_id=revoked_before_start_plan
        ).execution.state is CareExecutionState.INVALIDATED
        assert service.start(
            principal11,
            care_plan_id=revoked_before_start_plan,
            idempotency_key="start-after-revoke",
        ).outcome == "invalidated"
        assert service.history(
            principal11, care_plan_id=revoked_before_start_plan
        ) == ()

        # A completion that predates revocation remains immutable history while
        # current grant authority is reported separately as revoked.
        product8, principal8, proposal8 = seed_and_context(
            "h", action_type=CareActionType.RECOMMEND_MORNING_LIGHT
        )
        approve(product8, proposal8, "approve-completed-history")
        historical_plan = service.list(principal8)[0].plan.care_plan_id
        assert service.complete(
            principal8,
            care_plan_id=historical_plan,
            idempotency_key="complete-before-revoke",
        ).outcome == "applied"
        historical_detail = PostgresProductBackend(
            factory,
            cursor_key=b"h" * 32,
            now_factory=lambda: base + timedelta(minutes=5),
        ).get_care_proposal(product8, proposal_id=proposal8.proposal_id)
        assert historical_detail is not None
        assert PostgresProductBackend(
            factory,
            cursor_key=b"i" * 32,
            now_factory=lambda: base + timedelta(minutes=5),
        ).revoke_care_proposal(
            product8,
            proposal_id=proposal8.proposal_id,
            expected_version=historical_detail.proposal.version,
            idempotency_key="revoke-after-completion",
            reason_code="human_revoked",
            reason=None,
        ).outcome == "applied"
        historical = service.show(principal8, care_plan_id=historical_plan)
        assert historical.execution.state is CareExecutionState.COMPLETED
        assert historical.approval_grant_state == "revoked"
        assert historical.executable is False
        assert [item.event_type.value for item in service.history(
            principal8, care_plan_id=historical_plan
        )] == ["completed"]
        historical_product = PostgresProductBackend(
            factory,
            cursor_key=b"j" * 32,
            now_factory=lambda: base + timedelta(minutes=6),
        ).get_care(product8, limit=20, cursor=None)
        historical_record = next(
            item
            for item in historical_product.items
            if item.record_type == "care_plan"
        )
        assert historical_record.state == "completed"
        assert historical_record.completion_semantics == (
            "human_attested_execution_only"
        )

        # Active and IN_PROGRESS list filters are subject/role scoped.
        product10, principal10, proposal10 = seed_and_context("i")
        approve(product10, proposal10, "approve-in-progress-filter")
        in_progress_plan = service.list(principal10)[0].plan.care_plan_id
        assert service.start(
            principal10,
            care_plan_id=in_progress_plan,
            idempotency_key="filter-start",
        ).outcome == "applied"
        assert len(service.list(
            principal10, state=CarePlanFilter.ACTIVE
        )) == 1
        assert len(service.list(
            principal10, state=CarePlanFilter.IN_PROGRESS
        )) == 1

        # Execution tracking produces no delivery, Habit, Memory, or outcome row.
        with psycopg.connect(admin_dsn) as admin:
            isolation = admin.execute(
                """
                SELECT
                  (SELECT count(*) FROM public.backend_delivery_intents
                    WHERE namespace_id LIKE %s),
                  (SELECT count(*) FROM public.backend_delivery_journal
                    WHERE namespace_id LIKE %s),
                  (SELECT count(*) FROM public.backend_governed_memory_revisions_v2
                    WHERE namespace_id LIKE %s),
                  (SELECT count(*) FROM public.backend_habit_profile_revisions_v2
                    WHERE namespace_id LIKE %s)
                """,
                tuple(f"live:g9-{root}%" for _ in range(4)),
            ).fetchone()
        assert isolation == (0, 0, 0, 0)

        # The application-facing internal aggregate is executable and contains
        # only the documented non-subject lifecycle dimensions.
        metrics = PostgresInternalStatus(
            type(
                "Settings",
                (),
                {
                    "data_mode": type("Mode", (), {"value": "live"})(),
                    "service_principal_id": principal_id,
                },
            )(),
            factory,
        ).operational_metrics()["care_execution"]
        expected_metrics = {
            "active_care_plans",
            "not_started_care_plans",
            "in_progress_care_plans",
            "completed_care_plans",
            "cancelled_care_plans",
            "expired_invalidated_care_plans",
            "oldest_executable_plan_age_seconds",
            "execution_command_conflict_error_count",
        }
        assert expected_metrics.issubset(metrics)
        assert all(metrics[key] >= 0 for key in expected_metrics)
        serialized_metrics = json.dumps(metrics, sort_keys=True)
        assert "subject" not in serialized_metrics
        assert "note" not in serialized_metrics

        structured_logs = "\n".join(
            record.getMessage() for record in caplog.records
        )
        for event_name in (
            "care_plan_created",
            "care_plan_deduplicated",
            "care_execution_started",
            "care_execution_completed",
            "care_execution_cancelled",
            "care_plan_expired",
            "care_plan_invalidated",
            "care_execution_command_deduplicated",
            "care_execution_command_conflict",
        ):
            assert event_name in structured_logs
        assert "今天" not in structured_logs
    finally:
        provider.close()
