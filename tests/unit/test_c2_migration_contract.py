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
    "024_care_evaluation_operational_metrics.sql"
)


def test_c2_is_manifest_pinned_additive_migration_024() -> None:
    assert LATEST_SCHEMA_VERSION >= 24
    assert any(
        identity.startswith("024:024_care_evaluation_operational_metrics:")
        for identity in EXPECTED_MIGRATION_IDENTITIES
    )
    assert MIGRATION.is_file()


def test_c2_operational_metrics_are_aggregate_only_and_protected() -> None:
    body = MIGRATION.read_text(encoding="utf-8")
    for signal in (
        "pending_care_evaluations",
        "oldest_pending_care_evaluation_age_seconds",
        "care_evaluations_succeeded",
        "care_evaluations_deduplicated",
        "care_evaluations_failed_or_conflicted",
    ):
        assert signal in body
    returned = body.split("SELECT jsonb_build_object(", 1)[1]
    for forbidden in (
        "'subject_id'",
        "'operation_id'",
        "'night_episode_id'",
        "'analysis_revision_id'",
        "'night_finalization_revision_id'",
    ):
        assert forbidden not in returned
    assert "'internal_status'" in body
    assert "REVOKE ALL ON FUNCTION" in body


def test_c2_metrics_function_is_in_api_role_bootstrap() -> None:
    body = (ROOT / "sleepagent/persistence/migrate.py").read_text(
        encoding="utf-8"
    )
    assert '"sleepagent_care_evaluation_operational_metrics_v1()"' in body
