from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Iterable, Protocol
from uuid import uuid4

from .contracts import (
    CapturedHabitAnswer,
    HabitAnswerDisposition,
    HabitAnswerType,
    HabitConceptDefinition,
    HabitConceptStatus,
    HabitPersistenceEligibility,
    HabitQuestionAnswer,
    HabitQuestionCandidate,
    HabitQuestionCapture,
    HabitQuestionSelectionReceipt,
    HabitQuestionSelectionRequest,
    HabitQuestionTrigger,
    HabitRespondentRule,
    HabitSafetyEvent,
    QuestionSuppression,
)
from .defaults import DEFAULT_HABIT_CONCEPTS

HABIT_QUESTIONNAIRE_VERSION = "sleepagent-habit-questionnaire.v3"


class HabitQuestionnaireStateStore(Protocol):
    def episode_question_count(
        self,
        *,
        episode_id: str,
        subject_id: str,
    ) -> int: ...

    def last_asked_at(
        self,
        *,
        subject_id: str,
        actor_id: str,
        role: str,
        trigger: str,
        concept_id: str,
    ) -> datetime | None: ...

    def issue_selection(
        self,
        receipt: HabitQuestionSelectionReceipt,
        *,
        expected_question_count: int,
        cooldown_hours_by_concept: dict[str, int],
    ) -> None: ...

    def get_selection(
        self,
        selection_id: str,
    ) -> tuple[HabitQuestionSelectionReceipt, bool] | None: ...

    def finalize_capture(
        self,
        receipt: HabitQuestionSelectionReceipt,
        suppressions: Iterable[QuestionSuppression],
        *,
        now: datetime | None = None,
    ) -> None: ...

    def list_active_suppressions(
        self,
        *,
        subject_id: str,
        now: datetime | None = None,
    ) -> tuple[QuestionSuppression, ...]: ...


class InMemoryHabitQuestionnaireStateStore:
    def __init__(self) -> None:
        self._episode_counts: dict[tuple[str, str], int] = {}
        self._selections: dict[str, HabitQuestionSelectionReceipt] = {}
        self._consumed_selections: set[str] = set()
        self._last_asked: dict[
            tuple[str, str, str, str, str], datetime
        ] = {}
        self._suppressions: dict[
            tuple[str, str, str], QuestionSuppression
        ] = {}
        self._lock = RLock()

    def episode_question_count(
        self,
        *,
        episode_id: str,
        subject_id: str,
    ) -> int:
        with self._lock:
            return self._episode_counts.get((episode_id, subject_id), 0)

    def last_asked_at(
        self,
        *,
        subject_id: str,
        actor_id: str,
        role: str,
        trigger: str,
        concept_id: str,
    ) -> datetime | None:
        with self._lock:
            return self._last_asked.get(
                (subject_id, actor_id, role, trigger, concept_id)
            )

    def issue_selection(
        self,
        receipt: HabitQuestionSelectionReceipt,
        *,
        expected_question_count: int,
        cooldown_hours_by_concept: dict[str, int],
    ) -> None:
        key = (receipt.episode_id, receipt.subject_id)
        candidate_ids = {
            candidate.concept_id for candidate in receipt.candidates
        }
        if set(cooldown_hours_by_concept) != candidate_ids:
            raise ValueError("Habit selection cooldown contract mismatch")
        with self._lock:
            actual = self._episode_counts.get(key, 0)
            if actual != expected_question_count:
                raise ValueError(
                    "concurrent Habit question selection conflict"
                )
            if receipt.selection_id in self._selections:
                raise ValueError("Habit selection ID already exists")
            if (
                receipt.episode_question_count_after
                != actual + len(receipt.candidates)
                or receipt.episode_question_count_after > 3
            ):
                raise ValueError("Habit selection exceeds Episode budget")
            for candidate in receipt.candidates:
                cooldown_hours = cooldown_hours_by_concept[
                    candidate.concept_id
                ]
                if cooldown_hours < 0:
                    raise ValueError("Habit selection cooldown is invalid")
                last_asked = self._last_asked.get(
                    (
                        receipt.subject_id,
                        receipt.actor_id,
                        receipt.role,
                        receipt.trigger.value,
                        candidate.concept_id,
                    )
                )
                if (
                    last_asked is not None
                    and receipt.issued_at
                    < last_asked + timedelta(hours=cooldown_hours)
                ):
                    raise ValueError(
                        "concurrent Habit question cooldown conflict"
                    )
            self._selections[receipt.selection_id] = receipt.model_copy(
                deep=True
            )
            self._episode_counts[key] = receipt.episode_question_count_after
            for candidate in receipt.candidates:
                self._last_asked[
                    (
                        receipt.subject_id,
                        receipt.actor_id,
                        receipt.role,
                        receipt.trigger.value,
                        candidate.concept_id,
                    )
                ] = receipt.issued_at

    def get_selection(
        self,
        selection_id: str,
    ) -> tuple[HabitQuestionSelectionReceipt, bool] | None:
        with self._lock:
            receipt = self._selections.get(selection_id)
            if receipt is None:
                return None
            return (
                receipt.model_copy(deep=True),
                selection_id in self._consumed_selections,
            )

    def finalize_capture(
        self,
        receipt: HabitQuestionSelectionReceipt,
        suppressions: Iterable[QuestionSuppression],
        *,
        now: datetime | None = None,
    ) -> None:
        del now
        staged = tuple(suppressions)
        with self._lock:
            stored = self._selections.get(receipt.selection_id)
            if stored is None or stored != receipt:
                raise ValueError("Habit selection receipt is forged or unknown")
            if receipt.selection_id in self._consumed_selections:
                raise ValueError("Habit selection receipt was already consumed")
            for item in staged:
                if (
                    item.subject_id != receipt.subject_id
                    or item.concept_id
                    not in {
                        candidate.concept_id
                        for candidate in receipt.candidates
                    }
                ):
                    raise ValueError(
                        "Question suppression is outside issued selection"
                    )
            for item in staged:
                self._suppressions[
                    (item.subject_id, item.concept_id, item.scope)
                ] = item.model_copy(deep=True)
            self._consumed_selections.add(receipt.selection_id)

    def list_active_suppressions(
        self,
        *,
        subject_id: str,
        now: datetime | None = None,
    ) -> tuple[QuestionSuppression, ...]:
        read_at = now or datetime.now(timezone.utc)
        with self._lock:
            return tuple(
                item.model_copy(deep=True)
                for (stored_subject, _, _), item in sorted(
                    self._suppressions.items()
                )
                if stored_subject == subject_id and item.expires_at > read_at
            )


class HabitQuestionnaireService:
    """Reviewed, receipt-bound progressive Habit question selection.

    This extends the existing Questionnaire capability; it is not an Agent and
    owns no long-term Profile state.
    """

    EPISODE_QUESTION_LIMIT = 3

    def __init__(
        self,
        concepts: Iterable[HabitConceptDefinition] | None = None,
        *,
        state_store: HabitQuestionnaireStateStore | None = None,
    ) -> None:
        source = list(concepts) if concepts is not None else list(DEFAULT_HABIT_CONCEPTS)
        self.concepts = {(item.concept_id, item.version): item for item in source}
        if len(self.concepts) != len(source):
            raise ValueError("duplicate Habit concept id/version")
        self.state_store = (
            state_store or InMemoryHabitQuestionnaireStateStore()
        )
        self._captured_answers: dict[str, CapturedHabitAnswer] = {}
        self._lock = RLock()

    def select(
        self,
        request: HabitQuestionSelectionRequest,
        *,
        now: datetime | None = None,
    ) -> HabitQuestionSelectionReceipt:
        with self._lock:
            return self._select_unlocked(request, now=now)

    def _select_unlocked(
        self,
        request: HabitQuestionSelectionRequest,
        *,
        now: datetime | None = None,
    ) -> HabitQuestionSelectionReceipt:
        selected_at = now or datetime.now(timezone.utc)
        if request.role == "doctor":
            raise PermissionError("doctor cannot answer Habit profile questions")
        if (
            request.trigger == HabitQuestionTrigger.OPTIONAL_LIGHT_INTAKE
            and request.role != "elder"
        ):
            raise PermissionError("optional Habit intake is elder-only")
        already_issued = self.state_store.episode_question_count(
            episode_id=request.episode_id,
            subject_id=request.subject_id,
        )
        actual_remaining = self.EPISODE_QUESTION_LIMIT - already_issued
        if request.remaining_episode_budget > actual_remaining:
            raise ValueError("request attempts to reset Episode Habit question budget")
        remaining = min(request.remaining_episode_budget, actual_remaining)
        active_suppressions = {
            item.concept_id
            for item in self.state_store.list_active_suppressions(
                subject_id=request.subject_id,
                now=selected_at,
            )
            if item.subject_id == request.subject_id
            and item.expires_at > selected_at
        }
        if request.candidate_concept_ids:
            known_ids = {item.concept_id for item in self.concepts.values()}
            unknown = set(request.candidate_concept_ids) - known_ids
            if unknown:
                raise ValueError("selection requested an unreviewed Habit concept")
        requested_ids = set(request.candidate_concept_ids)
        candidates: list[
            tuple[tuple[int, int, int, str], HabitQuestionCandidate]
        ] = []
        for definition in self.concepts.values():
            if definition.domain_review_status != "approved":
                continue
            if requested_ids and definition.concept_id not in requested_ids:
                continue
            if definition.concept_id in active_suppressions:
                continue
            if request.trigger not in definition.triggers:
                continue
            if (
                request.role == "family"
                and definition.respondent_rule != HabitRespondentRule.ELDER_OR_OBSERVER
            ):
                continue
            cooldown_key = (
                request.subject_id,
                request.actor_id,
                request.role,
                request.trigger.value,
                definition.concept_id,
            )
            last_asked = self.state_store.last_asked_at(
                subject_id=cooldown_key[0],
                actor_id=cooldown_key[1],
                role=cooldown_key[2],
                trigger=cooldown_key[3],
                concept_id=cooldown_key[4],
            )
            if (
                last_asked is not None
                and selected_at
                < last_asked + timedelta(hours=definition.cooldown_hours)
            ):
                continue
            state = request.concept_states.get(
                definition.concept_id, HabitConceptStatus.UNKNOWN
            )
            if (
                state == HabitConceptStatus.KNOWN
                and request.trigger
                not in {
                    HabitQuestionTrigger.EXPLICIT_HABIT_QUESTION,
                    HabitQuestionTrigger.EXPLICIT_PROFILE_REVIEW,
                }
            ):
                continue
            priority = {
                HabitConceptStatus.DISPUTED: 0,
                HabitConceptStatus.STALE: 1,
                HabitConceptStatus.UNKNOWN: 2,
                HabitConceptStatus.KNOWN: 3,
            }[state]
            burden = {
                HabitAnswerType.CHOICE: 0,
                HabitAnswerType.SCALE: 1,
                HabitAnswerType.BOUNDED_NUMBER: 2,
                HabitAnswerType.SHORT_TEXT: 3,
            }[definition.answer_type]
            intake_priority = (
                {
                    "habit.primary_goal": 0,
                    "habit.schedule_constraint": 1,
                    "habit.sleep_satisfaction_recent": 2,
                    "habit.nap_pattern": 3,
                }.get(definition.concept_id, 4)
                if request.trigger == HabitQuestionTrigger.OPTIONAL_LIGHT_INTAKE
                else 0
            )
            text = (
                definition.observer_text
                if request.role == "family"
                else definition.elder_text
            )
            candidate = HabitQuestionCandidate(
                concept_id=definition.concept_id,
                concept_version=definition.version,
                prompt_text=text or definition.canonical_question,
                answer_type=definition.answer_type,
                options=definition.options,
                unit=definition.unit,
                minimum=definition.minimum,
                maximum=definition.maximum,
                allowed_dispositions=definition.allowed_dispositions,
                respondent_rule=definition.respondent_rule,
                persistence=definition.persistence,
                trigger=request.trigger,
                decision_gap_ref=request.decision_gap_ref,
            )
            candidates.append(
                (
                    (
                        priority,
                        intake_priority,
                        burden,
                        definition.concept_id,
                    ),
                    candidate,
                )
            )
        count = min(remaining, request.max_questions)
        chosen = tuple(item for _, item in sorted(candidates)[:count])
        selection_id = (
            f"habit-selection:{request.episode_id}:{uuid4().hex}"
        )
        material = {
            "selection_id": selection_id,
            "request_id": request.request_id,
            "episode_id": request.episode_id,
            "subject_id": request.subject_id,
            "actor_id": request.actor_id,
            "role": request.role,
            "plan_id": request.plan_id,
            "plan_revision": request.plan_revision,
            "plan_step_id": request.plan_step_id,
            "trigger": request.trigger,
            "issued_at": selected_at,
            "expires_at": selected_at + timedelta(minutes=30),
            "episode_question_count_after": already_issued + len(chosen),
            "candidates": chosen,
        }
        unsigned = HabitQuestionSelectionReceipt(
            selection_hash="0" * 64,
            **material,
        )
        receipt = unsigned.model_copy(
            update={
                "selection_hash": _stable_hash(
                    _selection_hash_material(
                        unsigned.model_dump(exclude={"selection_hash"})
                    )
                )
            }
        )
        self.state_store.issue_selection(
            receipt,
            expected_question_count=already_issued,
            cooldown_hours_by_concept={
                candidate.concept_id: self.concepts[
                    (candidate.concept_id, candidate.concept_version)
                ].cooldown_hours
                for candidate in receipt.candidates
            },
        )
        return receipt

    def remaining_budget(self, *, episode_id: str, subject_id: str) -> int:
        with self._lock:
            return max(
                0,
                self.EPISODE_QUESTION_LIMIT
                - self.state_store.episode_question_count(
                    episode_id=episode_id,
                    subject_id=subject_id,
                ),
            )

    def capture(
        self,
        receipt: HabitQuestionSelectionReceipt,
        answers: Iterable[HabitQuestionAnswer],
        *,
        episode_id: str,
        subject_id: str,
        actor_id: str,
        role: str,
        suppression_confirmation_ref: str | None = None,
        now: datetime | None = None,
    ) -> HabitQuestionCapture:
        with self._lock:
            return self._capture_unlocked(
                receipt,
                answers,
                episode_id=episode_id,
                subject_id=subject_id,
                actor_id=actor_id,
                role=role,
                suppression_confirmation_ref=suppression_confirmation_ref,
                now=now,
            )

    def _capture_unlocked(
        self,
        receipt: HabitQuestionSelectionReceipt,
        answers: Iterable[HabitQuestionAnswer],
        *,
        episode_id: str,
        subject_id: str,
        actor_id: str,
        role: str,
        suppression_confirmation_ref: str | None = None,
        now: datetime | None = None,
    ) -> HabitQuestionCapture:
        captured_at = now or datetime.now(timezone.utc)
        stored_state = self.state_store.get_selection(receipt.selection_id)
        if stored_state is None or stored_state[0] != receipt:
            raise ValueError("Habit selection receipt is forged or unknown")
        if stored_state[1]:
            raise ValueError("Habit selection receipt was already consumed")
        if (
            receipt.episode_id,
            receipt.subject_id,
            receipt.actor_id,
            receipt.role,
        ) != (episode_id, subject_id, actor_id, role):
            raise ValueError("Habit selection receipt binding mismatch")
        if receipt.expires_at <= captured_at:
            raise ValueError("Habit selection receipt expired")
        material = receipt.model_dump(exclude={"selection_hash"})
        if receipt.selection_hash != _stable_hash(_selection_hash_material(material)):
            raise ValueError("Habit selection receipt hash mismatch")
        issued = {
            (item.concept_id, item.concept_version): item
            for item in receipt.candidates
        }
        captured: list[CapturedHabitAnswer] = []
        suppressions: list[QuestionSuppression] = []
        safety_events: list[HabitSafetyEvent] = []
        seen: set[tuple[str, str]] = set()
        for index, answer in enumerate(answers):
            key = (answer.concept_id, answer.concept_version)
            if key in seen:
                raise ValueError("duplicate answer for one Habit concept")
            seen.add(key)
            candidate = issued.get(key)
            definition = self.concepts.get(key)
            if candidate is None or definition is None:
                raise ValueError("answer does not belong to issued Habit selection")
            if answer.disposition not in candidate.allowed_dispositions:
                raise ValueError("answer disposition is not allowed")
            if role == "family" and (
                definition.respondent_rule != HabitRespondentRule.ELDER_OR_OBSERVER
            ):
                raise PermissionError("family cannot answer elder-only Habit concept")
            disposition = answer.disposition
            normalized = self._normalize_answer(definition, answer)
            if (
                role == "family"
                and definition.negative_answer_requires_observation_opportunity
                and normalized == "没有观察到"
                and (
                    answer.observation_opportunity is None
                    or not answer.observation_opportunity.present
                    or answer.observation_opportunity.confidence <= 0
                )
            ):
                disposition = HabitAnswerDisposition.UNKNOWN
                normalized = None
            answer_ref = (
                f"habit-answer:{episode_id}:{answer.concept_id}:{index + 1}"
            )
            if disposition == HabitAnswerDisposition.NEVER_ASK:
                if not suppression_confirmation_ref:
                    raise ValueError(
                        "never-ask requires a separate confirmation reference"
                    )
                suppressions.append(
                    QuestionSuppression(
                        suppression_id=f"suppress:{subject_id}:{answer.concept_id}",
                        subject_id=subject_id,
                        concept_id=answer.concept_id,
                        scope="profile_question",
                        confirmation_ref=suppression_confirmation_ref,
                        expires_at=captured_at + timedelta(days=365),
                    )
                )
            urgent_reason = (
                "habit_observed_breathing_signal"
                if definition.safety_route_enabled
                and normalized in {"观察到", "偶尔"}
                else _habit_response_risk_reason(normalized)
            )
            if urgent_reason:
                safety_events.append(
                    HabitSafetyEvent(
                        event_id=f"habit-safety:{answer_ref}",
                        answer_ref=answer_ref,
                        episode_id=episode_id,
                        subject_id=subject_id,
                        source_role=role,
                        reason_code=urgent_reason,
                        minimal_text=str(normalized)[:240],
                        captured_at=captured_at,
                        valid_until=captured_at + timedelta(days=30),
                    )
                )
            fact_dispositions = {
                HabitAnswerDisposition.ANSWERED,
                HabitAnswerDisposition.VARIABLE,
                HabitAnswerDisposition.NOT_APPLICABLE,
            }
            captured.append(
                CapturedHabitAnswer(
                    answer_ref=answer_ref,
                    episode_id=episode_id,
                    subject_id=subject_id,
                    actor_id=actor_id,
                    role=role,
                    concept_id=answer.concept_id,
                    concept_version=answer.concept_version,
                    disposition=disposition,
                    normalized_value=normalized,
                    origin_semantic=(
                        "elder_self_report"
                        if role == "elder"
                        else "family_observation"
                    ),
                    observation_date_start=answer.observation_date_start,
                    observation_date_end=answer.observation_date_end,
                    timezone_name=answer.timezone_name,
                    day_type=answer.day_type,
                    sleep_day_rule=answer.sleep_day_rule,
                    observation_opportunity=answer.observation_opportunity,
                    profile_candidate_eligible=(
                        disposition in fact_dispositions
                        and definition.persistence
                        == HabitPersistenceEligibility.PROFILE_ELIGIBLE
                        and urgent_reason is None
                        and not _habit_profile_forbidden_context(normalized)
                    ),
                    captured_at=captured_at,
                    episode_valid_until=captured_at + timedelta(hours=24),
                )
            )
            if urgent_reason:
                break
        result = HabitQuestionCapture(
            selection_id=receipt.selection_id,
            answers=tuple(captured),
            suppressions=tuple(suppressions),
            safety_events=tuple(safety_events),
            stop_remaining_questions=bool(safety_events),
        )
        self.state_store.finalize_capture(
            receipt,
            result.suppressions,
            now=captured_at,
        )
        for item in result.answers:
            self._captured_answers[item.answer_ref] = item
        return result

    def verify_captured_answer(
        self,
        answer: CapturedHabitAnswer,
        *,
        now: datetime | None = None,
    ) -> None:
        read_at = now or datetime.now(timezone.utc)
        with self._lock:
            stored = self._captured_answers.get(answer.answer_ref)
            if stored is None or stored != answer:
                raise ValueError("Habit answer is forged or was not captured")
            if answer.episode_valid_until <= read_at:
                raise ValueError("Habit answer expired")

    def get_captured_answer(
        self,
        answer_ref: str,
        *,
        subject_id: str,
        now: datetime | None = None,
    ) -> CapturedHabitAnswer:
        read_at = now or datetime.now(timezone.utc)
        with self._lock:
            answer = self._captured_answers.get(answer_ref)
            if answer is None or answer.subject_id != subject_id:
                raise KeyError("Habit answer is unavailable")
            if answer.episode_valid_until <= read_at:
                raise ValueError("Habit answer expired")
            return answer.model_copy(deep=True)

    def list_suppressions(
        self,
        *,
        subject_id: str,
        now: datetime | None = None,
    ) -> tuple[QuestionSuppression, ...]:
        read_at = now or datetime.now(timezone.utc)
        return self.state_store.list_active_suppressions(
            subject_id=subject_id,
            now=read_at,
        )

    @staticmethod
    def _normalize_answer(
        definition: HabitConceptDefinition,
        answer: HabitQuestionAnswer,
    ):
        if answer.disposition in {
            HabitAnswerDisposition.UNKNOWN,
            HabitAnswerDisposition.PREFER_NOT_TO_ANSWER,
            HabitAnswerDisposition.SKIPPED,
            HabitAnswerDisposition.NEVER_ASK,
            HabitAnswerDisposition.NOT_APPLICABLE,
        }:
            if answer.value is not None and answer.value != "":
                raise ValueError("non-answer disposition cannot carry a value")
            return None
        if answer.disposition == HabitAnswerDisposition.VARIABLE:
            if answer.value is not None and answer.value not in {"", "不固定"}:
                raise ValueError("variable disposition has a fixed canonical value")
            return "variable"
        if answer.value is None:
            raise ValueError("answered Habit question requires value")
        if definition.answer_type in {
            HabitAnswerType.CHOICE,
            HabitAnswerType.SCALE,
        }:
            value = str(answer.value).strip()
            if value not in definition.options:
                raise ValueError("Habit answer is outside reviewed options")
            return value
        if definition.answer_type == HabitAnswerType.BOUNDED_NUMBER:
            if isinstance(answer.value, bool) or not isinstance(
                answer.value, (int, float)
            ):
                raise ValueError("bounded Habit answer must be numeric")
            value = float(answer.value)
            if not definition.minimum <= value <= definition.maximum:
                raise ValueError("bounded Habit answer is outside reviewed range")
            return {"value": value, "unit": definition.unit}
        value = unicodedata.normalize("NFKC", str(answer.value)).strip()
        if not value or len(value) > definition.short_text_max_length:
            raise ValueError("short-text Habit answer length is invalid")
        return value


def _selection_hash_material(material: dict[str, object]) -> dict[str, object]:
    """Normalize receipt material so creation and validation hash identically."""

    normalized: dict[str, object] = {}
    for key, value in material.items():
        if isinstance(value, datetime):
            normalized[key] = value.isoformat()
        elif hasattr(value, "model_dump"):
            normalized[key] = value.model_dump(mode="json")
        elif isinstance(value, tuple):
            normalized[key] = [
                item.model_dump(mode="json")
                if hasattr(item, "model_dump")
                else getattr(item, "value", item)
                for item in value
            ]
        else:
            normalized[key] = getattr(value, "value", value)
    return normalized


def _stable_hash(value) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _habit_response_risk_reason(value) -> str | None:
    text = str(value or "").lower()
    urgent_markers = (
        "呼吸困难",
        "胸痛",
        "昏厥",
        "叫不醒",
        "跌倒受伤",
        "cannot breathe",
        "chest pain",
        "fainted",
    )
    if any(marker in text for marker in urgent_markers):
        return "urgent_response_boundary"
    return None


def _habit_profile_forbidden_context(value) -> bool:
    text = str(value or "").lower()
    return any(
        marker in text
        for marker in (
            "过敏",
            "疾病",
            "诊断",
            "用药",
            "药物",
            "剂量",
            "呼吸暂停",
            "跌倒",
            "psqi",
            "isi",
            "ess",
            "allergy",
            "diagnosis",
            "medication",
            "dose",
            "apnea",
        )
    )


__all__ = [
    "HABIT_QUESTIONNAIRE_VERSION",
    "HabitQuestionnaireService",
]
