from __future__ import annotations

import json
import os
import time

import pytest


pytestmark = pytest.mark.postgres


def test_stage4_process_proof_has_induction_and_delivery_effects() -> None:
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ.get("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN", "").strip()
    root_operation_id = os.environ.get(
        "SLEEPAGENT_STAGE4_ROOT_OPERATION_ID", ""
    ).strip()
    raw_operations = os.environ.get("SLEEPAGENT_STAGE4_OPERATION_IDS", "").strip()
    if not dsn or not root_operation_id or not raw_operations:
        pytest.skip("a completed Stage-4 process proof database is required")
    operation_ids = json.loads(raw_operations)
    assert isinstance(operation_ids, dict)
    confirm_operation_id = str(operation_ids["confirm"])
    fault_delivery_intent_id = os.environ.get(
        "SLEEPAGENT_STAGE4_FAULT_DELIVERY_INTENT_ID", ""
    ).strip()

    deadline = time.monotonic() + 90
    last_state: tuple[object, ...] | None = None
    while time.monotonic() < deadline:
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                scope = _journey_scope(cursor, root_operation_id)
                last_state = _stage4_counts(
                    cursor,
                    scope=scope,
                    confirm_operation_id=confirm_operation_id,
                )
        if last_state == (5, 5, 5, 5, 1, 1, 1, 1):
            break
        time.sleep(0.25)
    assert last_state == (5, 5, 5, 5, 1, 1, 1, 1)

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            scope = _journey_scope(cursor, root_operation_id)
            namespace_id, generation, run_id, arm_id, subject_id = scope
            cursor.execute(
                "SELECT profile.current_version, profile.cas_version, "
                "count(revision.*), count(DISTINCT revision.source_analysis_revision_id) "
                "FROM public.backend_personalization_profiles_v2 AS profile "
                "JOIN public.backend_personalization_profile_revisions_v2 AS revision "
                "ON revision.profile_id = profile.profile_id "
                "WHERE profile.namespace_id = %s AND profile.data_mode = 'replay' "
                "AND profile.namespace_generation = %s AND profile.run_id = %s "
                "AND profile.arm_id = %s AND profile.subject_id = %s "
                "GROUP BY profile.current_version, profile.cas_version",
                (namespace_id, generation, run_id, arm_id, subject_id),
            )
            assert cursor.fetchone() == (5, 5, 5, 5)

            cursor.execute(
                "SELECT intent.status, intent.attempt_count, "
                "invocation.current_state, effect.result_json ->> 'status', "
                "count(journal.*) "
                "FROM public.backend_delivery_intents AS intent "
                "JOIN public.sleep_domain_domain_outbox AS source "
                "ON source.event_id = intent.source_event_id "
                "JOIN public.backend_invocations AS invocation "
                "ON invocation.operation_id = source.operation_id "
                "AND invocation.invocation_kind = 'external_sink' "
                "JOIN public.backend_replay_delivery_effects_v2 AS effect "
                "ON effect.delivery_intent_id = intent.delivery_intent_id "
                "JOIN public.backend_delivery_journal AS journal "
                "ON journal.delivery_intent_id = intent.delivery_intent_id "
                "WHERE source.operation_id = %s "
                "GROUP BY intent.status, intent.attempt_count, "
                "invocation.current_state, effect.result_json ->> 'status'",
                (confirm_operation_id,),
            )
            delivered = cursor.fetchone()
            assert delivered is not None
            expected_attempt_count = 3 if fault_delivery_intent_id else 1
            assert delivered[:4] == (
                "delivered",
                expected_attempt_count,
                "response_received",
                "delivered",
            )
            assert int(delivered[4]) >= 3

            cursor.execute(
                "SELECT count(*) FROM public.backend_consumer_inbox AS inbox "
                "JOIN public.backend_delivery_intents AS intent "
                "ON intent.delivery_intent_id = inbox.delivery_intent_id "
                "JOIN public.sleep_domain_domain_outbox AS source "
                "ON source.event_id = intent.source_event_id "
                "WHERE source.operation_id = %s AND inbox.applied_at IS NOT NULL",
                (confirm_operation_id,),
            )
            assert cursor.fetchone() == (1,)

            cursor.execute(
                "SELECT action.state FROM public.backend_care_actions_v2 AS action "
                "JOIN public.backend_human_decisions_v2 AS decision "
                "ON decision.human_decision_id = action.human_decision_id "
                "AND decision.interaction_id = action.interaction_id "
                "WHERE decision.operation_id = %s",
                (confirm_operation_id,),
            )
            care_state = cursor.fetchone()
            assert care_state == ("active",)

            if fault_delivery_intent_id:
                cursor.execute(
                    "SELECT resolution FROM "
                    "public.backend_delivery_reconciliation_receipts_v2 "
                    "WHERE delivery_intent_id = %s",
                    (fault_delivery_intent_id,),
                )
                assert cursor.fetchone() == ("known_not_delivered",)
                cursor.execute(
                    "SELECT array_agg(journal.to_state ORDER BY journal.sequence) "
                    "FROM public.backend_invocation_journal AS journal "
                    "JOIN public.backend_invocations AS invocation "
                    "ON invocation.invocation_id = journal.invocation_id "
                    "JOIN public.backend_delivery_intents AS intent "
                    "ON invocation.invocation_key = "
                    "'replay-delivery:' || intent.semantic_effect_key || ':v1' "
                    "WHERE intent.delivery_intent_id = %s",
                    (fault_delivery_intent_id,),
                )
                states = cursor.fetchone()[0]
                assert states.count("send_started") == 2
                assert "reconciled" in states
                assert states[-1] == "response_received"


def _journey_scope(cursor, root_operation_id: str) -> tuple[object, ...]:
    cursor.execute(
        "SELECT namespace_id, namespace_generation, run_id, arm_id, subject_id "
        "FROM public.backend_demo_journeys WHERE root_operation_id = %s",
        (root_operation_id,),
    )
    row = cursor.fetchone()
    assert row is not None
    return tuple(row)


def _stage4_counts(
    cursor,
    *,
    scope: tuple[object, ...],
    confirm_operation_id: str,
) -> tuple[object, ...]:
    namespace_id, generation, run_id, arm_id, subject_id = scope
    params = (namespace_id, generation, run_id, arm_id, subject_id)
    cursor.execute(
        "SELECT count(*) FILTER (WHERE operation_type = 'product_agent' "
        "AND status = 'succeeded'), "
        "count(*) FILTER (WHERE operation_type = 'induction'), "
        "count(*) FILTER (WHERE operation_type = 'induction' "
        "AND status = 'succeeded') "
        "FROM public.sleep_domain_operations "
        "WHERE namespace_id = %s AND data_mode = 'replay' "
        "AND namespace_generation = %s AND run_id = %s AND arm_id = %s "
        "AND subject_id = %s",
        params,
    )
    product_count, induction_count, induction_succeeded = cursor.fetchone()
    cursor.execute(
        "SELECT count(*) FROM public.backend_induction_receipts_v2 "
        "WHERE namespace_id = %s AND data_mode = 'replay' "
        "AND namespace_generation = %s AND run_id = %s AND arm_id = %s "
        "AND subject_id = %s AND status = 'succeeded'",
        params,
    )
    receipt_count = cursor.fetchone()[0]
    cursor.execute(
        "SELECT count(*) FILTER (WHERE intent.status = 'delivered'), "
        "count(effect.*), count(inbox.*), count(invocation.*) FILTER ("
        "WHERE invocation.current_state = 'response_received') "
        "FROM public.backend_delivery_intents AS intent "
        "JOIN public.sleep_domain_domain_outbox AS source "
        "ON source.event_id = intent.source_event_id "
        "LEFT JOIN public.backend_replay_delivery_effects_v2 AS effect "
        "ON effect.delivery_intent_id = intent.delivery_intent_id "
        "LEFT JOIN public.backend_consumer_inbox AS inbox "
        "ON inbox.delivery_intent_id = intent.delivery_intent_id "
        "LEFT JOIN public.backend_invocations AS invocation "
        "ON invocation.operation_id = source.operation_id "
        "AND invocation.invocation_kind = 'external_sink' "
        "WHERE source.operation_id = %s",
        (confirm_operation_id,),
    )
    delivery = cursor.fetchone()
    return (
        product_count,
        induction_count,
        induction_succeeded,
        receipt_count,
        *delivery,
    )
