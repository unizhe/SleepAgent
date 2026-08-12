from __future__ import annotations

import json
import os

import pytest


pytestmark = pytest.mark.postgres


EXPECTED_OPERATION_TYPES = {
    "confirm_start": "interaction.start",
    "confirm_ask": "interaction.ask",
    "confirm_answer": "interaction.answer",
    "confirm": "interaction.confirm",
    "decline_start": "interaction.start",
    "decline_answer": "interaction.answer",
    "decline": "interaction.decline",
    "product_feedback": "interaction.feedback",
    "monitoring_activate": "sleep_api.monitoring.activate.v1",
    "monitoring_deactivate": "sleep_api.monitoring.deactivate.v1",
    "elder_feedback": "sleep_api.feedback.elder.v1",
    "family_feedback": "sleep_api.feedback.family.v1",
    "explicit_reanalysis": "sleep_api.reanalysis.v1",
}


def test_stage2_process_proof_has_exact_local_effects() -> None:
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ.get("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN", "").strip()
    raw_operations = os.environ.get("SLEEPAGENT_STAGE2_OPERATION_IDS", "").strip()
    if not dsn or not raw_operations:
        pytest.skip("a completed Stage-2 process proof database is required")
    operation_ids = json.loads(raw_operations)
    assert isinstance(operation_ids, dict)
    assert set(operation_ids) == set(EXPECTED_OPERATION_TYPES)
    ids = list(operation_ids.values())
    assert len(ids) == len(set(ids)) == len(EXPECTED_OPERATION_TYPES)

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT operation_id, operation_type, status, queue_name "
                "FROM public.sleep_domain_operations "
                "WHERE operation_id = ANY(%s)",
                (ids,),
            )
            operations = {
                str(row[0]): (str(row[1]), str(row[2]), str(row[3]))
                for row in cursor.fetchall()
            }
            assert set(operations) == set(ids)
            for name, operation_id in operation_ids.items():
                expected_type = EXPECTED_OPERATION_TYPES[name]
                expected_queue = (
                    "product_interaction"
                    if expected_type.startswith("interaction.")
                    else "sleep_command"
                )
                assert operations[operation_id] == (
                    expected_type,
                    "succeeded",
                    expected_queue,
                )

            cursor.execute(
                "SELECT count(*) FROM public.backend_command_receipts "
                "WHERE operation_id = ANY(%s)",
                (ids,),
            )
            assert cursor.fetchone() == (len(ids),)
            cursor.execute(
                "SELECT count(*) FROM public.sleep_domain_domain_outbox "
                "WHERE operation_id = ANY(%s) "
                "AND event_type = 'STAGE2_COMMAND_COMMITTED' "
                "AND status = 'committed'",
                (ids,),
            )
            assert cursor.fetchone() == (len(ids),)

            cursor.execute(
                "SELECT receipt.operation_id, receipt.monitoring_snapshot_id, "
                "receipt.from_state, receipt.to_state, receipt.from_cas_version, "
                "receipt.to_cas_version "
                "FROM public.backend_monitoring_transition_receipts AS receipt "
                "WHERE receipt.operation_id = ANY(%s) "
                "ORDER BY receipt.from_cas_version",
                (
                    [
                        operation_ids["monitoring_activate"],
                        operation_ids["monitoring_deactivate"],
                    ],
                ),
            )
            monitoring_receipts = cursor.fetchall()
            assert len(monitoring_receipts) == 2
            activate, deactivate = monitoring_receipts
            assert activate[0] == operation_ids["monitoring_activate"]
            assert activate[2:4] == ("dormant", "active")
            assert activate[5] == activate[4] + 1
            assert deactivate[0] == operation_ids["monitoring_deactivate"]
            assert deactivate[1] == activate[1]
            assert deactivate[2:4] == ("active", "dormant")
            assert deactivate[4] == activate[5]
            assert deactivate[5] == deactivate[4] + 1
            cursor.execute(
                "SELECT state, cas_version "
                "FROM public.backend_monitoring_snapshots_v2 AS snapshot "
                "WHERE snapshot.monitoring_snapshot_id = %s",
                (activate[1],),
            )
            assert cursor.fetchone() == ("dormant", deactivate[5])

            cursor.execute(
                "SELECT canonical_source, count(*) "
                "FROM public.backend_human_facts "
                "WHERE operation_id = ANY(%s) GROUP BY canonical_source",
                (ids,),
            )
            assert dict(cursor.fetchall()) == {
                "elder_self_report": 2,
                "family_observation": 1,
            }
            reanalysis_source_ids = [
                operation_ids["product_feedback"],
                operation_ids["elder_feedback"],
                operation_ids["family_feedback"],
                operation_ids["explicit_reanalysis"],
            ]
            cursor.execute(
                "SELECT count(*), count(DISTINCT product_operation_id), "
                "count(DISTINCT night_episode_revision_id) "
                "FROM public.backend_reanalysis_links "
                "WHERE source_operation_id = ANY(%s)",
                (reanalysis_source_ids,),
            )
            assert cursor.fetchone() == (4, 4, 1)

            confirm_interaction = _operation_result(
                cursor, operation_ids["confirm_start"]
            )["interaction_id"]
            decline_interaction = _operation_result(
                cursor, operation_ids["decline_start"]
            )["interaction_id"]
            cursor.execute(
                "SELECT interaction_id, state, current_revision, cas_version "
                "FROM public.backend_product_interactions "
                "WHERE interaction_id = ANY(%s)",
                ([confirm_interaction, decline_interaction],),
            )
            interactions = {str(row[0]): tuple(row[1:]) for row in cursor.fetchall()}
            assert interactions == {
                confirm_interaction: ("confirmed", 4, 4),
                decline_interaction: ("declined", 3, 3),
            }
            cursor.execute(
                "SELECT interaction_id, array_agg(state ORDER BY revision_number), "
                "count(DISTINCT fact_snapshot_sha256) "
                "FROM public.backend_product_interaction_revisions "
                "WHERE interaction_id = ANY(%s) GROUP BY interaction_id",
                ([confirm_interaction, decline_interaction],),
            )
            revisions = {str(row[0]): (row[1], row[2]) for row in cursor.fetchall()}
            assert revisions == {
                confirm_interaction: (
                    ["waiting_user", "waiting_user", "awaiting_confirmation", "confirmed"],
                    1,
                ),
                decline_interaction: (
                    ["waiting_user", "awaiting_confirmation", "declined"],
                    1,
                ),
            }
            cursor.execute(
                "SELECT interaction_id, choice FROM public.backend_human_decisions_v2 "
                "WHERE interaction_id = ANY(%s)",
                ([confirm_interaction, decline_interaction],),
            )
            assert dict(cursor.fetchall()) == {
                confirm_interaction: "confirm",
                decline_interaction: "decline",
            }
            cursor.execute(
                "SELECT interaction_id, count(*) FROM public.backend_care_actions_v2 "
                "WHERE interaction_id = ANY(%s) GROUP BY interaction_id",
                ([confirm_interaction, decline_interaction],),
            )
            assert dict(cursor.fetchall()) == {confirm_interaction: 1}
            cursor.execute(
                "SELECT followup.state, followup.cas_version, "
                "count(receipt.*), array_agg(receipt.from_state || '->' || "
                "receipt.to_state ORDER BY receipt.occurred_at) FROM "
                "public.sleep_domain_care_followups AS followup JOIN "
                "public.sleep_domain_care_followup_transition_receipts AS receipt "
                "ON receipt.namespace_id = followup.namespace_id "
                "AND receipt.data_mode = followup.data_mode "
                "AND receipt.night_episode_id = followup.night_episode_id "
                "WHERE receipt.command_id = %s "
                "GROUP BY followup.state, followup.cas_version",
                (operation_ids["confirm"],),
            )
            assert cursor.fetchone() == (
                "pending_feedback",
                1,
                1,
                ["none->pending_feedback"],
            )
            cursor.execute(
                "SELECT count(*) FROM public.sleep_domain_domain_outbox "
                "WHERE operation_id = %s "
                "AND event_type = 'CARE_FOLLOWUP_PENDING' "
                "AND aggregate_type = 'CareFollowup'",
                (operation_ids["confirm"],),
            )
            assert cursor.fetchone() == (1,)
            cursor.execute(
                "SELECT status, attempt_count, lease_generation, fencing_token, "
                "worker_instance FROM public.backend_delivery_intents "
                "WHERE aggregate_id = (SELECT care_action_id "
                "FROM public.backend_care_actions_v2 WHERE interaction_id = %s)",
                (confirm_interaction,),
            )
            assert cursor.fetchone() == ("pending", 0, 0, None, None)

            cursor.execute(
                "SELECT target_resource_id, status, count(*) "
                "FROM public.backend_pending_handles "
                "WHERE target_resource_id = ANY(%s) "
                "GROUP BY target_resource_id, status",
                ([confirm_interaction, decline_interaction],),
            )
            handles = {
                (str(row[0]), str(row[1])): int(row[2])
                for row in cursor.fetchall()
            }
            assert handles == {
                (confirm_interaction, "consumed"): 2,
                (confirm_interaction, "revoked"): 1,
                (decline_interaction, "consumed"): 2,
            }

            model_operation_ids = [
                operation_ids["confirm_start"],
                operation_ids["confirm_ask"],
                operation_ids["confirm_answer"],
                operation_ids["decline_start"],
                operation_ids["decline_answer"],
            ]
            cursor.execute(
                "SELECT count(*), count(*) FILTER ("
                "WHERE current_state = 'response_received') "
                "FROM public.backend_invocations "
                "WHERE operation_id = ANY(%s) AND invocation_kind = 'model'",
                (model_operation_ids,),
            )
            assert cursor.fetchone() == (5, 5)


def _operation_result(cursor, operation_id: str) -> dict:
    cursor.execute(
        "SELECT operation_json -> 'result' FROM public.sleep_domain_operations "
        "WHERE operation_id = %s",
        (operation_id,),
    )
    row = cursor.fetchone()
    assert row is not None
    value = row[0]
    return json.loads(value) if isinstance(value, str) else dict(value)
