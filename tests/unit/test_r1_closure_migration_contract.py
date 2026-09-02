from __future__ import annotations

from pathlib import Path

import pytest

from sleepagent.persistence.migrations import (
    EXPECTED_MIGRATION_IDENTITIES,
    LATEST_SCHEMA_VERSION,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
RUNTIME_MIGRATION = ROOT / (
    "sleepagent/persistence/migrations/026_runtime_reliability_closure.sql"
)
CARE_MIGRATION = ROOT / (
    "sleepagent/persistence/migrations/027_care_operational_access_closure.sql"
)
WORKER_AUTHORITY_MIGRATION = ROOT / (
    "sleepagent/persistence/migrations/028_worker_authority_boundaries.sql"
)


def test_r1_is_manifest_pinned_as_two_additive_migrations() -> None:
    assert LATEST_SCHEMA_VERSION >= 27
    assert any(
        identity.startswith("026:026_runtime_reliability_closure:")
        for identity in EXPECTED_MIGRATION_IDENTITIES
    )
    assert any(
        identity.startswith("027:027_care_operational_access_closure:")
        for identity in EXPECTED_MIGRATION_IDENTITIES
    )
    assert RUNTIME_MIGRATION.is_file()
    assert CARE_MIGRATION.is_file()


def test_p0b_worker_authority_is_manifest_pinned_as_additive_028() -> None:
    assert LATEST_SCHEMA_VERSION == 28
    assert any(
        identity.startswith("028:028_worker_authority_boundaries:")
        for identity in EXPECTED_MIGRATION_IDENTITIES
    )
    body = WORKER_AUTHORITY_MIGRATION.read_text(encoding="utf-8")
    for claim_name in (
        "sleepagent_claim_normalization_work",
        "sleepagent_claim_operation",
        "sleepagent_claim_delivery",
        "sleepagent_claim_retention_job",
    ):
        assert claim_name in body
    assert "sleepagent_exhaust_authorized_reclaims_v2" in body
    assert "sleepagent_worker_claim_authority_v2" in body
    assert "LIMIT requested_limit" in body
    assert body.count("FOR UPDATE") >= 5
    assert "sleepagent_ensure_delivery_reconciliation_v2" in body
    assert (
        "ALTER TABLE public.sleep_domain_provider_accounts "
        "FORCE ROW LEVEL SECURITY"
    ) in body
    assert "ALTER TABLE public.sleep_domain_quarantine FORCE ROW LEVEL SECURITY" in body


def test_r1_reclaim_budget_is_independent_and_covers_all_claim_kinds() -> None:
    body = RUNTIME_MIGRATION.read_text(encoding="utf-8")
    for table in (
        "sleep_domain_normalization_work",
        "sleep_domain_operations",
        "backend_delivery_intents",
        "backend_retention_jobs",
    ):
        assert f"ALTER TABLE public.{table}" in body
    assert body.count("ADD COLUMN execution_reclaim_count") == 4
    assert body.count("ADD COLUMN max_execution_reclaims") == 4
    for kind in ("normalization", "operation", "delivery", "retention"):
        assert f"requested_kind = '{kind}'" in body
        assert f"'''{kind}''," in body
    assert "outcome_unknown" in body
    assert "execution_reclaim_exhausted" in body
    assert "COALESCE(sum(reclaim_count), 0)" in body


def test_r1_schedule_constraint_and_forward_progress_are_database_enforced() -> None:
    body = RUNTIME_MIGRATION.read_text(encoding="utf-8")
    assert "backend_acquisition_schedule_jitter_lt_cadence" in body
    assert "jitter_seconds < cadence_seconds" in body
    assert "next_run_at = GREATEST(" in body
    assert "clock_timestamp() + make_interval" in body
    assert "candidate.cadence_seconds + jitter_offset" in body


def test_r1_internal_status_has_no_care_base_table_policy_bypass() -> None:
    body = CARE_MIGRATION.read_text(encoding="utf-8")
    policy_section, aggregate_section = body.split(
        "CREATE OR REPLACE FUNCTION "
        "public.sleepagent_internal_status_principal_allows_v1()",
        1,
    )
    assert "internal_status" not in policy_section
    for table in (
        "backend_care_action_proposals_v3",
        "backend_care_action_decisions_v3",
        "backend_approval_grants_v3",
        "backend_care_plans_v1",
        "backend_care_execution_states_v1",
        "backend_care_execution_events_v1",
        "backend_care_outcome_evaluations_v1",
        "backend_care_outcomes_v1",
        "backend_personalization_effect_receipts_v1",
        "backend_personalization_governance_v1",
    ):
        assert f"ON public.{table}" in policy_section
    assert "principal_kind = 'bff'" in aggregate_section
    assert "principal.database_role_name::text = session_user::text" in (
        aggregate_section
    )
    assert "REVOKE ALL ON FUNCTION" in aggregate_section
    assert aggregate_section.count(
        "sleepagent_internal_status_principal_allows_v1"
    ) >= 2
