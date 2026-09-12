from __future__ import annotations

from pathlib import Path
import re

import pytest

from sleepagent.persistence.migrations import (
    EXPECTED_MIGRATION_IDENTITIES,
    LATEST_SCHEMA_VERSION,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "sleepagent/persistence/migrations/022_care_outcome_feedback.sql"


def test_g10_is_one_manifest_pinned_additive_migration() -> None:
    assert LATEST_SCHEMA_VERSION >= 22
    assert any(
        identity.startswith("022:022_care_outcome_feedback:")
        for identity in EXPECTED_MIGRATION_IDENTITIES
    )
    assert MIGRATION.is_file()


def test_g10_schema_is_subject_scoped_forced_rls_and_append_only() -> None:
    body = MIGRATION.read_text(encoding="utf-8")
    for table in (
        "backend_care_outcome_evaluations_v1",
        "backend_care_outcomes_v1",
        "backend_personalization_effect_receipts_v1",
    ):
        assert re.search(
            rf"ALTER TABLE public\.{table}\s+ENABLE ROW LEVEL SECURITY", body
        )
        assert re.search(
            rf"ALTER TABLE public\.{table}\s+FORCE ROW LEVEL SECURITY", body
        )
        assert f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC" in body
    assert "backend_care_outcome_append_only_v1_trigger" in body
    assert "backend_personalization_effect_append_only_v1_trigger" in body
    assert "supersedes_receipt_id TEXT" in body
    assert "direct_memory_write BOOLEAN NOT NULL DEFAULT FALSE" in body
    assert "CHECK (\n    NOT direct_memory_write" in body


def test_g10_uses_durable_operations_and_hard_finalization_trigger() -> None:
    body = MIGRATION.read_text(encoding="utf-8")
    assert "'care.outcome.evaluate.v1'" in body
    assert "INSERT INTO public.sleep_domain_operations" in body
    assert "sleep_domain_finalization_trigger_care_outcome_v1" in body
    assert "NEW.state <> 'hard_finalized' OR NEW.provisional" in body
    assert "ON CONFLICT DO NOTHING" in body
    assert "source_finalization_revision_id = 'window_expiry'" in body
    assert "THEN evaluation.observation_window_end" in body
    assert "WITH eligible_completion AS" in body
    assert "ON CONFLICT (care_plan_id) DO NOTHING" in body
    assert "execution_authority = 'human_attested'" in body
    assert "causal_claim BOOLEAN NOT NULL DEFAULT FALSE CHECK (NOT causal_claim)" in body


def test_g10_does_not_write_confirmed_habit_or_memory_or_external_effects() -> None:
    body = MIGRATION.read_text(encoding="utf-8")
    for forbidden_insert in (
        "INSERT INTO public.backend_habit_profile_revisions_v2",
        "INSERT INTO public.backend_governed_memory_revisions_v2",
        "INSERT INTO public.backend_delivery_intents",
    ):
        assert forbidden_insert not in body
    for external in ("smtp", "sms", "email_address", "device_control"):
        assert external not in body.lower()


def test_g10_operational_metrics_are_aggregate_only() -> None:
    body = MIGRATION.read_text(encoding="utf-8")
    for metric in (
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
    ):
        assert metric in body
    function = body.split(
        "CREATE OR REPLACE FUNCTION public.sleepagent_care_outcome_operational_metrics_v1()",
        1,
    )[1]
    function = function.split("REVOKE ALL ON TABLE", 1)[0]
    assert "jsonb_build_object" in function
    assert "subject_id'" not in function
