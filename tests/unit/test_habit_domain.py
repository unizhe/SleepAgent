from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest
from pydantic import ValidationError

from sleepagent.domain.habit import (
    CATALOG_VERSION,
    HABIT_CONCEPTS,
    REVIEWED_HABIT_CONCEPTS,
    HabitAnswer,
    HabitChange,
    HabitConcept,
    HabitConceptStatus,
    HabitConfirmation,
    HabitDisposition,
    HabitEvidence,
    HabitOperation,
    HabitProfileState,
    HabitQuestionState,
    apply_confirmed_habit_change,
    capture_habit_answers,
    propose_habit_change,
    select_habit_questions,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc
NOW = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)
EXPECTED_IDS = set(
    """habit.primary_goal habit.schedule_constraint habit.nap_pattern
    habit.nap_duration_minutes habit.pre_sleep_behavior habit.environment_preference
    habit.stimulant_timing habit.sleep_satisfaction_recent habit.observed_snoring
    habit.night_toileting_pattern habit.night_out_of_bed_frequency habit.night_out_of_bed_time_window
    habit.night_out_of_bed_duration_minutes habit.night_activity_assistance_need habit.observed_night_leaving
    habit.delivery_timing_preference habit.delivery_modality_preference habit.interruption_burden
    habit.family_notification_preference habit.quiet_hours habit.voice_volume_preference
    habit.device_position_last_night""".split()
)


def _select(
    *ids: str,
    role: str = "elder",
    actor: str = "elder-1",
    budget: int = 3,
    now: datetime = NOW,
    **updates: Any,
) -> HabitQuestionState:
    values: dict[str, Any] = {
        "episode_id": "episode-1",
        "subject_id": "subject-1",
        "actor_id": actor,
        "role": role,
        "candidate_concept_ids": ids,
        "remaining_episode_budget": budget,
        **updates,
    }
    return select_habit_questions(HabitQuestionState(**values), now=now)


def _capture(
    concept_id: str,
    value: Any,
    *,
    disposition: HabitDisposition = HabitDisposition.ANSWERED,
    role: str = "elder",
    actor: str = "elder-1",
    now: datetime = NOW,
    **answer_fields: Any,
) -> HabitEvidence:
    selected = _select(concept_id, role=role, actor=actor, now=now)
    answer = HabitAnswer(
        concept_id=concept_id,
        disposition=disposition,
        value=value,
        **answer_fields,
    )
    return _capture_selected(selected, answer, now=now + timedelta(minutes=1))[0]


def _capture_selected(
    selected: HabitQuestionState,
    *answers: HabitAnswer,
    now: datetime,
    catalog: Mapping[str, HabitConcept] = HABIT_CONCEPTS,
) -> tuple[HabitEvidence, ...]:
    return capture_habit_answers(
        selected,
        answers,
        episode_id=selected.episode_id,
        subject_id=selected.subject_id,
        actor_id=selected.actor_id,
        role=selected.role,
        now=now,
        catalog=catalog,
    )


def _change(
    evidence: HabitEvidence,
    operation: HabitOperation = HabitOperation.REMEMBER,
    *,
    version: int = 0,
    target: str | None = None,
    actor: str = "elder-1",
    now: datetime | None = None,
) -> HabitChange:
    return propose_habit_change(
        operation=operation,
        subject_id=evidence.subject_id,
        concept_id=evidence.concept_id,
        expected_profile_version=version,
        confirmation_actor_id=actor,
        now=now or evidence.captured_at + timedelta(minutes=1),
        evidence=evidence if operation in {HabitOperation.REMEMBER, HabitOperation.CORRECT} else None,
        target_fact_id=target,
    )


def _confirmation(change: HabitChange, **updates: Any) -> HabitConfirmation:
    values: dict[str, Any] = {
        "confirmation_id": f"confirm:{change.change_id}",
        "actor_id": change.confirmation_actor_id,
        "actor_role": "elder",
        "subject_id": change.subject_id,
        "target_change_id": change.change_id,
        "target_hash": change.change_hash,
        "approved_at": change.created_at + timedelta(minutes=1),
        "expires_at": change.confirmation_expires_at,
        **updates,
    }
    return HabitConfirmation(**values)


def _apply(
    state: HabitProfileState,
    change: HabitChange,
    **confirmation_updates: Any,
) -> HabitProfileState:
    confirmation = _confirmation(change, **confirmation_updates)
    return apply_confirmed_habit_change(
        state, change, confirmation, now=confirmation.approved_at
    )


def test_reviewed_catalog_preserves_all_ids_versions_and_schema() -> None:
    assert len(REVIEWED_HABIT_CONCEPTS) == len(HABIT_CONCEPTS) == 22
    assert set(HABIT_CONCEPTS) == EXPECTED_IDS
    for concept in REVIEWED_HABIT_CONCEPTS:
        assert (concept.version, concept.catalog_version) == ("1.0.0", CATALOG_VERSION)
        assert HabitConcept.model_validate(concept.model_dump()) == concept
        with pytest.raises(ValidationError, match="Extra inputs"):
            HabitConcept.model_validate({**concept.model_dump(), "runtime": True})


@pytest.mark.parametrize(
    ("budget", "expected"),
    [
        (0, ()),
        (1, ("habit.primary_goal",)),
        (3, ("habit.primary_goal", "habit.schedule_constraint", "habit.nap_pattern")),
    ],
)
def test_selection_budget_and_priority(budget: int, expected: tuple[str, ...]) -> None:
    selected = _select(
        "habit.nap_pattern", "habit.schedule_constraint", "habit.primary_goal",
        budget=budget,
        concept_states={
            "habit.primary_goal": HabitConceptStatus.DISPUTED,
            "habit.schedule_constraint": HabitConceptStatus.STALE,
            "habit.nap_pattern": HabitConceptStatus.UNKNOWN,
        },
    )
    assert selected.selected_concepts == tuple(
        (concept_id, "1.0.0") for concept_id in expected
    )


def test_selection_applies_cooldown_suppression_and_family_scope() -> None:
    selected = _select(
        "habit.primary_goal", "habit.nap_pattern", "habit.observed_snoring",
        role="family", actor="family-1",
        suppressed_concept_ids=("habit.nap_pattern",),
        cooldown_until={"habit.observed_snoring": NOW + timedelta(hours=1)},
    )
    assert selected.selected_concepts == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject_id", "subject-2"), ("actor_id", "elder-2"),
        ("role", "family"), ("episode_id", "episode-2"),
        ("selected_concepts", (("habit.primary_goal", "2.0.0"),)),
    ],
)
def test_receipt_rejects_binding_and_payload_tampering(field: str, value: Any) -> None:
    forged = _select("habit.primary_goal").model_copy(update={field: value})
    with pytest.raises(ValueError, match="receipt is forged"):
        _capture_selected(
            forged,
            HabitAnswer(
                concept_id="habit.primary_goal",
                disposition=HabitDisposition.ANSWERED,
                value="更规律",
            ),
            now=NOW + timedelta(minutes=1),
        )


def test_receipt_rejects_expiry_and_unreviewed_answer_version() -> None:
    selected = _select("habit.primary_goal")
    wrong_version = HabitAnswer(
        concept_id="habit.primary_goal", concept_version="2.0.0",
        disposition=HabitDisposition.ANSWERED, value="更规律",
    )
    with pytest.raises(ValueError, match="version does not match selection receipt"):
        _capture_selected(selected, wrong_version, now=NOW + timedelta(minutes=1))
    with pytest.raises(ValueError, match="expired"):
        _capture_selected(selected, now=NOW + timedelta(minutes=30))


@pytest.mark.parametrize(
    ("answer_version", "accepted"),
    [("2.0.0", False), ("1.0.0", True)],
    ids=("new-answer-rejected", "frozen-answer-accepted"),
)
def test_capture_uses_receipt_version_after_catalog_upgrade(
    answer_version: str, accepted: bool
) -> None:
    selected = _select("habit.primary_goal")
    upgraded = HABIT_CONCEPTS["habit.primary_goal"].model_copy(
        update={"version": "2.0.0", "question": "升级后的审核问题"}
    )
    answer = HabitAnswer(
        concept_id="habit.primary_goal",
        concept_version=answer_version,
        disposition=HabitDisposition.ANSWERED,
        value="更规律",
    )
    if not accepted:
        with pytest.raises(
            ValueError, match="version does not match selection receipt"
        ):
            _capture_selected(
                selected, answer, now=NOW + timedelta(minutes=1),
                catalog={"habit.primary_goal": upgraded},
            )
        return
    evidence = _capture_selected(
        selected, answer, now=NOW + timedelta(minutes=1),
        catalog={"habit.primary_goal": upgraded},
    )
    assert evidence[0].concept_version == "1.0.0"


@pytest.mark.parametrize(
    ("concept", "value", "disposition", "expected"),
    [
        ("habit.primary_goal", "更规律", HabitDisposition.ANSWERED, "更规律"),
        ("habit.nap_duration_minutes", 45, HabitDisposition.ANSWERED, {"value": 45.0, "unit": "minute"}),
        ("habit.quiet_hours", "２２:００－０６:００", HabitDisposition.ANSWERED, "22:00-06:00"),
        ("habit.nap_pattern", None, HabitDisposition.VARIABLE, "variable"),
        ("habit.nap_pattern", None, HabitDisposition.UNKNOWN, None),
        ("habit.nap_pattern", None, HabitDisposition.SKIPPED, None),
        ("habit.nap_pattern", None, HabitDisposition.NOT_APPLICABLE, None),
    ],
)
def test_answer_normalization_and_nonanswers(
    concept: str, value: Any, disposition: HabitDisposition, expected: Any
) -> None:
    evidence = _capture(concept, value, disposition=disposition)
    assert evidence.value == expected
    assert evidence.profile_eligible is (disposition in {
        HabitDisposition.ANSWERED,
        HabitDisposition.VARIABLE,
        HabitDisposition.NOT_APPLICABLE,
    })


@pytest.mark.parametrize(
    ("disposition", "value"),
    [(HabitDisposition.UNKNOWN, "偷偷带值"), (HabitDisposition.ANSWERED, "目录外选项")],
)
def test_invalid_answer_value_is_rejected(
    disposition: HabitDisposition, value: str
) -> None:
    with pytest.raises(ValueError):
        _capture("habit.primary_goal", value, disposition=disposition)


def test_family_direct_observation_and_hearsay_boundary() -> None:
    indirect = _capture(
        "habit.observed_snoring", "没有观察到", role="family", actor="family-1"
    )
    direct = _capture(
        "habit.observed_snoring", "没有观察到", role="family", actor="family-1",
        direct_observation=True,
        observation_description="本人整夜在同一房间直接观察",
        observation_confidence=0.8,
    )
    assert (indirect.disposition, indirect.value) == (HabitDisposition.UNKNOWN, None)
    assert (direct.disposition, direct.value, direct.origin) == (
        HabitDisposition.ANSWERED, "没有观察到", "family_observation"
    )
    with pytest.raises(ValidationError, match="hearsay"):
        HabitAnswer(
            concept_id="habit.observed_snoring",
            disposition=HabitDisposition.ANSWERED, value="观察到",
            direct_observation=True, observation_description="听说老人昨晚打鼾",
            observation_confidence=0.8,
        )


def test_safety_preempts_remaining_capture() -> None:
    selected = _select("habit.observed_snoring", "habit.nap_pattern")
    answers = (
        HabitAnswer(
            concept_id="habit.observed_snoring",
            disposition=HabitDisposition.ANSWERED, value="观察到",
        ),
        HabitAnswer(
            concept_id="habit.nap_pattern",
            disposition=HabitDisposition.ANSWERED, value="偶尔午睡",
        ),
    )
    captured = _capture_selected(
        selected, *answers, now=NOW + timedelta(minutes=1)
    )
    assert len(captured) == 1
    assert (captured[0].safety_reason, captured[0].profile_eligible) == (
        "observed_breathing_signal", False
    )


def test_suppression_clinical_and_episode_only_boundaries() -> None:
    suppressed = _capture(
        "habit.primary_goal", None, disposition=HabitDisposition.NEVER_ASK,
        opt_out_acknowledged=True,
    )
    clinical = _capture("habit.quiet_hours", "因高血压用药后保持安静")
    episode_only = _capture("habit.device_position_last_night", "没有")
    assert suppressed.suppressed_until == NOW + timedelta(minutes=1, days=365)
    for evidence in (clinical, episode_only):
        assert evidence.profile_eligible is False
        with pytest.raises(ValueError):
            _change(evidence)


def test_evidence_hash_detects_tampering_before_change() -> None:
    forged = _capture("habit.primary_goal", "更规律").model_copy(
        update={"value": "白天更有精神"}
    )
    with pytest.raises(ValueError, match="evidence hash mismatch"):
        _change(forged)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor_id", "elder-2"), ("subject_id", "subject-2"),
        ("target_change_id", "habit-change:other"),
        ("target_hash", "f" * 64), ("expires_at", NOW + timedelta(hours=2)),
    ],
)
def test_confirmation_exact_actor_subject_target_hash_and_expiry(
    field: str, value: Any
) -> None:
    change = _change(_capture("habit.primary_goal", "更规律"))
    with pytest.raises(PermissionError, match="not exactly bound"):
        _apply(HabitProfileState(subject_id="subject-1"), change, **{field: value})


def test_remember_correct_expire_are_confirmed_append_only() -> None:
    remembered = _apply(
        HabitProfileState(subject_id="subject-1"),
        _change(_capture("habit.primary_goal", "更规律")),
    )
    original = remembered.revisions[0]
    evidence = _capture(
        "habit.primary_goal", "白天更有精神", now=NOW + timedelta(hours=1)
    )
    corrected = _apply(remembered, _change(
        evidence, HabitOperation.CORRECT, version=1, target=original.fact_id
    ))
    corrected_fact = corrected.current(NOW + timedelta(hours=2))[0]
    expired = _apply(corrected, _change(
        evidence, HabitOperation.EXPIRE, version=2, target=corrected_fact.fact_id,
        now=NOW + timedelta(hours=2),
    ))
    assert remembered.revisions == (original,)
    assert corrected.revisions[:1] == remembered.revisions
    assert [item.operation for item in expired.revisions] == [
        HabitOperation.REMEMBER, HabitOperation.CORRECT, HabitOperation.EXPIRE
    ]
    assert expired.current(NOW + timedelta(hours=3)) == ()


def test_forget_tombstone_and_stale_profile_version() -> None:
    evidence = _capture("habit.primary_goal", "更规律")
    remembered = _apply(
        HabitProfileState(subject_id="subject-1"), _change(evidence)
    )
    fact = remembered.revisions[0]
    forgotten = _apply(remembered, _change(
        evidence, HabitOperation.FORGET, version=1, target=fact.fact_id,
        now=NOW + timedelta(hours=1),
    ))
    assert (forgotten.revisions[-1].operation, forgotten.revisions[-1].value) == (
        HabitOperation.FORGET, None
    )
    assert forgotten.current(NOW + timedelta(hours=2)) == ()
    valid_but_stale = _change(
        _capture("habit.nap_pattern", "偶尔午睡"), version=0
    )
    with pytest.raises(ValueError, match="subject/version is stale"):
        _apply(remembered, valid_but_stale)


def test_independent_sources_create_conflict_projection() -> None:
    first = _apply(
        HabitProfileState(subject_id="subject-1"),
        _change(_capture("habit.nap_pattern", "偶尔午睡")),
    )
    other = _capture(
        "habit.nap_pattern", "多数天午睡",
        actor="elder-2", now=NOW + timedelta(hours=1),
    )
    change = _change(
        other, version=1, actor="elder-2", now=NOW + timedelta(hours=1, minutes=2)
    )
    conflicted = _apply(first, change)
    assert len(conflicted.current(NOW + timedelta(hours=2))) == 2
    assert conflicted.disputed_concept_ids(NOW + timedelta(hours=2)) == (
        "habit.nap_pattern",
    )


def test_habit_change_rejects_memory_identity_namespace() -> None:
    values = _change(_capture("habit.primary_goal", "更规律")).model_dump()
    values.update(change_id="memory:change-1", change_hash="0" * 64)
    with pytest.raises(ValidationError, match="cannot share change identity"):
        HabitChange.model_validate(values)
