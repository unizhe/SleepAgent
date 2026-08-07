from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sleepagent.radar_agent.persistence import RadarPersistenceStore
from sleepagent.radar_agent.product_agent.habit_profile import (
    HabitProfileCommitReceipt,
    HabitProfileConfirmation,
    HabitProfileChangeSet,
    HabitProfileState,
    InMemoryHabitProfileStore,
    habit_commit_payload_hash,
)
from sleepagent.radar_agent.questionnaire import (
    HabitConceptDefinition,
    HabitQuestionSelectionReceipt,
    QuestionSuppression,
)


HABIT_PERSISTENCE_VERSION = "sleepagent-habit-persistence.v3"


class PersistentHabitQuestionnaireStateStore:
    """Durable budget, cooldown, receipt and suppression authority."""

    def __init__(self, persistence: RadarPersistenceStore) -> None:
        self.persistence = persistence
        self.connection = persistence.connection
        self.dialect = persistence.dialect

    def episode_question_count(
        self,
        *,
        episode_id: str,
        subject_id: str,
    ) -> int:
        with self.persistence.transaction_lock:
            row = self.connection.execute(
                self._sql(
                    """
                    SELECT question_count
                    FROM product_habit_question_episodes
                    WHERE episode_id = ? AND subject_id = ?
                    """
                ),
                (episode_id, subject_id),
            ).fetchone()
        count = 0 if row is None else int(row[0])
        if count < 0 or count > 3:
            raise ValueError("Habit Episode question count is corrupted")
        return count

    def last_asked_at(
        self,
        *,
        subject_id: str,
        actor_id: str,
        role: str,
        trigger: str,
        concept_id: str,
    ) -> datetime | None:
        with self.persistence.transaction_lock:
            row = self.connection.execute(
                self._sql(
                    """
                    SELECT last_asked_at
                    FROM product_habit_question_cooldowns
                    WHERE subject_id = ? AND actor_id = ? AND role = ?
                      AND trigger = ?
                      AND concept_id = ?
                    """
                ),
                (subject_id, actor_id, role, trigger, concept_id),
            ).fetchone()
        return None if row is None else _database_datetime(row[0])

    def issue_selection(
        self,
        receipt: HabitQuestionSelectionReceipt,
        *,
        expected_question_count: int,
        cooldown_hours_by_concept: dict[str, int],
    ) -> None:
        candidate_ids = {
            candidate.concept_id for candidate in receipt.candidates
        }
        if set(cooldown_hours_by_concept) != candidate_ids:
            raise ValueError("Habit selection cooldown contract mismatch")
        if any(hours < 0 for hours in cooldown_hours_by_concept.values()):
            raise ValueError("Habit selection cooldown is invalid")
        if (
            receipt.episode_question_count_after
            != expected_question_count + len(receipt.candidates)
            or receipt.episode_question_count_after > 3
        ):
            raise ValueError("Habit selection exceeds Episode budget")
        with self.persistence.transaction_lock:
            cursor = self.connection.cursor()
            try:
                if self.dialect == "sqlite":
                    cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO product_habit_question_episodes (
                          episode_id, subject_id, question_count, updated_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(episode_id, subject_id) DO NOTHING
                        """
                    ),
                    (
                        receipt.episode_id,
                        receipt.subject_id,
                        0,
                        receipt.issued_at.isoformat(),
                    ),
                )
                count_row = cursor.execute(
                    self._sql(
                        """
                        SELECT question_count
                        FROM product_habit_question_episodes
                        WHERE episode_id = ? AND subject_id = ?
                        """
                        + (" FOR UPDATE" if self.dialect == "postgres" else "")
                    ),
                    (receipt.episode_id, receipt.subject_id),
                ).fetchone()
                if (
                    count_row is None
                    or int(count_row[0]) != expected_question_count
                ):
                    raise ValueError(
                        "concurrent Habit question selection conflict"
                    )
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO product_habit_question_selections (
                          selection_id, episode_id, subject_id, actor_id, role,
                          receipt_json, consumed, issued_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        receipt.selection_id,
                        receipt.episode_id,
                        receipt.subject_id,
                        receipt.actor_id,
                        receipt.role,
                        receipt.model_dump_json(),
                        False,
                        receipt.issued_at.isoformat(),
                        receipt.expires_at.isoformat(),
                    ),
                )
                cursor.execute(
                    self._sql(
                        """
                        UPDATE product_habit_question_episodes
                        SET question_count = ?, updated_at = ?
                        WHERE episode_id = ? AND subject_id = ?
                          AND question_count = ?
                        """
                    ),
                    (
                        receipt.episode_question_count_after,
                        receipt.issued_at.isoformat(),
                        receipt.episode_id,
                        receipt.subject_id,
                        expected_question_count,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "concurrent Habit question selection conflict"
                    )
                for candidate in receipt.candidates:
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_habit_question_cooldowns (
                              subject_id, actor_id, role, trigger, concept_id,
                              last_asked_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(
                              subject_id, actor_id, role, trigger, concept_id
                            )
                            DO UPDATE SET last_asked_at = excluded.last_asked_at
                            WHERE product_habit_question_cooldowns.last_asked_at
                              <= ?
                            """
                        ),
                        (
                            receipt.subject_id,
                            receipt.actor_id,
                            receipt.role,
                            receipt.trigger.value,
                            candidate.concept_id,
                            receipt.issued_at.isoformat(),
                            (
                                receipt.issued_at
                                - timedelta(
                                    hours=cooldown_hours_by_concept[
                                        candidate.concept_id
                                    ]
                                )
                            ).isoformat(),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError(
                            "concurrent Habit question cooldown conflict"
                        )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def get_selection(
        self,
        selection_id: str,
    ) -> tuple[HabitQuestionSelectionReceipt, bool] | None:
        with self.persistence.transaction_lock:
            row = self.connection.execute(
                self._sql(
                    """
                    SELECT episode_id, subject_id, actor_id, role, receipt_json,
                           consumed, issued_at, expires_at
                    FROM product_habit_question_selections
                    WHERE selection_id = ?
                    """
                ),
                (selection_id,),
            ).fetchone()
        if row is None:
            return None
        receipt = _model_from_database(HabitQuestionSelectionReceipt, row[4])
        if (
            receipt.selection_id,
            receipt.episode_id,
            receipt.subject_id,
            receipt.actor_id,
            receipt.role,
            receipt.issued_at,
            receipt.expires_at,
        ) != (
            selection_id,
            row[0],
            row[1],
            row[2],
            row[3],
            _database_datetime(row[6]),
            _database_datetime(row[7]),
        ):
            raise ValueError("Habit selection persisted index mismatch")
        return receipt, bool(row[5])

    def finalize_capture(
        self,
        receipt: HabitQuestionSelectionReceipt,
        suppressions: Iterable[QuestionSuppression],
        *,
        now: datetime | None = None,
    ) -> None:
        staged = tuple(suppressions)
        captured_at = now or datetime.now(timezone.utc)
        if receipt.expires_at <= captured_at:
            raise ValueError("Habit selection receipt expired")
        candidate_ids = {
            candidate.concept_id for candidate in receipt.candidates
        }
        if len(
            {
                (item.subject_id, item.concept_id, item.scope)
                for item in staged
            }
        ) != len(staged):
            raise ValueError("duplicate Question suppression")
        for item in staged:
            if item.expires_at <= captured_at:
                raise ValueError("Question suppression must expire in the future")
            if (
                item.subject_id != receipt.subject_id
                or item.concept_id not in candidate_ids
            ):
                raise ValueError(
                    "Question suppression is outside issued selection"
                )
        with self.persistence.transaction_lock:
            cursor = self.connection.cursor()
            try:
                if self.dialect == "sqlite":
                    cursor.execute("BEGIN IMMEDIATE")
                row = cursor.execute(
                    self._sql(
                        """
                        SELECT receipt_json, consumed
                        FROM product_habit_question_selections
                        WHERE selection_id = ?
                        """
                        + (" FOR UPDATE" if self.dialect == "postgres" else "")
                    ),
                    (receipt.selection_id,),
                ).fetchone()
                if row is None or _model_from_database(
                    HabitQuestionSelectionReceipt, row[0]
                ) != receipt:
                    raise ValueError(
                        "Habit selection receipt is forged or unknown"
                    )
                if bool(row[1]):
                    raise ValueError(
                        "Habit selection receipt was already consumed"
                    )
                for item in staged:
                    cursor.execute(
                        self._sql(
                            """
                            INSERT INTO product_habit_question_suppressions (
                              subject_id, concept_id, scope, confirmation_ref,
                              expires_at, suppression_json, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(subject_id, concept_id, scope) DO UPDATE SET
                              confirmation_ref = excluded.confirmation_ref,
                              expires_at = excluded.expires_at,
                              suppression_json = excluded.suppression_json,
                              updated_at = excluded.updated_at
                            """
                        ),
                        (
                            item.subject_id,
                            item.concept_id,
                            item.scope,
                            item.confirmation_ref,
                            item.expires_at.isoformat(),
                            item.model_dump_json(),
                            captured_at.isoformat(),
                        ),
                    )
                cursor.execute(
                    self._sql(
                        """
                        UPDATE product_habit_question_selections
                        SET consumed = ?
                        WHERE selection_id = ? AND consumed = ?
                        """
                    ),
                    (True, receipt.selection_id, False),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "Habit selection receipt was already consumed"
                    )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def list_active_suppressions(
        self,
        *,
        subject_id: str,
        now: datetime | None = None,
    ) -> tuple[QuestionSuppression, ...]:
        read_at = now or datetime.now(timezone.utc)
        with self.persistence.transaction_lock:
            rows = self.connection.execute(
                self._sql(
                    """
                    SELECT concept_id, scope, confirmation_ref, expires_at,
                           suppression_json
                    FROM product_habit_question_suppressions
                    WHERE subject_id = ?
                    ORDER BY concept_id, scope
                    """
                ),
                (subject_id,),
            ).fetchall()
        active: list[QuestionSuppression] = []
        for row in rows:
            item = _model_from_database(QuestionSuppression, row[4])
            indexed_expiry = _database_datetime(row[3])
            if (
                item.subject_id,
                item.concept_id,
                item.scope,
                item.confirmation_ref,
                item.expires_at,
            ) != (
                subject_id,
                row[0],
                row[1],
                row[2],
                indexed_expiry,
            ):
                raise ValueError("Question suppression persisted index mismatch")
            if item.expires_at > read_at:
                active.append(item)
        return tuple(active)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql


PersistentQuestionSuppressionStore = PersistentHabitQuestionnaireStateStore


class PersistentHabitProfileStore(InMemoryHabitProfileStore):
    """Database-backed typed Habit Profile authority.

    It reuses the same validation and atomic candidate application rules as the
    in-memory reference store, while persisting state, audit receipts,
    idempotency bindings and consumed confirmation IDs in one DB transaction.
    """

    def __init__(
        self,
        persistence: RadarPersistenceStore,
        concepts: Iterable[HabitConceptDefinition] | None = None,
    ) -> None:
        super().__init__(concepts=concepts)
        self.persistence = persistence
        self.connection = persistence.connection
        self.dialect = persistence.dialect

    def get(self, subject_id: str) -> HabitProfileState:
        with self.persistence.transaction_lock:
            row = self.connection.execute(
                self._sql(
                    """
                    SELECT version, state_json
                    FROM product_habit_profile_states
                    WHERE subject_id = ?
                    """
                ),
                (subject_id,),
            ).fetchone()
            state = (
                HabitProfileState(subject_id=subject_id)
                if row is None
                else _model_from_database(HabitProfileState, row[1])
            )
            if state.subject_id != subject_id:
                raise ValueError("Habit Profile persisted subject mismatch")
            if row is not None and int(row[0]) != state.version:
                raise ValueError("Habit Profile persisted version mismatch")
            self._validate_state_integrity(state)
            return state.model_copy(deep=True)

    def commit(
        self,
        change_set: HabitProfileChangeSet,
        confirmation: HabitProfileConfirmation,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> HabitProfileCommitReceipt:
        committed_at = now or datetime.now(timezone.utc)
        self._validate_change_set_integrity(change_set)
        payload_hash = habit_commit_payload_hash(change_set, confirmation)
        with self.persistence.transaction_lock:
            cursor = self.connection.cursor()
            try:
                if self.dialect == "sqlite":
                    cursor.execute("BEGIN IMMEDIATE")
                initial = HabitProfileState(subject_id=change_set.subject_id)
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO product_habit_profile_states (
                          subject_id, version, state_json, updated_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(subject_id) DO NOTHING
                        """
                    ),
                    (
                        initial.subject_id,
                        initial.version,
                        initial.model_dump_json(),
                        committed_at.isoformat(),
                    ),
                )
                state_row = cursor.execute(
                    self._sql(
                        """
                        SELECT version, state_json
                        FROM product_habit_profile_states
                        WHERE subject_id = ?
                        """
                        + (" FOR UPDATE" if self.dialect == "postgres" else "")
                    ),
                    (change_set.subject_id,),
                ).fetchone()
                if state_row is None:
                    raise ValueError(
                        "Habit Profile state row was not initialized"
                    )
                state = _model_from_database(
                    HabitProfileState,
                    state_row[1],
                )
                if (
                    int(state_row[0]) != state.version
                ):
                    raise ValueError(
                        "Habit Profile persisted version mismatch"
                    )
                self._validate_state_integrity(state)

                replay_row = cursor.execute(
                    self._sql(
                        """
                        SELECT payload_hash, receipt_json, subject_id
                        FROM product_habit_profile_commits
                        WHERE idempotency_key = ?
                        """
                    ),
                    (idempotency_key,),
                ).fetchone()
                if replay_row is not None:
                    if replay_row[0] != payload_hash:
                        raise ValueError(
                            "Habit Profile idempotency-key collision"
                        )
                    if replay_row[2] != change_set.subject_id:
                        raise ValueError(
                            "Habit Profile idempotency replay crosses subject"
                        )
                    receipt = _model_from_database(
                        HabitProfileCommitReceipt,
                        replay_row[1],
                    )
                    if (
                        receipt.subject_id,
                        receipt.change_set_id,
                        receipt.manifest_hash,
                        receipt.idempotency_key,
                    ) != (
                        change_set.subject_id,
                        change_set.change_set_id,
                        change_set.manifest_hash,
                        idempotency_key,
                    ):
                        raise ValueError(
                            "Habit Profile persisted receipt mismatch"
                        )
                    if receipt not in state.audit_receipts:
                        raise ValueError(
                            "Habit Profile receipt is absent from state audit"
                        )
                    self.connection.commit()
                    return receipt.model_copy(deep=True)

                confirmation.validate_for(change_set, now=committed_at)
                consumed = cursor.execute(
                    self._sql(
                        """
                        SELECT idempotency_key
                        FROM product_habit_profile_commits
                        WHERE confirmation_id = ?
                        """
                    ),
                    (confirmation.confirmation_id,),
                ).fetchone()
                if consumed is not None:
                    raise ValueError(
                        "Habit Profile confirmation already consumed"
                    )
                if state.version != change_set.expected_memory_version:
                    raise ValueError("stale Habit Profile memory version")

                worker = InMemoryHabitProfileStore(concepts=self._concepts)
                worker._states[change_set.subject_id] = state.model_copy(  # noqa: SLF001
                    deep=True
                )
                receipt = worker.commit(
                    change_set,
                    confirmation,
                    idempotency_key=idempotency_key,
                    now=committed_at,
                )
                updated = worker.get(change_set.subject_id)
                cursor.execute(
                    self._sql(
                        """
                        UPDATE product_habit_profile_states
                        SET version = ?, state_json = ?, updated_at = ?
                        WHERE subject_id = ? AND version = ?
                        """
                    ),
                    (
                        updated.version,
                        updated.model_dump_json(),
                        committed_at.isoformat(),
                        updated.subject_id,
                        state.version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "concurrent Habit Profile commit conflict"
                    )
                cursor.execute(
                    self._sql(
                        """
                        INSERT INTO product_habit_profile_commits (
                          idempotency_key, payload_hash, confirmation_id,
                          subject_id, receipt_json, committed_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (
                        idempotency_key,
                        payload_hash,
                        confirmation.confirmation_id,
                        change_set.subject_id,
                        receipt.model_dump_json(),
                        committed_at.isoformat(),
                    ),
                )
                self.connection.commit()
                return receipt.model_copy(deep=True)
            except Exception:
                self.connection.rollback()
                raise
            finally:
                cursor.close()

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql


def _model_from_database(model: type[Any], value: Any) -> Any:
    if isinstance(value, (str, bytes, bytearray)):
        return model.model_validate_json(value)
    return model.model_validate(value)


def _database_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


__all__ = [
    "HABIT_PERSISTENCE_VERSION",
    "PersistentHabitProfileStore",
    "PersistentHabitQuestionnaireStateStore",
    "PersistentQuestionSuppressionStore",
]
