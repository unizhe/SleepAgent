from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sleepagent.api.postgres import (
    PostgresProductBackend,
    ResolvedActorAuthority,
    _authorization_policy_sha256,
)
from sleepagent.api.product import ProductApiError, ProductRequestContext
from sleepagent.api.product_contracts import (
    OutcomePersonalizationDecisionRequest,
    ProductRole,
)
from sleepagent.application.care_execution import (
    CARE_READ_SCOPE,
    CareExecutionPrincipal,
    CarePlanApplicationService,
)
from sleepagent.application.night_finalization import (
    NightFinalizationPolicy,
    NightFinalizationService,
)
from sleepagent.config import ModelMode, ReportPipelineMode
from sleepagent.domain.care_actions import CareAudience
from sleepagent.domain.care_execution import CARE_EXECUTION_SCOPE
from sleepagent.application.personalization_governance import (
    CARE_OUTCOME_MEMORY_CONCEPT_ID,
)
from sleepagent.infrastructure.postgres_care_execution import (
    PostgresCarePlanRepository,
)
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
)
from sleepagent.workers.product import (
    CARE_EVALUATION_OPERATION,
    PRODUCT_REPORT_RUN_OPERATION,
    PRODUCT_SHARED_ANALYSIS_OPERATION,
    ProductAgentProcessor,
    ProductAgentWorkHandlerAdapter,
)
from sleepagent.workers.runtime import WorkContext, WorkDisposition
from sleepagent.domain.care_outcomes import CARE_OUTCOME_QUEUE
from tests.integration.test_g10_care_outcome_postgres import (
    _insert_night,
    _run_one,
)
from tests.integration.test_product_postgres_integration import (
    _cancel_pending_product_work,
    _claim_product_work,
    _convert_seed_to_report_request,
    _deterministic_runtime_bundle,
    _lease_for_claim,
    _prepare_shared_acceptance_case,
    _seed_product_scope,
    _worker_runtime,
)


pytestmark = pytest.mark.postgres
UTC = timezone.utc


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"{name} is required for PostgreSQL integration tests")
    return value


def _seed_elder_authority(
    connection: object,
    *,
    namespace_id: str,
    run_id: str,
    arm_id: str,
    subject_id: str,
    principal_id: str,
    now: datetime,
) -> tuple[ProductRequestContext, CareExecutionPrincipal]:
    suffix = uuid4().hex
    actor_id = f"c3-elder-{suffix}"
    binding_id = f"c3-binding-{suffix}"
    scopes = frozenset(
        {
            CARE_READ_SCOPE,
            CARE_EXECUTION_SCOPE,
            "product:sleep:care:confirm",
            "product:sleep:today:read",
            "product:sleep:interaction:write",
        }
    )
    cursor = connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute(
            "INSERT INTO public.backend_actors (actor_id,actor_kind,status) "
            "VALUES (%s,'human','active')",
            (actor_id,),
        )
        cursor.execute(
            """
            INSERT INTO public.backend_actor_subject_bindings (
              binding_id, namespace_id, data_mode, actor_id, subject_id, role,
              status, purpose_json, scopes_json, authorization_epoch, valid_from
            ) VALUES (%s,%s,'replay',%s,%s,'elder','active',
              '["sleep_care"]'::jsonb,%s::jsonb,1,%s)
            """,
            (
                binding_id,
                namespace_id,
                actor_id,
                subject_id,
                json.dumps(sorted(scopes)),
                now - timedelta(minutes=1),
            ),
        )
        cursor.execute(
            """
            INSERT INTO public.backend_principal_grants (
              grant_id, principal_id, namespace_id, data_mode, purpose,
              scopes_json, authorization_epoch, status, valid_from
            ) VALUES (%s,%s,%s,'replay','sleep_care',%s::jsonb,1,'active',
              %s)
            """,
            (
                f"c3-api-grant-{suffix}",
                principal_id,
                namespace_id,
                json.dumps(sorted(scopes)),
                now - timedelta(minutes=1),
            ),
        )
    finally:
        cursor.close()
    resolved = ResolvedActorAuthority(
        namespace_id=namespace_id,
        data_mode="replay",
        namespace_generation=1,
        run_id=run_id,
        arm_id=arm_id,
        binding_id=binding_id,
        role=ProductRole.ELDER,
        effective_scopes=scopes,
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
    )
    policy_hash = _authorization_policy_sha256(
        principal_id=principal_id,
        resolved=resolved,
        purpose="sleep_care",
    )
    context = ProductRequestContext(
        service_principal_id=principal_id,
        actor_id=actor_id,
        binding_id=binding_id,
        subject_id=subject_id,
        role=ProductRole.ELDER,
        effective_scopes=scopes,
        namespace_id=namespace_id,
        namespace_generation=1,
        data_mode="replay",
        run_id=run_id,
        arm_id=arm_id,
        purpose="sleep_care",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_epoch=1,
        policy_sha256=policy_hash,
    )
    principal = CareExecutionPrincipal(
        service_principal_id=principal_id,
        actor_id=actor_id,
        actor_role=CareAudience.ELDER,
        actor_binding_id=binding_id,
        subject_id=subject_id,
        effective_scopes=scopes,
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        namespace_id=namespace_id,
        namespace_generation=1,
        data_mode="replay",
        run_id=run_id,
        arm_id=arm_id,
    )
    return context, principal


def _decision_request(item: object, key: str):  # type: ignore[no-untyped-def]
    return OutcomePersonalizationDecisionRequest(
        expected_state_version=item.state_version,
        candidate_semantic_hash=item.candidate_semantic_hash,
        candidate_target_hash=item.candidate_target_hash,
        idempotency_key=key,
    )


def _outcome_items(
    backend: PostgresProductBackend,
    context: ProductRequestContext,
):  # type: ignore[no-untyped-def]
    return backend.list_outcome_personalization_candidates(
        context,
        status=None,
        limit=100,
    ).items


def _run_shared_cycle(
    *,
    psycopg: object,
    admin_dsn: str,
    worker_principal: str,
    base_seed: object,
    store: object,
    processor: ProductAgentProcessor,
    priority: int,
    episode_day_offset: int,
):  # type: ignore[no-untyped-def]
    seed = _seed_product_scope(
        psycopg,
        admin_dsn=admin_dsn,
        worker_principal=worker_principal,
        existing_scope=base_seed,
        episode_day_offset=episode_day_offset,
    )
    _convert_seed_to_report_request(psycopg, admin_dsn=admin_dsn, seed=seed)
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        admin.execute(
            "UPDATE public.sleep_domain_operations SET priority=%s "
            "WHERE operation_id=%s",
            (priority, seed.operation_id),
        )
        admin.commit()
    report_claim = _claim_product_work(
        store,
        worker_instance=f"c3-report-{priority}",
        operation_type=PRODUCT_REPORT_RUN_OPERATION,
    )
    routed = processor.route_report_request(
        store.uow_scope_for_claim(report_claim),  # type: ignore[attr-defined]
        _lease_for_claim(report_claim),
    )
    assert routed.shared_operation_id is not None
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        admin.execute(
            "UPDATE public.sleep_domain_operations SET priority=%s "
            "WHERE operation_id=%s",
            (priority + 1, routed.shared_operation_id),
        )
        admin.commit()
    shared_claim = _claim_product_work(
        store,
        worker_instance=f"c3-shared-{priority}",
        operation_type=PRODUCT_SHARED_ANALYSIS_OPERATION,
    )
    scope = store.uow_scope_for_claim(shared_claim)  # type: ignore[attr-defined]
    lease = _lease_for_claim(shared_claim)
    source = processor.load_source(scope, lease)
    artifact = processor.prepare_shared(
        scope=scope,
        source=source,
        lease=lease,
        prepared_at=datetime.now(tz=UTC),
    )
    processor.persist_and_commit(scope, lease, artifact, source=source)
    return seed, artifact


def _memory_items(artifact: object):  # type: ignore[no-untyped-def]
    receipts = artifact.role_runs[0].personalization.memory_read_receipts
    return tuple(item for receipt in receipts for item in receipt.items)


def _evaluate_next_outcome(
    *,
    psycopg: object,
    admin_dsn: str,
    store: object,
    seed: object,
    suffix: str,
    wake_at: datetime,
) -> None:
    with psycopg.connect(admin_dsn) as admin:  # type: ignore[attr-defined]
        _insert_night(
            admin,
            namespace_id=seed.namespace_id,
            subject_id=seed.subject_id,
            suffix=suffix,
            wake_at=wake_at,
            data_mode="replay",
            run_id=seed.run_id,
            arm_id=seed.arm_id,
        )
        admin.commit()
    states = []
    for _ in range(5):
        state = _run_one(store)  # type: ignore[arg-type]
        if state is None:
            break
        states.append(state)
        if state == "evaluated":
            break
    assert "evaluated" in states


def test_c3_receipt_governance_and_next_shared_analysis_process_proof() -> None:
    psycopg = pytest.importorskip("psycopg")
    admin_dsn = _required("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN")
    api_dsn = _required("SLEEPAGENT_TEST_POSTGRES_API_DSN")
    worker_dsn = _required("SLEEPAGENT_TEST_POSTGRES_WORKER_DSN")
    api_principal = _required("SLEEPAGENT_TEST_POSTGRES_API_PRINCIPAL")
    worker_principal = _required("SLEEPAGENT_TEST_POSTGRES_WORKER_PRINCIPAL")

    case = _prepare_shared_acceptance_case(
        psycopg,
        admin_dsn=admin_dsn,
        worker_dsn=worker_dsn,
        worker_principal=worker_principal,
        report_pipeline_mode=ReportPipelineMode.SHARED_ONLY,
        worker_queues=("product_agent", CARE_OUTCOME_QUEUE),
        care_eligible=True,
        fast_path_compatible=True,
        seed_l2_for_care=False,
    )
    api_pool = PsycopgPoolProvider.from_dsn(
        api_dsn,
        configuration=PoolConfiguration(min_size=1, max_size=4),
    )
    api_pool.open()
    restarted_provider = None
    try:
        close_at = case.source.episode.deterministic_close_deadline_at
        finalizer = NightFinalizationService(
            case.factory,
            policy=NightFinalizationPolicy(minimum_observation_count=0),
        )
        finalizer.finalize(
            case.scope,
            night_episode_id=case.seed.night_episode_id,
            evaluated_at=close_at + timedelta(hours=3),
        )
        hard = finalizer.finalize(
            case.scope,
            night_episode_id=case.seed.night_episode_id,
            evaluated_at=close_at + timedelta(hours=25),
        )
        case.processor.persist_and_commit(
            case.scope,
            case.lease,
            case.artifact,
            source=case.source,  # type: ignore[arg-type]
        )
        care_claim = _claim_product_work(
            case.store,
            worker_instance="c3-care-evaluation",
            operation_type=CARE_EVALUATION_OPERATION,
        )
        care_result = ProductAgentWorkHandlerAdapter(
            processor=case.processor,
            model_mode=ModelMode.DETERMINISTIC,
        )(WorkContext(care_claim, case.store, threading.Event()))
        assert care_result.disposition is WorkDisposition.SUCCEEDED
        proposal_id = care_result.result["proposal_id"]
        assert proposal_id is not None

        now = datetime.now(tz=UTC)
        with psycopg.connect(admin_dsn) as admin:
            context, principal = _seed_elder_authority(
                admin,
                namespace_id=case.seed.namespace_id,
                run_id=case.seed.run_id,
                arm_id=case.seed.arm_id,
                subject_id=case.seed.subject_id,
                principal_id=api_principal,
                now=now,
            )
            admin.execute(
                """
                UPDATE public.backend_principal_grants
                    SET allowed_handlers_json =
                      (allowed_handlers_json || %s::jsonb)
                    WHERE namespace_id=%s AND data_mode='replay'
                      AND purpose='worker'
                """,
                (
                        json.dumps([CARE_OUTCOME_QUEUE]),
                        case.seed.namespace_id,
                ),
            )
            _insert_night(
                admin,
                namespace_id=case.seed.namespace_id,
                subject_id=case.seed.subject_id,
                suffix=f"c3-baseline-a-{uuid4().hex}",
                wake_at=now - timedelta(days=2, hours=1),
                data_mode="replay",
                run_id=case.seed.run_id,
                arm_id=case.seed.arm_id,
            )
            _insert_night(
                admin,
                namespace_id=case.seed.namespace_id,
                subject_id=case.seed.subject_id,
                suffix=f"c3-baseline-b-{uuid4().hex}",
                wake_at=now - timedelta(days=1),
                data_mode="replay",
                run_id=case.seed.run_id,
                arm_id=case.seed.arm_id,
            )
            admin.commit()

        backend = PostgresProductBackend(
            UnitOfWorkFactory(api_pool),
            cursor_key=b"c" * 32,
            now_factory=lambda: now,
        )
        proposal = backend.get_care_proposal(
            context,
            proposal_id=proposal_id,
        )
        assert proposal is not None
        approved = backend.decide_care_proposal(
            context,
            proposal_id=proposal_id,
            expected_version=proposal.proposal.version,
            choice="approve",
            idempotency_key="c3-care-approve",
            reason_code="human_approved",
            reason=None,
        )
        assert approved.grant_id is not None
        clock = {"now": now}
        service = CarePlanApplicationService(
            PostgresCarePlanRepository(UnitOfWorkFactory(api_pool)),
            now_factory=lambda: clock["now"],
        )
        plan = service.list(principal)[0].plan
        service.start(
            principal,
            care_plan_id=plan.care_plan_id,
            idempotency_key="c3-plan-start",
        )
        service.complete(
            principal,
            care_plan_id=plan.care_plan_id,
            idempotency_key="c3-plan-complete",
            occurred_at=clock["now"],
        )
        assert _run_one(case.store) == "waiting_for_followup"

        base_wake = now
        with psycopg.connect(admin_dsn) as admin:
            _insert_night(
                admin,
                namespace_id=case.seed.namespace_id,
                subject_id=case.seed.subject_id,
                suffix=f"c3-follow-a-{uuid4().hex}",
                wake_at=base_wake + timedelta(days=1),
                data_mode="replay",
                run_id=case.seed.run_id,
                arm_id=case.seed.arm_id,
            )
            _insert_night(
                admin,
                namespace_id=case.seed.namespace_id,
                subject_id=case.seed.subject_id,
                suffix=f"c3-follow-b-{uuid4().hex}",
                wake_at=base_wake + timedelta(days=2, minutes=10),
                data_mode="replay",
                run_id=case.seed.run_id,
                arm_id=case.seed.arm_id,
            )
            admin.commit()
        outcome_states = []
        for _ in range(6):
            state = _run_one(case.store)
            if state is None:
                break
            outcome_states.append(state)
            if state == "evaluated":
                break
        assert "evaluated" in outcome_states

        backend = PostgresProductBackend(
            UnitOfWorkFactory(api_pool),
            cursor_key=b"c" * 32,
            now_factory=lambda: datetime.now(tz=UTC),
        )
        items = _outcome_items(backend, context)
        first = next(item for item in items if item.status == "pending")
        assert first.causal_claim is False
        assert first.memory_concept_id == CARE_OUTCOME_MEMORY_CONCEPT_ID
        with psycopg.connect(admin_dsn) as admin:
            assert admin.execute(
                "SELECT count(*) FROM "
                "public.backend_personalization_governance_v1 "
                "WHERE receipt_id=%s",
                (first.receipt_id,),
            ).fetchone() == (1,)
            before = admin.execute(
                """
                SELECT
                  (SELECT count(*) FROM public.backend_governed_memory_revisions_v2
                   WHERE namespace_id=%s AND subject_id=%s),
                  (SELECT count(*) FROM public.backend_habit_profile_revisions_v2
                   WHERE namespace_id=%s AND subject_id=%s)
                """,
                (
                    case.seed.namespace_id,
                    case.seed.subject_id,
                    case.seed.namespace_id,
                    case.seed.subject_id,
                ),
            ).fetchone()
        assert before == (0, 0)

        _pending_seed, pending_artifact = _run_shared_cycle(
            psycopg=psycopg,
            admin_dsn=admin_dsn,
            worker_principal=worker_principal,
            base_seed=case.seed,
            store=case.store,
            processor=case.processor,
            priority=60000,
            episode_day_offset=30,
        )
        assert not any(
            item.concept_id == CARE_OUTCOME_MEMORY_CONCEPT_ID
            for item in _memory_items(pending_artifact)
        )

        accept_request = _decision_request(first, "c3-accept-idempotency")
        accepted = backend.decide_outcome_personalization_candidate(
            context,
            governance_id=first.governance_id,
            choice="accept",
            request=accept_request,
        )
        replayed_accept = backend.decide_outcome_personalization_candidate(
            context,
            governance_id=first.governance_id,
            choice="accept",
            request=accept_request,
        )
        assert replayed_accept.idempotent_replay is True
        assert replayed_accept.memory_revision_ref == accepted.memory_revision_ref

        _evaluate_next_outcome(
            psycopg=psycopg,
            admin_dsn=admin_dsn,
            store=case.store,
            seed=case.seed,
            suffix=f"c3-follow-c-{uuid4().hex}",
            wake_at=base_wake + timedelta(days=3, minutes=5),
        )
        second = next(
            item for item in _outcome_items(backend, context)
            if item.status == "pending"
        )
        reject_request = _decision_request(second, "c3-reject-idempotency")
        rejected = backend.decide_outcome_personalization_candidate(
            context,
            governance_id=second.governance_id,
            choice="reject",
            request=reject_request,
        )
        assert rejected.memory_revision_ref is None
        assert backend.decide_outcome_personalization_candidate(
            context,
            governance_id=second.governance_id,
            choice="reject",
            request=reject_request,
        ).idempotent_replay is True

        _evaluate_next_outcome(
            psycopg=psycopg,
            admin_dsn=admin_dsn,
            store=case.store,
            seed=case.seed,
            suffix=f"c3-follow-d-{uuid4().hex}",
            wake_at=base_wake + timedelta(days=4, minutes=7),
        )
        third = next(
            item for item in _outcome_items(backend, context)
            if item.status == "pending"
        )
        _evaluate_next_outcome(
            psycopg=psycopg,
            admin_dsn=admin_dsn,
            store=case.store,
            seed=case.seed,
            suffix=f"c3-follow-e-{uuid4().hex}",
            wake_at=base_wake + timedelta(days=5, minutes=8),
        )
        refreshed = _outcome_items(backend, context)
        assert next(
            item for item in refreshed if item.governance_id == third.governance_id
        ).status == "superseded"
        fourth = next(item for item in refreshed if item.status == "pending")
        with pytest.raises(ProductApiError) as stale_candidate:
            backend.decide_outcome_personalization_candidate(
                context,
                governance_id=third.governance_id,
                choice="accept",
                request=_decision_request(third, "c3-stale-superseded"),
            )
        assert stale_candidate.value.code == "candidate_superseded"

        with pytest.raises(ProductApiError):
            backend.decide_outcome_personalization_candidate(
                replace(context, authorization_epoch=0),
                governance_id=fourth.governance_id,
                choice="accept",
                request=_decision_request(fourth, "c3-stale-epoch"),
            )
        with pytest.raises(ProductApiError):
            backend.decide_outcome_personalization_candidate(
                replace(context, subject_id="wrong-subject"),
                governance_id=fourth.governance_id,
                choice="accept",
                request=_decision_request(fourth, "c3-wrong-subject"),
            )
        with pytest.raises(ProductApiError):
            backend.decide_outcome_personalization_candidate(
                replace(context, namespace_id="replay:wrong-namespace"),
                governance_id=fourth.governance_id,
                choice="accept",
                request=_decision_request(fourth, "c3-wrong-namespace"),
            )
        with pytest.raises(ProductApiError):
            backend.decide_outcome_personalization_candidate(
                replace(
                    context,
                    namespace_id="live:wrong-mode",
                    data_mode="live",
                    run_id=None,
                    arm_id=None,
                ),
                governance_id=fourth.governance_id,
                choice="accept",
                request=_decision_request(fourth, "c3-wrong-mode"),
            )
        with pytest.raises(ProductApiError):
            backend.decide_outcome_personalization_candidate(
                replace(context, role=ProductRole.FAMILY),
                governance_id=fourth.governance_id,
                choice="accept",
                request=_decision_request(fourth, "c3-wrong-role"),
            )
        with pytest.raises(ProductApiError) as wrong_hash:
            backend.decide_outcome_personalization_candidate(
                context,
                governance_id=fourth.governance_id,
                choice="accept",
                request=_decision_request(
                    fourth.model_copy(update={"candidate_target_hash": "f" * 64}),
                    "c3-wrong-target-hash",
                ),
            )
        assert wrong_hash.value.code == "confirmation_binding_changed"

        corrected = backend.decide_outcome_personalization_candidate(
            context,
            governance_id=fourth.governance_id,
            choice="accept",
            request=_decision_request(fourth, "c3-correction-accept"),
        )
        assert corrected.memory_revision_ref != accepted.memory_revision_ref
        _evaluate_next_outcome(
            psycopg=psycopg,
            admin_dsn=admin_dsn,
            store=case.store,
            seed=case.seed,
            suffix=f"c3-follow-f-{uuid4().hex}",
            wake_at=base_wake + timedelta(days=6, minutes=9),
        )
        fifth = next(
            item for item in _outcome_items(backend, context)
            if item.status == "pending"
        )

        case.provider.close()
        restarted_provider, restarted_factory, restarted_store = _worker_runtime(
            worker_dsn=worker_dsn,
            worker_principal=worker_principal,
            namespace_id=case.seed.namespace_id,
            worker_queues=("product_agent", CARE_OUTCOME_QUEUE),
        )
        restarted_processor = ProductAgentProcessor(
            restarted_factory,
            runtime_bundle=_deterministic_runtime_bundle(),
            report_pipeline_mode=ReportPipelineMode.SHARED_ONLY,
        )
        accepted_seed, accepted_artifact = _run_shared_cycle(
            psycopg=psycopg,
            admin_dsn=admin_dsn,
            worker_principal=worker_principal,
            base_seed=case.seed,
            store=restarted_store,
            processor=restarted_processor,
            priority=70000,
            episode_day_offset=31,
        )
        selected = tuple(
            item
            for item in _memory_items(accepted_artifact)
            if item.concept_id == CARE_OUTCOME_MEMORY_CONCEPT_ID
        )
        assert len(selected) == 1
        assert selected[0].revision_ref == corrected.memory_revision_ref
        assert selected[0].source_ref == f"evidence:{fourth.care_outcome_id}"
        assert selected[0].typed_value in {"improved", "stable", "worsened"}
        assert accepted_artifact.shared_analysis is not None
        with psycopg.connect(admin_dsn) as admin:
            rows = admin.execute(
                """
                SELECT requesting_agent, receipt_json -> 'items'
                FROM public.backend_memory_read_receipts_v2
                WHERE namespace_id=%s AND subject_id=%s
                  AND product_episode_id=%s
                ORDER BY requesting_agent
                """,
                (
                    accepted_seed.namespace_id,
                    accepted_seed.subject_id,
                    accepted_artifact.role_runs[0].product_episode_id,
                ),
            ).fetchall()
            assert any(
                agent == "evidence_reasoning"
                and any(
                    item["revision_ref"] == corrected.memory_revision_ref
                    for item in items
                )
                for agent, items in rows
            )
            assert admin.execute(
                "SELECT count(*) FROM public.backend_governed_memory_revisions_v2 "
                "WHERE namespace_id=%s AND subject_id=%s",
                (case.seed.namespace_id, case.seed.subject_id),
            ).fetchone() == (2,)
            assert admin.execute(
                "SELECT count(*) FROM public.backend_habit_profile_revisions_v2 "
                "WHERE namespace_id=%s AND subject_id=%s",
                (case.seed.namespace_id, case.seed.subject_id),
            ).fetchone() == (0,)

        barrier = threading.Barrier(2)

        def decide(choice: str, key: str):  # type: ignore[no-untyped-def]
            local_pool = PsycopgPoolProvider.from_dsn(
                api_dsn,
                configuration=PoolConfiguration(min_size=1, max_size=1),
            )
            local_pool.open()
            try:
                local_backend = PostgresProductBackend(
                    UnitOfWorkFactory(local_pool),
                    cursor_key=b"r" * 32,
                )
                barrier.wait()
                return local_backend.decide_outcome_personalization_candidate(
                    context,
                    governance_id=fifth.governance_id,
                    choice=choice,  # type: ignore[arg-type]
                    request=_decision_request(fifth, key),
                )
            except ProductApiError as exc:
                return exc
            finally:
                local_pool.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            race_results = (
                executor.submit(decide, "accept", "c3-race-accept"),
                executor.submit(decide, "reject", "c3-race-reject"),
            )
            results = [item.result(timeout=30) for item in race_results]
        assert sum(not isinstance(item, ProductApiError) for item in results) == 1
        assert sum(isinstance(item, ProductApiError) for item in results) == 1
        with psycopg.connect(admin_dsn) as admin:
            assert admin.execute(
                "SELECT count(*) FROM "
                "public.backend_personalization_governance_decisions_v1 "
                "WHERE governance_id=%s",
                (fifth.governance_id,),
            ).fetchone() == (1,)
            assert admin.execute(
                "SELECT count(*) FROM public.backend_governed_memory_revisions_v2 "
                "WHERE namespace_id=%s AND subject_id=%s",
                (case.seed.namespace_id, case.seed.subject_id),
            ).fetchone()[0] in {2, 3}
    finally:
        _cancel_pending_product_work(
            psycopg,
            admin_dsn=admin_dsn,
            namespace_id=case.seed.namespace_id,
        )
        api_pool.close()
        if restarted_provider is not None:
            restarted_provider.close()
        else:
            try:
                case.provider.close()
            except Exception:
                pass
