from __future__ import annotations

import json
import os

import pytest


pytestmark = pytest.mark.postgres


def test_committed_first_slice_has_one_exact_causal_product_chain() -> None:
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ.get("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN", "").strip()
    root_operation_id = os.environ.get(
        "SLEEPAGENT_FIRST_SLICE_ROOT_OPERATION_ID", ""
    ).strip()
    if not dsn or not root_operation_id:
        pytest.skip("a completed process proof database and root operation are required")

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT journey_id, namespace_id, namespace_generation, run_id, "
                "arm_id, subject_id, phase, result_json FROM "
                "public.backend_demo_journeys WHERE root_operation_id = %s",
                (root_operation_id,),
            )
            journey = cursor.fetchone()
            assert journey is not None
            assert str(journey[6]) == "succeeded"
            result = (
                json.loads(journey[7])
                if isinstance(journey[7], str)
                else dict(journey[7])
            )
            scope = (
                str(journey[1]),
                int(journey[2]),
                str(journey[3]),
                str(journey[4]),
                str(journey[5]),
            )

            cursor.execute(
                "SELECT status, operation_json #>> '{result,analysis_revision_id}' "
                "FROM public.sleep_domain_operations WHERE operation_id = %s",
                (root_operation_id,),
            )
            root = cursor.fetchone()
            assert root == ("succeeded", result["analysis_revision_id"])
            cursor.execute(
                "SELECT count(*) FROM public.backend_demo_journey_receipts "
                "WHERE root_operation_id = %s AND outcome = 'succeeded'",
                (root_operation_id,),
            )
            assert cursor.fetchone() == (1,)

            cursor.execute(
                "SELECT count(*), count(*) FILTER (WHERE status = 'succeeded') "
                "FROM public.sleep_domain_normalization_work WHERE "
                "namespace_id = %s AND namespace_generation = %s "
                "AND run_id = %s AND arm_id = %s AND subject_id = %s",
                scope,
            )
            assert cursor.fetchone() == (497, 497)
            cursor.execute(
                "SELECT count(*) FROM public.sleep_domain_canonical_observations "
                "WHERE namespace_id = %s AND data_mode = 'replay' "
                "AND subject_id = %s",
                (scope[0], scope[4]),
            )
            assert cursor.fetchone() == (497,)

            cursor.execute(
                "SELECT count(*) FROM public.sleep_domain_night_episodes "
                "WHERE namespace_id = %s AND data_mode = 'replay' "
                "AND namespace_generation = %s AND run_id = %s AND arm_id = %s "
                "AND subject_id = %s AND current_revision_id = %s "
                "AND night_episode_id = %s AND date_state = 'finalized' "
                "AND date_conflict = FALSE AND assignment_basis = 'observed_wake'",
                (*scope, result["night_episode_revision_id"], result["night_episode_id"]),
            )
            assert cursor.fetchone() == (1,)
            cursor.execute(
                "SELECT episode.current_revision_number, "
                "revision.revision_number, "
                "jsonb_array_length(revision.revision_json -> "
                "'observation_ids'), "
                "(SELECT count(*) FROM "
                "public.sleep_domain_episode_observation_memberships AS member "
                "WHERE member.namespace_id = episode.namespace_id "
                "AND member.data_mode = episode.data_mode "
                "AND member.night_episode_id = episode.night_episode_id), "
                "(SELECT count(DISTINCT member.observation_id) FROM "
                "public.sleep_domain_episode_observation_memberships AS member "
                "WHERE member.namespace_id = episode.namespace_id "
                "AND member.data_mode = episode.data_mode "
                "AND member.night_episode_id = episode.night_episode_id) "
                "FROM public.sleep_domain_night_episodes AS episode "
                "JOIN public.sleep_domain_night_episode_revisions AS revision "
                "ON revision.night_episode_revision_id = "
                "episode.current_revision_id "
                "WHERE episode.night_episode_id = %s",
                (result["night_episode_id"],),
            )
            assert cursor.fetchone() == (494, 494, 494, 494, 494)
            cursor.execute(
                "SELECT count(*) FROM "
                "public.sleep_domain_night_episode_revisions "
                "WHERE namespace_id = %s AND data_mode = 'replay' "
                "AND night_episode_id = %s",
                (scope[0], result["night_episode_id"]),
            )
            assert cursor.fetchone() == (494,)
            cursor.execute(
                "SELECT (SELECT array_agg(member.observation_id ORDER BY "
                "member.observation_id) FROM "
                "public.sleep_domain_episode_observation_memberships AS member "
                "WHERE member.namespace_id = revision.namespace_id "
                "AND member.data_mode = revision.data_mode "
                "AND member.night_episode_id = revision.night_episode_id) = "
                "(SELECT array_agg(value ORDER BY value) FROM "
                "jsonb_array_elements_text(revision.revision_json -> "
                "'observation_ids') AS ids(value)) FROM "
                "public.sleep_domain_night_episode_revisions AS revision "
                "WHERE revision.night_episode_revision_id = %s",
                (result["night_episode_revision_id"],),
            )
            assert cursor.fetchone() == (True,)

            cursor.execute(
                "SELECT count(*) FROM public.sleep_domain_analysis_revisions "
                "WHERE analysis_revision_id = %s AND namespace_id = %s "
                "AND data_mode = 'replay' AND night_episode_revision_id = %s",
                (
                    result["analysis_revision_id"],
                    scope[0],
                    result["night_episode_revision_id"],
                ),
            )
            assert cursor.fetchone() == (1,)
            cursor.execute(
                "SELECT count(*), array_agg(role ORDER BY role) FROM "
                "public.sleep_domain_analysis_role_views WHERE namespace_id = %s "
                "AND data_mode = 'replay' AND analysis_revision_id = %s "
                "AND status = 'ready'",
                (scope[0], result["analysis_revision_id"]),
            )
            assert cursor.fetchone() == (3, ["doctor", "elder", "family"])
            cursor.execute(
                "SELECT count(*) FROM public.sleep_domain_operations WHERE "
                "namespace_id = %s AND data_mode = 'replay' "
                "AND namespace_generation = %s AND run_id = %s AND arm_id = %s "
                "AND subject_id = %s AND operation_type = 'product_agent' "
                "AND target_resource_key = %s AND operation_id = %s "
                "AND status = 'succeeded' "
                "AND operation_json #>> '{result,analysis_revision_id}' = %s",
                (
                    *scope,
                    result["night_episode_revision_id"],
                    result["product_operation_id"],
                    result["analysis_revision_id"],
                ),
            )
            assert cursor.fetchone() == (1,)
