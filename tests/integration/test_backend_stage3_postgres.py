from __future__ import annotations

import json
import os

import pytest


pytestmark = pytest.mark.postgres


def test_stage3_process_proof_has_exact_durable_effects() -> None:
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ.get("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN", "").strip()
    raw_proof = os.environ.get("SLEEPAGENT_STAGE3_PROOF_JSON", "").strip()
    if not dsn or not raw_proof:
        pytest.skip("a completed Stage-3 process proof database is required")
    proof = json.loads(raw_proof)
    assert isinstance(proof, dict)
    assert proof.get("proof_kind") in {"success", "quality", "urgent"}
    root_operation_id = _required_text(proof, "operation_id")
    subject_id = _required_text(proof, "subject_id")

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT namespace_id, namespace_generation, run_id, arm_id, "
                "subject_id, phase, error_code FROM public.backend_demo_journeys "
                "WHERE root_operation_id = %s",
                (root_operation_id,),
            )
            journey = cursor.fetchone()
            assert journey is not None
            assert str(journey[4]) == subject_id
            scope = tuple(journey[:5])
            if proof["proof_kind"] == "success":
                _assert_success(cursor, proof=proof, scope=scope)
            else:
                _assert_abnormal(cursor, proof=proof, scope=scope, journey=journey)


def _assert_success(cursor, *, proof: dict[str, object], scope: tuple[object, ...]) -> None:
    namespace_id, generation, run_id, arm_id, subject_id = scope
    expected_nights = int(proof["expected_night_count"])
    assert expected_nights >= 1
    assert proof.get("stage3_schema_version") == "backend_stage3_verification.v1"

    cursor.execute(
        "SELECT count(*) FROM public.sleep_domain_night_episodes "
        "WHERE namespace_id = %s AND data_mode = 'replay' "
        "AND namespace_generation = %s AND run_id = %s AND arm_id = %s "
        "AND subject_id = %s AND protocol_version >= 2 "
        "AND date_state = 'finalized' AND date_conflict = FALSE "
        "AND current_revision_id IS NOT NULL",
        (namespace_id, generation, run_id, arm_id, subject_id),
    )
    assert cursor.fetchone() == (expected_nights,)
    cursor.execute(
        "SELECT count(*) FROM public.sleep_domain_operations "
        "WHERE namespace_id = %s AND data_mode = 'replay' "
        "AND namespace_generation = %s AND run_id = %s AND arm_id = %s "
        "AND subject_id = %s AND operation_type = 'product_agent' "
        "AND status = 'succeeded'",
        (namespace_id, generation, run_id, arm_id, subject_id),
    )
    assert cursor.fetchone() == (expected_nights,)
    cursor.execute(
        "SELECT role, count(*) FROM public.sleep_domain_analysis_role_views "
        "WHERE namespace_id = %s AND data_mode = 'replay' "
        "AND namespace_generation = %s AND run_id = %s AND arm_id = %s "
        "AND subject_id = %s AND protocol_version >= 2 "
        "GROUP BY role ORDER BY role",
        (namespace_id, generation, run_id, arm_id, subject_id),
    )
    assert dict(cursor.fetchall()) == {
        "doctor": expected_nights,
        "elder": expected_nights,
        "family": expected_nights,
    }

    advance_operation_id = proof.get("advance_operation_id")
    if advance_operation_id is None:
        assert expected_nights == 1
        return
    assert isinstance(advance_operation_id, str) and advance_operation_id
    cursor.execute(
        "SELECT receipt.to_scenario_time, receipt.to_clock_version, "
        "receipt.released_fact_count, operation.status, "
        "operation.lease_owner, operation.lease_expires_at, "
        "clock.scenario_now, clock.clock_version, "
        "clock.last_command_operation_id "
        "FROM public.backend_demo_advance_receipts AS receipt "
        "JOIN public.sleep_domain_operations AS operation "
        "ON operation.operation_id = receipt.operation_id "
        "JOIN public.backend_replay_scenario_clocks AS clock "
        "ON clock.namespace_id = receipt.namespace_id "
        "AND clock.data_mode = receipt.data_mode "
        "AND clock.namespace_generation = receipt.namespace_generation "
        "AND clock.run_id = receipt.run_id AND clock.arm_id = receipt.arm_id "
        "WHERE receipt.operation_id = %s",
        (advance_operation_id,),
    )
    advance = cursor.fetchone()
    assert advance is not None
    assert advance[0] == advance[6]
    assert int(advance[1]) == int(advance[7])
    assert int(advance[2]) > 0
    assert tuple(advance[3:6]) == ("succeeded", None, None)
    assert str(advance[8]) == advance_operation_id
    cursor.execute(
        "SELECT count(*) FILTER (WHERE status = 'staged'), "
        "count(*) FILTER (WHERE status = 'released') "
        "FROM public.backend_replay_staged_facts "
        "WHERE namespace_id = %s AND data_mode = 'replay' "
        "AND namespace_generation = %s AND run_id = %s AND arm_id = %s "
        "AND subject_id = %s",
        (namespace_id, generation, run_id, arm_id, subject_id),
    )
    staged, released = cursor.fetchone()
    assert int(staged) == 0
    assert int(released) == int(advance[2])


def _assert_abnormal(
    cursor,
    *,
    proof: dict[str, object],
    scope: tuple[object, ...],
    journey: tuple[object, ...],
) -> None:
    namespace_id, generation, run_id, arm_id, subject_id = scope
    expected_error = {
        "quality": "quality_insufficient",
        "urgent": "unexpected_urgent_route",
    }[str(proof["proof_kind"])]
    assert journey[5:] == ("failed", expected_error)
    assert proof.get("error_code") == expected_error
    cursor.execute(
        "SELECT count(*) FROM public.sleep_domain_operations "
        "WHERE namespace_id = %s AND data_mode = 'replay' "
        "AND namespace_generation = %s AND run_id = %s AND arm_id = %s "
        "AND subject_id = %s AND operation_type = 'product_agent'",
        (namespace_id, generation, run_id, arm_id, subject_id),
    )
    assert cursor.fetchone() == (0,)
    cursor.execute(
        "SELECT count(*) FROM public.sleep_domain_analysis_role_views "
        "WHERE namespace_id = %s AND data_mode = 'replay' "
        "AND namespace_generation = %s AND run_id = %s AND arm_id = %s "
        "AND subject_id = %s",
        (namespace_id, generation, run_id, arm_id, subject_id),
    )
    assert cursor.fetchone() == (0,)
    cursor.execute(
        "SELECT count(*) FROM public.backend_invocations "
        "WHERE namespace_id = %s AND data_mode = 'replay' "
        "AND namespace_generation = %s AND run_id = %s AND arm_id = %s "
        "AND subject_id = %s AND invocation_kind = 'model'",
        (namespace_id, generation, run_id, arm_id, subject_id),
    )
    assert cursor.fetchone() == (0,)


def _required_text(value: dict[str, object], name: str) -> str:
    item = value.get(name)
    assert isinstance(item, str) and item
    return item
