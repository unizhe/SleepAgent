from __future__ import annotations

from pathlib import Path

import pytest

from sleepagent.persistence.migrations import (
    EXPECTED_MIGRATION_IDENTITIES,
    LATEST_SCHEMA_VERSION,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / (
    "sleepagent/persistence/migrations/"
    "023_late_episode_association_authority.sql"
)


def test_c1b_is_manifest_pinned_additive_migration_023() -> None:
    assert LATEST_SCHEMA_VERSION == 23
    assert EXPECTED_MIGRATION_IDENTITIES[-1].startswith(
        "023:023_late_episode_association_authority:"
    )
    assert MIGRATION.is_file()


def test_c1b_association_evidence_is_generation_scoped_and_forced_rls() -> None:
    body = MIGRATION.read_text(encoding="utf-8")
    assert "ADD COLUMN namespace_generation BIGINT" in body
    assert "ADD COLUMN run_id TEXT" in body
    assert "ADD COLUMN arm_id TEXT" in body
    assert "sleep_domain_pending_association_generation_required_v2" in body
    assert "sleep_domain_pending_association_generation_fk_v2" in body
    assert "sleep_domain_pending_association_arm_fk_v2" in body
    assert "ENABLE ROW LEVEL SECURITY" in body
    assert "FORCE ROW LEVEL SECURITY" in body
    assert "sleepagent_subject_generation_scope_allows" in body
    assert "idx_sleep_domain_membership_binding_episode_v2" in body


def test_c1b_migration_does_not_rewrite_earlier_migrations() -> None:
    assert "ALTER TABLE public.sleep_domain_pending_episode_associations" in (
        MIGRATION.read_text(encoding="utf-8")
    )
