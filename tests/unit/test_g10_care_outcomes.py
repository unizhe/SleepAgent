from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sleepagent.domain.care_actions import CareActionType
from sleepagent.domain.care_outcomes import (
    CAUSAL_DISCLAIMER_ZH_CN,
    CareExecutionEvidence,
    EvaluationMode,
    EvidenceQuality,
    OutcomeCategory,
    OutcomeLifecycleState,
    OutcomeMetricFact,
    OutcomeNightEvidence,
    PersonalizationCandidateType,
    PersonalizationReceiptState,
    SLEEP_WINDOW_METRIC,
    WAKE_TIME_METRIC,
    evaluate_care_outcome,
    evaluation_eligible,
    outcome_policy_for,
)
from sleepagent.runtime.contracts import MemoryChangeCandidate, SourceScopeKind
from sleepagent.runtime.memory import (
    GovernedMemoryState,
    MemoryChange,
    MemoryConfirmation,
    MemoryOperation,
    ProvenanceType,
    apply_memory_change,
    project_current_memory,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc
COMPLETED = datetime(2026, 8, 10, 8, tzinfo=UTC)


def _execution(
    action: CareActionType = CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME,
    **changes: object,
) -> CareExecutionEvidence:
    values: dict[str, object] = {
        "care_plan_id": "care-plan:1",
        "care_execution_event_id": "care-execution-event:1",
        "subject_id": "subject-a",
        "action_type": action,
        "state": "completed",
        "completed_at": COMPLETED,
        "execution_authority": "human_attested",
        "source_analysis_revision_id": "analysis-1",
        "authority_valid_at_completion": True,
        "plan_invalidated_before_completion": False,
    }
    values.update(changes)
    return CareExecutionEvidence.model_validate(values)


def _metric(
    value: float,
    *,
    definition=WAKE_TIME_METRIC,  # type: ignore[no-untyped-def]
    **changes: object,
) -> OutcomeMetricFact:
    values: dict[str, object] = {
        **definition.model_dump(mode="python"),
        "value": value,
        "coverage_ratio": 1.0,
        "observation_semantics_version": "observation_semantics.v2",
        "trusted_for_analytics": True,
        "ambiguity_status": "native_v2",
    }
    values.pop("direct_relevance")
    values.update(changes)
    return OutcomeMetricFact.model_validate(values)


def _night(
    name: str,
    offset_days: int,
    value: float,
    *,
    definition=WAKE_TIME_METRIC,  # type: ignore[no-untyped-def]
    state: str = "hard_finalized",
    current: bool = True,
    **metric_changes: object,
) -> OutcomeNightEvidence:
    at = COMPLETED + timedelta(days=offset_days)
    return OutcomeNightEvidence(
        night_episode_id=f"night-{name}",
        night_episode_revision_id=f"episode-revision-{name}",
        night_episode_revision_number=1,
        finalization_revision_id=f"finalization-revision-{name}",
        finalization_revision_number=1,
        finalization_material_sha256=(name[0] * 64),
        finalization_state=state,
        is_current_finalization_revision=current,
        subject_id="subject-a",
        local_sleep_date=at.date().isoformat(),
        observation_end_at=at,
        coverage_status="complete",
        metrics=(
            _metric(value, definition=definition, **metric_changes),
        ),
    )


def _wake_evidence(
    baseline: tuple[float, float], followup: tuple[float, float]
) -> tuple[OutcomeNightEvidence, ...]:
    return (
        _night("a", -3, baseline[0]),
        _night("b", -2, baseline[1]),
        _night("c", 1, followup[0]),
        _night("d", 2, followup[1]),
    )


def test_every_closed_action_has_an_explicit_hashed_policy() -> None:
    policies = {action: outcome_policy_for(action) for action in CareActionType}
    assert set(policies) == set(CareActionType)
    assert len({item.policy_hash for item in policies.values()}) == 4
    assert policies[
        CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME
    ].evaluation_mode is EvaluationMode.WAKE_TIME_CONSISTENCY
    assert policies[
        CareActionType.RECOMMEND_MORNING_LIGHT
    ].evaluation_mode is EvaluationMode.INDIRECT_SLEEP_CONTEXT
    assert policies[
        CareActionType.REQUEST_MANUAL_FOLLOW_UP
    ].evaluation_mode is EvaluationMode.EXECUTION_ONLY
    with pytest.raises(ValueError, match="unsupported"):
        outcome_policy_for("send_email")


@pytest.mark.parametrize(
    ("baseline", "followup", "expected"),
    [
        ((360.0, 420.0), (390.0, 400.0), OutcomeCategory.IMPROVED),
        ((360.0, 380.0), (400.0, 420.0), OutcomeCategory.STABLE),
        ((390.0, 400.0), (360.0, 420.0), OutcomeCategory.WORSENED),
    ],
)
def test_wake_time_outcomes_are_deterministic_noncausal_and_pinned(
    baseline: tuple[float, float],
    followup: tuple[float, float],
    expected: OutcomeCategory,
) -> None:
    decision = evaluate_care_outcome(
        _execution(),
        _wake_evidence(baseline, followup),
        evaluated_at=COMPLETED + timedelta(days=3),
    )
    assert decision.lifecycle_state is OutcomeLifecycleState.EVALUATED
    assert decision.outcome is not None
    assert decision.outcome.outcome_category is expected
    assert decision.outcome.causal_claim is False
    assert decision.outcome.execution_authority == "human_attested"
    assert decision.outcome.baseline_revision_ids == (
        "finalization-revision-a",
        "finalization-revision-b",
    )
    assert decision.outcome.followup_revision_ids == (
        "finalization-revision-c",
        "finalization-revision-d",
    )
    assert decision.outcome.baseline_episode_revision_ids == (
        "episode-revision-a",
        "episode-revision-b",
    )
    assert decision.outcome.followup_episode_revision_ids == (
        "episode-revision-c",
        "episode-revision-d",
    )
    assert CAUSAL_DISCLAIMER_ZH_CN in decision.outcome.caveats


def test_completed_execution_waits_instead_of_guessing_stable_or_improved() -> None:
    decision = evaluate_care_outcome(
        _execution(),
        (_night("a", -2, 360), _night("b", -1, 420)),
        evaluated_at=COMPLETED + timedelta(hours=1),
    )
    assert decision.lifecycle_state is OutcomeLifecycleState.WAITING_FOR_FOLLOWUP
    assert decision.outcome is None
    assert decision.reason_code == "eligible_hard_finalized_followup_not_available"


def test_soft_only_followup_does_not_become_outcome_authority() -> None:
    evidence = (
        _night("a", -2, 360),
        _night("b", -1, 420),
        _night("c", 1, 390, state="soft_finalized"),
        _night("d", 2, 400, state="soft_finalized"),
    )
    decision = evaluate_care_outcome(
        _execution(), evidence, evaluated_at=COMPLETED + timedelta(days=3)
    )
    assert decision.lifecycle_state is OutcomeLifecycleState.WAITING_FOR_FOLLOWUP


def test_window_expiry_with_too_few_followups_is_insufficient_data() -> None:
    decision = evaluate_care_outcome(
        _execution(),
        (_night("a", -2, 360), _night("b", -1, 420)),
        evaluated_at=COMPLETED + timedelta(days=14),
    )
    assert decision.lifecycle_state is OutcomeLifecycleState.INSUFFICIENT_DATA
    assert decision.outcome is not None
    assert decision.outcome.outcome_category is OutcomeCategory.INSUFFICIENT_DATA
    assert decision.outcome.evidence_quality is EvidenceQuality.INSUFFICIENT


@pytest.mark.parametrize(
    "change",
    [
        {"unit": "second_of_local_day"},
        {"window_semantics": "aggregation_hour"},
        {"coverage_semantics": "soft_provisional_episode"},
        {"source_authority": "vendor_guess"},
    ],
)
def test_unit_window_coverage_and_authority_mismatch_are_not_comparable(
    change: dict[str, object],
) -> None:
    evidence = list(_wake_evidence((360, 420), (390, 400)))
    evidence[-1] = _night("d", 2, 400, **change)
    decision = evaluate_care_outcome(
        _execution(), tuple(evidence), evaluated_at=COMPLETED + timedelta(days=3)
    )
    assert decision.lifecycle_state is OutcomeLifecycleState.NOT_COMPARABLE
    assert decision.outcome is not None
    assert decision.outcome.outcome_category is OutcomeCategory.NOT_COMPARABLE


def test_ambiguous_movement_and_generic_movement_never_enter_trusted_evaluation() -> None:
    with pytest.raises(ValueError, match="generic movement"):
        _metric(1, metric_id="average_movement")
    ambiguous = _metric(
        3,
        metric_id="legacy_ambiguous_movement",
        unit="legacy_unknown",
        window_semantics="unknown",
        coverage_semantics="unknown",
        source_authority="vendor_derived",
        trusted_for_analytics=False,
        ambiguity_status="legacy_ambiguous",
    )
    assert ambiguous.trusted_for_analytics is False


def test_low_coverage_is_insufficient_not_a_directional_result() -> None:
    evidence = list(_wake_evidence((360, 420), (390, 400)))
    evidence[-1] = _night("d", 2, 400, coverage_ratio=0.5)
    decision = evaluate_care_outcome(
        _execution(), tuple(evidence), evaluated_at=COMPLETED + timedelta(days=3)
    )
    assert decision.lifecycle_state is OutcomeLifecycleState.INSUFFICIENT_DATA


def test_morning_light_never_claims_radar_observed_compliance() -> None:
    execution = _execution(CareActionType.RECOMMEND_MORNING_LIGHT)
    evidence = (
        _night("a", -1, 420, definition=SLEEP_WINDOW_METRIC),
        _night("b", 1, 450, definition=SLEEP_WINDOW_METRIC),
    )
    decision = evaluate_care_outcome(
        execution, evidence, evaluated_at=COMPLETED + timedelta(days=2)
    )
    assert decision.lifecycle_state is OutcomeLifecycleState.NOT_COMPARABLE
    assert decision.outcome is not None
    assert decision.outcome.outcome_category is OutcomeCategory.NOT_COMPARABLE
    assert "不能直接观察晨间光照" in decision.outcome.caveats[0]
    assert decision.personalization_receipt is not None
    assert decision.personalization_receipt.candidate is None


@pytest.mark.parametrize(
    "action",
    [
        CareActionType.REQUEST_MANUAL_FOLLOW_UP,
        CareActionType.REQUEST_MORNING_REVIEW_FEEDBACK,
    ],
)
def test_followup_tasks_have_execution_only_semantics(action: CareActionType) -> None:
    decision = evaluate_care_outcome(
        _execution(action), (), evaluated_at=COMPLETED + timedelta(minutes=1)
    )
    assert decision.lifecycle_state is OutcomeLifecycleState.EVALUATED
    assert decision.outcome is not None
    assert decision.outcome.outcome_category is OutcomeCategory.EXECUTION_ONLY
    assert decision.outcome.causal_claim is False


def test_only_valid_human_completed_execution_is_eligible() -> None:
    assert evaluation_eligible(_execution())
    for changes in (
        {"state": "cancelled"},
        {"state": "not_started"},
        {"execution_authority": "device_verified"},
        {"authority_valid_at_completion": False},
        {"plan_invalidated_before_completion": True},
    ):
        execution = _execution(**changes)
        assert not evaluation_eligible(execution)
        decision = evaluate_care_outcome(
            execution, (), evaluated_at=COMPLETED + timedelta(days=1)
        )
        assert decision.outcome is None
        assert decision.reason_code == "execution_not_eligible"


def test_personalization_receipt_is_distinct_and_never_directly_writes_memory() -> None:
    decision = evaluate_care_outcome(
        _execution(),
        _wake_evidence((360, 420), (390, 400)),
        evaluated_at=COMPLETED + timedelta(days=3),
    )
    assert decision.outcome is not None
    receipt = decision.personalization_receipt
    assert receipt is not None
    assert receipt.care_outcome_id == decision.outcome.care_outcome_id
    assert receipt.receipt_id != decision.outcome.care_outcome_id
    assert receipt.state is PersonalizationReceiptState.CANDIDATE_PROPOSED
    assert receipt.candidate is not None
    assert receipt.candidate.candidate_type is PersonalizationCandidateType.GOVERNED_MEMORY
    assert receipt.candidate.confirmation_required is True
    assert receipt.candidate.direct_write_permitted is False
    assert receipt.candidate.governance_path == (
        "existing_longitudinal_memory_elder_confirmation"
    )
    governed = MemoryChangeCandidate.model_validate(
        receipt.candidate.semantic_content
    )
    assert governed.confirmation_required is True
    assert governed.provenance_type == "accepted_evidence"
    assert governed.candidate_hash == receipt.candidate.candidate_semantic_hash
    change = MemoryChange(
        change_id="memory-change:care-outcome-proof",
        operation=MemoryOperation.REMEMBER,
        memory_id=governed.candidate_id,
        subject_id=governed.subject_id,
        expected_state_version=0,
        proposed_value=governed,
        causal_ref=receipt.receipt_id,
        source_actor_id="care-outcome-evaluator",
        source_actor_role="system",
        source_scope_kind=SourceScopeKind.THIRTY_DAY,
        confirmation_actor_id="elder-1",
        created_at=COMPLETED + timedelta(days=3),
        confirmation_expires_at=COMPLETED + timedelta(days=3, minutes=30),
    )
    confirmation = MemoryConfirmation(
        confirmation_id="memory-confirmation:care-outcome-proof",
        actor_id="elder-1",
        subject_id=governed.subject_id,
        target_change_id=change.change_id,
        target_change_hash=str(change.change_hash),
        approved_at=COMPLETED + timedelta(days=3, minutes=1),
        expires_at=change.confirmation_expires_at,
    )
    accepted = apply_memory_change(
        GovernedMemoryState(subject_id=governed.subject_id),
        change,
        confirmation,
        now=confirmation.approved_at,
    )
    current = project_current_memory(
        accepted, now=confirmation.approved_at
    )
    assert len(current) == 1
    assert current[0].provenance_type is ProvenanceType.ACCEPTED_EVIDENCE
    assert current[0].confirmation_ref == confirmation.confirmation_id


def test_same_evidence_retry_is_same_authority_and_late_revision_supersedes() -> None:
    evidence = _wake_evidence((360, 420), (390, 400))
    first = evaluate_care_outcome(
        _execution(), evidence, evaluated_at=COMPLETED + timedelta(days=3)
    )
    assert first.outcome is not None
    retry = evaluate_care_outcome(
        _execution(),
        evidence,
        evaluated_at=COMPLETED + timedelta(days=4),
        prior_outcome=first.outcome,
    )
    assert retry.outcome == first.outcome
    revised = list(evidence)
    revised[3] = OutcomeNightEvidence.model_validate(
        {
            **_night("d2", 2, 450).model_dump(mode="python"),
            "night_episode_id": "night-d",
            "night_episode_revision_number": 2,
            "finalization_revision_number": 2,
        }
    )
    second = evaluate_care_outcome(
        _execution(),
        tuple(revised),
        evaluated_at=COMPLETED + timedelta(days=4),
        prior_outcome=first.outcome,
    )
    assert second.outcome is not None
    assert second.outcome.evaluation_revision == 2
    assert second.outcome.supersedes_care_outcome_id == first.outcome.care_outcome_id
    assert second.outcome.care_outcome_id != first.outcome.care_outcome_id
    assert first.personalization_receipt is not None
    assert second.personalization_receipt is not None
    assert second.personalization_receipt.supersedes_receipt_id == (
        first.personalization_receipt.receipt_id
    )
