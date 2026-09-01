from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from sleepagent.application.personalization_governance import (
    CARE_OUTCOME_MEMORY_CONCEPT_ID,
    build_outcome_personalization_governance,
)
from sleepagent.persistence.migrations import (
    EXPECTED_MIGRATION_IDENTITIES,
    LATEST_SCHEMA_VERSION,
)
from sleepagent.runtime.contracts import MemoryChangeCandidate
from sleepagent.runtime.memory import (
    MemoryItemStatus,
    ProvenanceType,
    select_memory_slice,
)
from sleepagent.workers.product import EVIDENCE_MEMORY_CONCEPT_IDS
from tests.unit.test_g10_care_outcomes import (
    COMPLETED,
    _execution,
    _wake_evidence,
)
from sleepagent.domain.care_outcomes import evaluate_care_outcome
from tests.unit.test_memory_capability import NOW, _item, _query, _state


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / (
    "sleepagent/persistence/migrations/"
    "025_outcome_personalization_governance.sql"
)


def _governance():  # type: ignore[no-untyped-def]
    decision = evaluate_care_outcome(
        _execution(),
        _wake_evidence((360, 420), (390, 400)),
        evaluated_at=COMPLETED + timedelta(days=3),
    )
    assert decision.outcome is not None
    assert decision.personalization_receipt is not None
    governance = build_outcome_personalization_governance(
        decision.outcome,
        decision.personalization_receipt,
    )
    assert governance is not None
    return governance


def test_receipt_candidate_adapts_to_exact_noncausal_governance_handle() -> None:
    governance = _governance()
    assert governance.causal_claim is False
    assert governance.confirmation_required is True
    assert governance.memory_candidate.concept_id == CARE_OUTCOME_MEMORY_CONCEPT_ID
    assert governance.memory_candidate.typed_value == "improved"
    assert governance.memory_candidate.provenance_type == "accepted_evidence"
    assert governance.memory_candidate.source_ref == (
        f"evidence:{governance.care_outcome_id}"
    )
    assert governance.memory_candidate.candidate_hash == (
        governance.candidate_semantic_hash
    )
    assert governance.baseline_revision_ids
    assert governance.followup_revision_ids


def test_unknown_outcome_concept_fails_closed() -> None:
    governance = _governance()
    unsupported = MemoryChangeCandidate.model_validate(
        governance.memory_candidate.model_dump(
            mode="python", exclude={"candidate_hash"}
        )
        | {"concept_id": "care_outcome.unsupported"}
    )
    with pytest.raises(ValueError, match="unsupported"):
        governance.model_copy(
            update={
                "memory_candidate": unsupported,
                "candidate_semantic_hash": unsupported.candidate_hash,
            }
        ).validate_exact_candidate()


def test_outcome_memory_is_one_exact_evidence_consumer_not_broad_retrieval() -> None:
    assert EVIDENCE_MEMORY_CONCEPT_IDS == (
        "sleep.context.night_routine",
        "sleep.context.environment",
        CARE_OUTCOME_MEMORY_CONCEPT_ID,
    )
    worker = (ROOT / "sleepagent/workers/product.py").read_text(encoding="utf-8")
    assert "backend_personalization_effect_receipts_v1" not in worker
    for forbidden in ("embedding", "vector db", "semantic similarity"):
        assert forbidden not in worker.lower()


def test_c3_is_manifest_pinned_additive_migration_025() -> None:
    assert LATEST_SCHEMA_VERSION >= 25
    assert any(
        identity.startswith("025:025_outcome_personalization_governance:")
        for identity in EXPECTED_MIGRATION_IDENTITIES
    )
    assert MIGRATION.is_file()


def test_c3_schema_is_forced_rls_terminal_and_keeps_memory_authority_existing() -> None:
    body = MIGRATION.read_text(encoding="utf-8")
    for table in (
        "backend_personalization_governance_v1",
        "backend_personalization_governance_decisions_v1",
    ):
        assert f"ALTER TABLE public.{table}" in body
        assert "ENABLE ROW LEVEL SECURITY" in body
        assert "FORCE ROW LEVEL SECURITY" in body
        assert f"REVOKE ALL ON TABLE public.{table}" in body
    assert "invalid personalization governance transition" in body
    assert "backend_personalization_decision_append_only_v1_trigger" in body
    assert "candidate_target_sha256" in body
    assert "authorization_epoch" in body
    assert "actor_binding_id" in body
    assert "INSERT INTO public.backend_governed_memory_revisions_v2" not in body
    assert "INSERT INTO public.backend_habit_profile_revisions_v2" not in body


def test_c3_metrics_are_aggregate_only_and_protected() -> None:
    body = MIGRATION.read_text(encoding="utf-8")
    for signal in (
        "pending_personalization_governance",
        "oldest_pending_candidate_age_seconds",
        "accepted_personalization_candidates",
        "rejected_personalization_candidates",
        "superseded_personalization_candidates",
        "decision_conflict_or_error_count",
    ):
        assert signal in body
    function = body.split(
        "sleepagent_personalization_governance_metrics_v1()", 1
    )[1]
    assert "'subject_id'" not in function
    assert "SECURITY DEFINER" in function
    assert "SET search_path = pg_catalog, public" in function


@pytest.mark.parametrize(
    ("terminal_status", "expected_refs"),
    [
        (None, ("memory:care-outcome:v2",)),
        (MemoryItemStatus.EXPIRED, ()),
        (MemoryItemStatus.FORGOTTEN, ()),
    ],
)
def test_outcome_memory_uses_existing_supersession_and_terminal_lifecycle(
    terminal_status: MemoryItemStatus | None,
    expected_refs: tuple[str, ...],
) -> None:
    common = {
        "memory_id": "memory:care-outcome",
        "concept_id": CARE_OUTCOME_MEMORY_CONCEPT_ID,
        "value_schema_id": "enum.v1",
        "provenance_type": ProvenanceType.ACCEPTED_EVIDENCE,
        "source_ref": "evidence:care-outcome-1",
        "source_scope_kind": "historical_range",
    }
    first = _item(**common, typed_value="improved")
    second = _item(
        **common,
        typed_value="stable" if terminal_status is None else None,
        version=2,
        recorded_at=NOW - timedelta(days=1),
        valid_from=NOW - timedelta(days=1),
        supersedes_ref=first.revision_ref,
        status=terminal_status or MemoryItemStatus.ACTIVE,
        confirmation_ref="confirmation:outcome:2",
    )
    receipt = select_memory_slice(
        _query(CARE_OUTCOME_MEMORY_CONCEPT_ID, scope="historical_range"),
        _state(first, second, version=2),
        now=NOW,
    )
    assert tuple(item.revision_ref for item in receipt.items) == expected_refs
