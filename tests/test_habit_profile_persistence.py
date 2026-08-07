from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sleepagent.radar_agent.persistence import RadarPersistenceStore
from sleepagent.radar_agent.product_agent import (
    HabitChangeOperation,
    HabitProfileChangeCandidate,
    HabitProfileChangeSet,
    HabitProfileConfirmation,
    PersistentHabitProfileStore,
    PersistentHabitQuestionnaireStateStore,
)
from sleepagent.radar_agent.product_agent.habit_api import (
    build_persistent_habit_profile_application,
)
from sleepagent.radar_agent.questionnaire import QuestionSuppression


NOW = datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc)


def change_set(
    *,
    change_set_id: str = "persistent-change",
    expected_version: int = 0,
) -> HabitProfileChangeSet:
    candidate = HabitProfileChangeCandidate.create(
        candidate_id=f"candidate:{change_set_id}",
        operation=HabitChangeOperation.CREATE,
        subject_id="elder-persistent",
        concept_id="habit.nap_pattern",
        concept_version="1.0.0",
        value="偶尔午睡",
        disposition="answered",
        origin_semantic="elder_self_report",
        source_actor_id="elder-persistent",
        source_role="elder",
        observation_date_start=date(2026, 7, 26),
        observation_date_end=date(2026, 7, 26),
        timezone_name="Asia/Shanghai",
        day_type="all_days",
        sleep_day_rule="wake_date",
        captured_at=NOW,
        valid_until=NOW + timedelta(days=90),
        source_answer_ref=f"answer:{change_set_id}",
    )
    return HabitProfileChangeSet.create(
        change_set_id=change_set_id,
        version=1,
        subject_id="elder-persistent",
        candidates=(candidate,),
        fact_snapshot_hash="a" * 64,
        expected_memory_version=expected_version,
        confirmation_expires_at=NOW + timedelta(hours=1),
        created_at=NOW,
    )


def confirmation(
    changes: HabitProfileChangeSet,
    *,
    confirmation_id: str = "confirmation:persistent",
) -> HabitProfileConfirmation:
    return HabitProfileConfirmation(
        confirmation_id=confirmation_id,
        actor_id="elder-persistent",
        actor_role="elder",
        subject_id=changes.subject_id,
        action_scope="write_habit_profile",
        change_set_id=changes.change_set_id,
        change_set_version=changes.version,
        manifest_hash=changes.manifest_hash,
        expires_at=changes.confirmation_expires_at,
    )


def persistent_store(path: Path) -> PersistentHabitProfileStore:
    persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(path, check_same_thread=False)
    )
    return PersistentHabitProfileStore(persistence)


def test_confirmed_profile_and_audit_survive_store_restart(tmp_path: Path) -> None:
    path = tmp_path / "habit.sqlite3"
    changes = change_set()
    signed = confirmation(changes)
    first = persistent_store(path)

    receipt = first.commit(
        changes,
        signed,
        idempotency_key="persistent-idempotency",
        now=NOW + timedelta(minutes=1),
    )
    first.persistence.connection.close()

    restarted = persistent_store(path)
    state = restarted.get(changes.subject_id)

    assert state.version == 1
    assert len(state.facts) == 1
    assert state.facts[0].concept_id == "habit.nap_pattern"
    assert state.audit_receipts == (receipt,)


def test_persistent_replay_returns_original_receipt_after_expiry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "habit.sqlite3"
    changes = change_set()
    signed = confirmation(changes)
    first = persistent_store(path)
    receipt = first.commit(
        changes,
        signed,
        idempotency_key="persistent-idempotency",
        now=NOW + timedelta(minutes=1),
    )
    first.persistence.connection.close()

    restarted = persistent_store(path)
    replay = restarted.commit(
        changes,
        signed,
        idempotency_key="persistent-idempotency",
        now=NOW + timedelta(days=1),
    )

    assert replay == receipt
    assert restarted.get(changes.subject_id).version == 1


def test_persistent_store_rejects_confirmation_reuse_and_stale_cas(
    tmp_path: Path,
) -> None:
    path = tmp_path / "habit.sqlite3"
    first_changes = change_set()
    signed = confirmation(first_changes)
    store = persistent_store(path)
    concurrent_store = persistent_store(path)
    store.commit(
        first_changes,
        signed,
        idempotency_key="first-idempotency",
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="confirmation already consumed"):
        store.commit(
            first_changes,
            signed,
            idempotency_key="different-idempotency",
            now=NOW + timedelta(minutes=2),
        )
    stale = change_set(change_set_id="stale-change", expected_version=0)
    with pytest.raises(ValueError, match="stale Habit Profile memory version"):
        concurrent_store.commit(
            stale,
            confirmation(stale, confirmation_id="confirmation:stale"),
            idempotency_key="stale-idempotency",
            now=NOW + timedelta(minutes=2),
        )
    assert store.get(first_changes.subject_id).version == 1


def test_persistent_idempotency_binds_exact_confirmation(tmp_path: Path) -> None:
    path = tmp_path / "habit.sqlite3"
    changes = change_set()
    store = persistent_store(path)
    store.commit(
        changes,
        confirmation(changes),
        idempotency_key="bound-idempotency",
        now=NOW + timedelta(minutes=1),
    )
    changed_confirmation = confirmation(
        changes,
        confirmation_id="confirmation:different",
    )

    with pytest.raises(ValueError, match="idempotency-key collision"):
        store.commit(
            changes,
            changed_confirmation,
            idempotency_key="bound-idempotency",
            now=NOW + timedelta(minutes=2),
        )


def test_product_application_uses_database_store_when_built_for_runtime() -> None:
    application = build_persistent_habit_profile_application(
        sqlite3.connect(":memory:", check_same_thread=False)
    )

    assert isinstance(application.runtime.store, PersistentHabitProfileStore)
    assert application.commit_controller.habit_profile_store is (
        application.runtime.store
    )
    assert isinstance(
        application.runtime.questionnaire.state_store,
        PersistentHabitQuestionnaireStateStore,
    )


def test_persistent_suppression_filters_expiry_and_detects_index_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "habit.sqlite3"
    persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(path, check_same_thread=False)
    )
    store = PersistentHabitQuestionnaireStateStore(persistence)
    suppression = QuestionSuppression(
        suppression_id="suppress:elder-persistent:habit.nap_pattern",
        subject_id="elder-persistent",
        concept_id="habit.nap_pattern",
        scope="profile_question",
        confirmation_ref="confirmation:suppress-nap",
        expires_at=NOW + timedelta(days=365),
    )
    from sleepagent.radar_agent.questionnaire import (
        HabitAnswerDisposition,
        HabitQuestionAnswer,
        HabitQuestionSelectionRequest,
        HabitQuestionTrigger,
        HabitQuestionnaireService,
    )

    service = HabitQuestionnaireService(state_store=store)
    selection = service.select(
        HabitQuestionSelectionRequest(
            request_id="request:suppression",
            episode_id="episode:suppression",
            subject_id="elder-persistent",
            actor_id="elder-persistent",
            role="elder",
            plan_id="plan:suppression",
            plan_revision=0,
            plan_step_id="progressive-habit-question",
            trigger=HabitQuestionTrigger.EXPLICIT_HABIT_QUESTION,
            decision_kind="evidence",
            alternative_explanations=("午睡",),
            candidate_concept_ids=("habit.nap_pattern",),
            remaining_episode_budget=3,
            max_questions=1,
        ),
        now=NOW,
    )
    service.capture(
        selection,
        (
            HabitQuestionAnswer(
                concept_id="habit.nap_pattern",
                concept_version="1.0.0",
                disposition=HabitAnswerDisposition.NEVER_ASK,
            ),
        ),
        episode_id=selection.episode_id,
        subject_id=selection.subject_id,
        actor_id=selection.actor_id,
        role=selection.role,
        suppression_confirmation_ref=suppression.confirmation_ref,
        now=NOW,
    )
    persistence.connection.close()

    restarted_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(path, check_same_thread=False)
    )
    restarted = PersistentHabitQuestionnaireStateStore(
        restarted_persistence
    )
    assert restarted.list_active_suppressions(
        subject_id="elder-persistent",
        now=NOW + timedelta(days=1),
    ) == (suppression,)
    assert restarted.list_active_suppressions(
        subject_id="elder-persistent",
        now=NOW + timedelta(days=366),
    ) == ()

    restarted_persistence.connection.execute(
        """
        UPDATE product_habit_question_suppressions
        SET confirmation_ref = ?
        WHERE subject_id = ? AND concept_id = ?
        """,
        ("confirmation:tampered", "elder-persistent", "habit.nap_pattern"),
    )
    restarted_persistence.connection.commit()
    with pytest.raises(ValueError, match="persisted index mismatch"):
        restarted.list_active_suppressions(
            subject_id="elder-persistent",
            now=NOW + timedelta(days=1),
        )


def test_persistent_question_budget_rejects_cross_connection_stale_issue(
    tmp_path: Path,
) -> None:
    from sleepagent.radar_agent.questionnaire import (
        HabitQuestionSelectionRequest,
        HabitQuestionTrigger,
        HabitQuestionnaireService,
    )

    path = tmp_path / "habit-question-concurrency.sqlite3"
    first_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(path, check_same_thread=False)
    )
    second_persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(path, check_same_thread=False)
    )
    first_store = PersistentHabitQuestionnaireStateStore(first_persistence)
    second_store = PersistentHabitQuestionnaireStateStore(second_persistence)
    selection = HabitQuestionnaireService(state_store=first_store).select(
        HabitQuestionSelectionRequest(
            request_id="request:concurrent-question",
            episode_id="episode:concurrent-question",
            subject_id="elder-persistent",
            actor_id="elder-persistent",
            role="elder",
            plan_id="plan:concurrent-question",
            plan_revision=0,
            plan_step_id="progressive-habit-question",
            trigger=HabitQuestionTrigger.EXPLICIT_HABIT_QUESTION,
            decision_kind="evidence",
            alternative_explanations=("午睡",),
            candidate_concept_ids=("habit.nap_pattern",),
            remaining_episode_budget=3,
            max_questions=1,
        ),
        now=NOW,
    )

    with pytest.raises(
        ValueError, match="concurrent Habit question selection conflict"
    ):
        second_store.issue_selection(
            selection,
            expected_question_count=0,
            cooldown_hours_by_concept={"habit.nap_pattern": 24},
        )
    assert second_store.episode_question_count(
        episode_id=selection.episode_id,
        subject_id=selection.subject_id,
    ) == 1


def test_persistent_cooldown_rejects_two_stale_concurrent_episodes(
    tmp_path: Path,
) -> None:
    from sleepagent.radar_agent.questionnaire import (
        HabitQuestionSelectionRequest,
        HabitQuestionTrigger,
        HabitQuestionnaireService,
    )

    def request(episode_id: str) -> HabitQuestionSelectionRequest:
        return HabitQuestionSelectionRequest(
            request_id=f"request:{episode_id}",
            episode_id=episode_id,
            subject_id="elder-persistent",
            actor_id="elder-persistent",
            role="elder",
            plan_id=f"plan:{episode_id}",
            plan_revision=0,
            plan_step_id="progressive-habit-question",
            trigger=HabitQuestionTrigger.EXPLICIT_HABIT_QUESTION,
            decision_kind="evidence",
            alternative_explanations=("午睡",),
            candidate_concept_ids=("habit.nap_pattern",),
            remaining_episode_budget=3,
            max_questions=1,
        )

    first_receipt = HabitQuestionnaireService().select(
        request("episode:cooldown-first"),
        now=NOW,
    )
    stale_second_receipt = HabitQuestionnaireService().select(
        request("episode:cooldown-second"),
        now=NOW,
    )
    path = tmp_path / "habit-question-cooldown.sqlite3"
    persistence = RadarPersistenceStore.connect_sqlite(
        sqlite3.connect(path, check_same_thread=False)
    )
    store = PersistentHabitQuestionnaireStateStore(persistence)
    cooldowns = {"habit.nap_pattern": 24}
    store.issue_selection(
        first_receipt,
        expected_question_count=0,
        cooldown_hours_by_concept=cooldowns,
    )

    with pytest.raises(
        ValueError, match="concurrent Habit question cooldown conflict"
    ):
        store.issue_selection(
            stale_second_receipt,
            expected_question_count=0,
            cooldown_hours_by_concept=cooldowns,
        )
    assert store.episode_question_count(
        episode_id=stale_second_receipt.episode_id,
        subject_id=stale_second_receipt.subject_id,
    ) == 0


def test_persistent_store_detects_index_and_receipt_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "habit.sqlite3"
    changes = change_set()
    signed = confirmation(changes)
    store = persistent_store(path)
    store.commit(
        changes,
        signed,
        idempotency_key="corruption-idempotency",
        now=NOW + timedelta(minutes=1),
    )
    connection = store.persistence.connection
    connection.execute(
        """
        UPDATE product_habit_profile_states
        SET version = 99
        WHERE subject_id = ?
        """,
        (changes.subject_id,),
    )
    connection.commit()
    with pytest.raises(ValueError, match="persisted version mismatch"):
        store.get(changes.subject_id)

    connection.execute(
        """
        UPDATE product_habit_profile_states
        SET version = 1
        WHERE subject_id = ?
        """,
        (changes.subject_id,),
    )
    row = connection.execute(
        """
        SELECT receipt_json
        FROM product_habit_profile_commits
        WHERE idempotency_key = ?
        """,
        ("corruption-idempotency",),
    ).fetchone()
    receipt = json.loads(row[0])
    receipt["subject_id"] = "different-subject"
    connection.execute(
        """
        UPDATE product_habit_profile_commits
        SET receipt_json = ?
        WHERE idempotency_key = ?
        """,
        (json.dumps(receipt), "corruption-idempotency"),
    )
    connection.commit()
    with pytest.raises(ValueError, match="persisted receipt mismatch"):
        store.commit(
            changes,
            signed,
            idempotency_key="corruption-idempotency",
            now=NOW + timedelta(minutes=2),
        )
