from __future__ import annotations

import json
import os

import pytest


pytestmark = pytest.mark.postgres


def test_stage5_process_proof_has_v2_keys_shred_and_reset_receipt() -> None:
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ.get("SLEEPAGENT_TEST_POSTGRES_ADMIN_DSN", "").strip()
    raw_proof = os.environ.get("SLEEPAGENT_STAGE5_PROOF_JSON", "").strip()
    if not dsn or not raw_proof:
        pytest.skip("a completed Stage-5 process proof database is required")
    proof = json.loads(raw_proof)
    assert proof["schema_version"] == "backend_stage5_verification.v1"
    assert proof["old_assertion_fenced"] is True
    assert proof["old_cursor_fenced"] is True
    assert proof["new_generation_today_state"] == "no_data"
    source_generation = int(proof["source_generation"])
    target_generation = int(proof["target_generation"])
    assert target_generation == source_generation + 1

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT namespace_id, subject_id, required_dek_count, status "
                "FROM public.backend_demo_resets_v2 "
                "WHERE operation_id = %s",
                (proof["reset_operation_id"],),
            )
            reset = cursor.fetchone()
            assert reset is not None
            namespace_id, subject_id, required_dek_count, status = reset
            assert status == "succeeded"

            cursor.execute(
                "SELECT count(*), count(*) FILTER (WHERE "
                "encryption_protocol_version >= 2), "
                "count(*) FILTER (WHERE retention_domain = 'raw' "
                "AND dek_generation IS NOT NULL) "
                "FROM public.sleep_domain_raw_inbox "
                "WHERE namespace_id = %s AND data_mode = 'replay' "
                "AND namespace_generation = %s AND subject_id = %s",
                (namespace_id, source_generation, subject_id),
            )
            raw_count, v2_count, key_ref_count = cursor.fetchone()
            assert raw_count > 0
            assert (raw_count, v2_count, key_ref_count) == (
                raw_count,
                raw_count,
                raw_count,
            )
            cursor.execute(
                "SELECT count(*) FROM public.backend_retention_bindings "
                "WHERE namespace_id = %s AND data_mode = 'replay' "
                "AND namespace_generation = %s AND subject_id = %s "
                "AND retention_domain = 'raw'",
                (namespace_id, source_generation, subject_id),
            )
            assert cursor.fetchone() == (raw_count,)
            cursor.execute(
                "SELECT count(*), count(*) FILTER (WHERE status = 'shredded' "
                "AND wrapped_dek IS NULL) "
                "FROM public.backend_retention_deks "
                "WHERE namespace_id = %s AND data_mode = 'replay' "
                "AND namespace_generation = %s AND subject_id = %s",
                (namespace_id, source_generation, subject_id),
            )
            key_count, shredded_count = cursor.fetchone()
            assert key_count > 0
            assert shredded_count == key_count
            cursor.execute(
                "SELECT count(*) FROM public.backend_demo_reset_key_receipts_v2 "
                "WHERE reset_id = (SELECT reset_id "
                "FROM public.backend_demo_resets_v2 WHERE operation_id = %s)",
                (proof["reset_operation_id"],),
            )
            assert cursor.fetchone() == (required_dek_count,)
            cursor.execute(
                "SELECT receipt_json FROM public.backend_demo_reset_receipts_v2 "
                "WHERE operation_id = %s",
                (proof["reset_operation_id"],),
            )
            receipt = cursor.fetchone()
            assert receipt is not None
            receipt_json = receipt[0]
            assert receipt_json["target_generation"] == target_generation
            assert receipt_json["domain_outcomes"]
            encoded = json.dumps(receipt_json, sort_keys=True)
            for forbidden in (
                "subject_id",
                "raw_ingress_record_id",
                "encrypted_payload",
                "plaintext",
            ):
                assert forbidden not in encoded
            cursor.execute(
                "SELECT current_generation FROM public.backend_namespaces "
                "WHERE namespace_id = %s AND data_mode = 'replay'",
                (namespace_id,),
            )
            assert cursor.fetchone() == (target_generation,)
            cursor.execute(
                "SELECT count(*) FROM public.sleep_domain_analysis_role_views "
                "WHERE namespace_id = %s AND data_mode = 'replay' "
                "AND namespace_generation = %s AND subject_id = %s",
                (namespace_id, target_generation, subject_id),
            )
            assert cursor.fetchone() == (0,)

