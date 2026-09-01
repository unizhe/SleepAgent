from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sleepagent.api.postgres import PostgresProductBackend
from sleepagent.api.product import ProductApiError, ProductRequestContext
from sleepagent.api.product_contracts import ProductRole
from sleepagent.domain.care_actions import (
    CareActionCandidateV2,
    CareActionProposal,
    DeterministicCareActionPolicy,
)
from sleepagent.application.care_actions import persist_care_action_proposal
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


def _seed_proposal(
    admin: object,
    *,
    namespace_id: str,
    subject_id: str,
    suffix: str,
    now: datetime,
) -> CareActionProposal:
    episode_id = f"night-{suffix}"
    episode_revision_id = f"night-revision-{suffix}"
    finalization_id = f"finalization-{suffix}"
    finalization_revision_id = f"finalization-revision-{suffix}"
    analysis_revision_id = f"analysis-{suffix}"
    policy_hash = hashlib.sha256(b"night-finalization.v1").hexdigest()
    material_hash = hashlib.sha256(suffix.encode()).hexdigest()
    cursor = admin.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_episodes (
              night_episode_id, namespace_id, data_mode, subject_id, night_key,
              state, current_revision_id, current_revision_number, cas_version,
              episode_json, created_at, updated_at
            ) VALUES (%s, %s, 'live', %s, %s, 'closed', %s, 1, 1,
              '{}'::jsonb, %s, %s)
            """,
            (
                episode_id,
                namespace_id,
                subject_id,
                f"2026-09-{suffix[-2:]}",
                episode_revision_id,
                now,
                now,
            ),
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_episode_revisions (
              night_episode_revision_id, namespace_id, data_mode,
              night_episode_id, subject_id, revision_number, revision_json,
              created_at
            ) VALUES (%s, %s, 'live', %s, %s, 1, '{}'::jsonb, %s)
            """,
            (
                episode_revision_id,
                namespace_id,
                episode_id,
                subject_id,
                now,
            ),
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_night_finalizations (
              night_finalization_id, namespace_id, data_mode,
              namespace_generation, subject_id, night_episode_id, state,
              policy_version, policy_sha256, created_at, updated_at
            ) VALUES (%s, %s, 'live', 1, %s, %s, 'open',
              'night-finalization.v1', %s, %s, %s)
            """,
            (
                finalization_id,
                namespace_id,
                subject_id,
                episode_id,
                policy_hash,
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
            ) VALUES (%s, %s, %s, 'live', 1, %s, %s, 1, %s,
              'hard_finalized', FALSE, 'complete', 'wake_grace_elapsed', %s,
              '{}'::jsonb, %s)
            """,
            (
                finalization_revision_id,
                finalization_id,
                namespace_id,
                subject_id,
                episode_id,
                episode_revision_id,
                material_hash,
                now,
            ),
        )
        cursor.execute(
            """
            UPDATE public.sleep_domain_night_finalizations
            SET state = 'hard_finalized',
                current_finalization_revision_id = %s,
                current_revision_number = 1, cas_version = 1,
                updated_at = %s
            WHERE night_finalization_id = %s
            """,
            (finalization_revision_id, now, finalization_id),
        )
        cursor.execute(
            """
            INSERT INTO public.sleep_domain_analysis_revisions (
              analysis_revision_id, namespace_id, data_mode, night_episode_id,
              night_episode_revision_id, subject_id, revision_number,
              analysis_json, created_at
            ) VALUES (%s, %s, 'live', %s, %s, %s, 1, '{}'::jsonb, %s)
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
            action_type="recommend_consistent_wake_time",
            catalog_action_id="consistent-wake-time",
            catalog_action_version=1,
            intent="routine_adjustment",
            rationale_evidence_refs=(f"claim-{suffix}",),
            urgency="normal",
            audience="elder",
            parameters={"tolerance_minutes": 30},
            created_at=now,
            display_explanation="Keep a consistent wake-time window.",
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
                worker_instance="g8-proof",
            ),
            night_episode_id=episode_id,
            proposal=proposal,
            persisted_at=now,
        )
        return proposal
    finally:
        cursor.close()


def _supersede_proposal(
    admin: object,
    *,
    namespace_id: str,
    subject_id: str,
    suffix: str,
    previous: CareActionProposal,
    now: datetime,
) -> CareActionProposal:
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
        policy = DeterministicCareActionPolicy().evaluate(
            candidate,
            source_is_current=True,
            source_is_hard_finalized=True,
            evidence_is_sufficient=True,
        )
        proposal = CareActionProposal.create(candidate, policy)
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
                worker_instance="g8-proof",
            ),
            night_episode_id=f"night-{suffix}",
            proposal=proposal,
            persisted_at=now,
        )
        return proposal
    finally:
        cursor.close()


def test_g8_non_owner_hitl_authority_is_durable_and_effect_free() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _required("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    principal_id = os.environ.get(
        "SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL", "sleepagent-api-test"
    )
    suffix = uuid4().hex
    namespace_id = f"live:g8-{suffix}"
    subject_id = f"subject-{suffix}"
    actor_id = f"actor-{suffix}"
    binding_id = f"binding-{suffix}"
    now = datetime.now(tz=UTC).replace(microsecond=0)
    scopes = frozenset(
        {"product:sleep:care:read", "product:sleep:care:confirm"}
    )

    with psycopg.connect(admin_dsn) as admin:
        with psycopg.connect(api_dsn) as api_identity:
            api_role = api_identity.info.user
        admin.execute(
            "INSERT INTO public.backend_namespaces "
            "(namespace_id,data_mode,current_generation,status,synthetic_non_release) "
            "VALUES (%s,'live',1,'active',FALSE)",
            (namespace_id,),
        )
        admin.execute(
            "INSERT INTO public.backend_namespace_generations "
            "(namespace_id,data_mode,generation,status,configuration_sha256) "
            "VALUES (%s,'live',1,'active',%s)",
            (namespace_id, hashlib.sha256(namespace_id.encode()).hexdigest()),
        )
        admin.execute(
            "INSERT INTO public.backend_actors (actor_id,actor_kind,status) "
            "VALUES (%s,'human','active')",
            (actor_id,),
        )
        admin.execute(
            "INSERT INTO public.backend_subjects "
            "(namespace_id,data_mode,subject_id,timezone_name,status) "
            "VALUES (%s,'live',%s,'UTC','active')",
            (namespace_id, subject_id),
        )
        admin.execute(
            "INSERT INTO public.backend_subject_epochs "
            "(namespace_id,data_mode,subject_id,authorization_epoch,"
            "privacy_epoch,retrieval_policy_epoch) VALUES (%s,'live',%s,1,1,1)",
            (namespace_id, subject_id),
        )
        admin.execute(
            """
            INSERT INTO public.backend_actor_subject_bindings (
              binding_id, namespace_id, data_mode, actor_id, subject_id, role,
              status, purpose_json, scopes_json, authorization_epoch, valid_from
            ) VALUES (%s,%s,'live',%s,%s,'elder','active',
              '["sleep_care"]'::jsonb,
              '["product:sleep:care:read","product:sleep:care:confirm"]'::jsonb,
              1,%s)
            """,
            (binding_id, namespace_id, actor_id, subject_id, now - timedelta(minutes=1)),
        )
        admin.execute(
            """
            INSERT INTO public.backend_principal_grants (
              grant_id, principal_id, namespace_id, data_mode, purpose,
              scopes_json, authorization_epoch, status, valid_from
            ) VALUES (%s,%s,%s,'live','sleep_care',
              '["product:sleep:care:read","product:sleep:care:confirm"]'::jsonb,
              1,'active',%s)
            """,
            (f"principal-grant-{suffix}", principal_id, namespace_id, now - timedelta(minutes=1)),
        )
        first = _seed_proposal(
            admin,
            namespace_id=namespace_id,
            subject_id=subject_id,
            suffix=f"{suffix}01",
            now=now,
        )
        concurrent = _seed_proposal(
            admin,
            namespace_id=namespace_id,
            subject_id=subject_id,
            suffix=f"{suffix}02",
            now=now,
        )
        expired_proposal = _seed_proposal(
            admin,
            namespace_id=namespace_id,
            subject_id=subject_id,
            suffix=f"{suffix}03",
            now=now - timedelta(days=2),
        )
        stale_suffix = f"{suffix}04"
        stale_proposal = _seed_proposal(
            admin,
            namespace_id=namespace_id,
            subject_id=subject_id,
            suffix=stale_suffix,
            now=now,
        )
        replacement_proposal = _supersede_proposal(
            admin,
            namespace_id=namespace_id,
            subject_id=subject_id,
            suffix=stale_suffix,
            previous=stale_proposal,
            now=now + timedelta(seconds=1),
        )
        admin.commit()

        assert api_role != admin.info.user

    context = ProductRequestContext(
        service_principal_id=principal_id,
        actor_id=actor_id,
        binding_id=binding_id,
        subject_id=subject_id,
        role=ProductRole.ELDER,
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
    provider = PsycopgPoolProvider.from_dsn(
        api_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=2),
        application_name="sleepagent-g8-postgres-proof",
    )
    provider.open()
    try:
        backend = PostgresProductBackend(
            UnitOfWorkFactory(provider),
            cursor_key=b"g" * 32,
            now_factory=lambda: now + timedelta(minutes=1),
        )
        visible = backend.list_care_proposals(context, state=None, limit=10)
        assert first.proposal_id in {item.proposal_id for item in visible.items}

        wrong_role = replace(context, role=ProductRole.FAMILY)
        with pytest.raises(ProductApiError) as unauthorized:
            backend.decide_care_proposal(
                wrong_role,
                proposal_id=concurrent.proposal_id,
                expected_version=concurrent.version,
                choice="approve",
                idempotency_key="unauthorized-family",
                reason_code="wrong_role",
                reason=None,
            )
        assert unauthorized.value.status_code == 403

        completed = subprocess.run(
            (
                sys.executable,
                "-m",
                "scripts.g8_hitl_process_probe",
                "--actor-id",
                actor_id,
                "--subject-id",
                subject_id,
                "--proposal-id",
                first.proposal_id,
                "--expected-version",
                str(first.version),
                "--idempotency-key",
                "approve-once",
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        approved_payload = json.loads(completed.stdout.strip().splitlines()[-1])
        retried = backend.decide_care_proposal(
            context,
            proposal_id=first.proposal_id,
            expected_version=first.version,
            choice="approve",
            idempotency_key="approve-once",
            reason_code="human_confirmed",
            reason=None,
        )
        conflict = backend.decide_care_proposal(
            context,
            proposal_id=first.proposal_id,
            expected_version=first.version,
            choice="reject",
            idempotency_key="reject-after-approval",
            reason_code="changed_mind",
            reason=None,
        )
        assert approved_payload["outcome"] == "applied"
        assert approved_payload["state"] == "approved"
        assert approved_payload["grant_id"] is not None
        assert retried.outcome == "idempotent"
        assert retried.grant_id == approved_payload["grant_id"]
        assert conflict.outcome == "conflict"

        def decide_concurrently(choice: str):
            return backend.decide_care_proposal(
                context,
                proposal_id=concurrent.proposal_id,
                expected_version=concurrent.version,
                choice=choice,  # type: ignore[arg-type]
                idempotency_key=f"concurrent-{choice}",
                reason_code=f"human_{choice}",
                reason=None,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_results = tuple(
                executor.map(decide_concurrently, ("approve", "reject"))
            )
        assert sorted(item.outcome for item in concurrent_results) == [
            "applied",
            "conflict",
        ]

        expired_result = backend.decide_care_proposal(
            context,
            proposal_id=expired_proposal.proposal_id,
            expected_version=expired_proposal.version,
            choice="approve",
            idempotency_key="too-late",
            reason_code="human_confirmed",
            reason=None,
        )
        assert expired_result.outcome == "expired"
        assert expired_result.grant_id is None
    finally:
        provider.close()

    provider = PsycopgPoolProvider.from_dsn(
        api_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=1),
        application_name="sleepagent-g8-postgres-restart-proof",
    )
    provider.open()
    try:
        reopened = PostgresProductBackend(
            UnitOfWorkFactory(provider),
            cursor_key=b"g" * 32,
            now_factory=lambda: now + timedelta(minutes=2),
        )
        detail = reopened.get_care_proposal(
            context, proposal_id=first.proposal_id
        )
        assert detail is not None
        assert detail.proposal.state == "approved"
        assert detail.proposal.grant_id == approved_payload["grant_id"]
        revoked = reopened.revoke_care_proposal(
            context,
            proposal_id=first.proposal_id,
            expected_version=first.version + 1,
            idempotency_key="revoke-once",
            reason_code="human_revoked",
            reason=None,
        )
        assert revoked.outcome == "applied"
        assert revoked.state == "revoked"
    finally:
        provider.close()

    with psycopg.connect(admin_dsn) as admin:
        proposal_state, proposal_version = admin.execute(
            "SELECT state,version FROM public.backend_care_action_proposals_v3 "
            "WHERE proposal_id=%s",
            (first.proposal_id,),
        ).fetchone()
        grant_state, grant_version = admin.execute(
            "SELECT state,version FROM public.backend_approval_grants_v3 "
            "WHERE proposal_id=%s",
            (first.proposal_id,),
        ).fetchone()
        decisions = admin.execute(
            "SELECT choice FROM public.backend_care_action_decisions_v3 "
            "WHERE proposal_id=%s ORDER BY decided_at,decision_id",
            (first.proposal_id,),
        ).fetchall()
        external_counts = admin.execute(
            "SELECT "
            "(SELECT count(*) FROM public.backend_delivery_intents "
            " WHERE namespace_id=%s), "
            "(SELECT count(*) FROM public.backend_delivery_journal "
            " WHERE namespace_id=%s), "
            "(SELECT count(*) FROM public.backend_replay_delivery_effects_v2 "
            " WHERE namespace_id=%s), "
            "(SELECT count(*) FROM public.backend_governed_memory_revisions_v2 "
            " WHERE namespace_id=%s)",
            (namespace_id,) * 4,
        ).fetchone()
        assert (proposal_state, proposal_version) == ("revoked", first.version + 2)
        assert (grant_state, grant_version) == ("revoked", 2)
        assert sorted(row[0] for row in decisions) == [
            "approve",
            "conflict",
            "revoke",
        ]
        assert external_counts == (0, 0, 0, 0)
        stale_state = admin.execute(
            "SELECT state FROM public.backend_care_action_proposals_v3 "
            "WHERE proposal_id=%s",
            (stale_proposal.proposal_id,),
        ).fetchone()[0]
        replacement_state = admin.execute(
            "SELECT state FROM public.backend_care_action_proposals_v3 "
            "WHERE proposal_id=%s",
            (replacement_proposal.proposal_id,),
        ).fetchone()[0]
        assert stale_state == "expired"
        assert replacement_state == "awaiting_approval"

    with psycopg.connect(api_dsn) as api:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api.execute(
                "UPDATE public.backend_care_action_proposals_v3 "
                "SET state='approved' WHERE proposal_id=%s",
                (first.proposal_id,),
            )
