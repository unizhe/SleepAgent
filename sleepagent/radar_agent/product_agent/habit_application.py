from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Literal, Protocol

from pydantic import Field, model_validator

from sleepagent.radar_agent.product_agent.contracts import (
    AuthenticatedBinding,
    EvidenceClaim,
    FactSnapshot,
    SourceScope,
    SourceScopeKind,
    StrictContract,
    ToolReceipt,
)
from sleepagent.radar_agent.product_agent.governance import (
    DeterministicCommitController,
)
from sleepagent.radar_agent.product_agent.habit_profile import (
    HabitChangeOperation,
    HabitProfileChangeCandidate,
    HabitProfileChangeSet,
    HabitProfileCommitReceipt,
    HabitProfileConfirmation,
    HabitProfileReadResult,
    evidence_claim_from_captured_answer,
)
from sleepagent.radar_agent.product_agent.habit_runtime import (
    HabitProfileRuntimeService,
)
from sleepagent.radar_agent.questionnaire import (
    CapturedHabitAnswer,
    HabitConceptStatus,
    HabitQuestionAnswer,
    HabitQuestionCapture,
    HabitQuestionSelectionReceipt,
    HabitQuestionSelectionRequest,
    HabitQuestionTrigger,
)


HABIT_APPLICATION_VERSION = "sleepagent-habit-application.v5"


class HabitInteractionStartRequest(StrictContract):
    episode_id: str
    trigger: HabitQuestionTrigger
    candidate_concept_ids: tuple[str, ...] = ()
    decision_gap_ref: str | None = None
    alternative_explanations: tuple[str, ...] = ()
    max_questions: int = Field(default=3, ge=1, le=3)
    profile_update_requested: bool = False

    @model_validator(mode="after")
    def allow_user_facing_triggers(self) -> "HabitInteractionStartRequest":
        if self.trigger not in {
            HabitQuestionTrigger.OPTIONAL_LIGHT_INTAKE,
            HabitQuestionTrigger.EXPLICIT_HABIT_QUESTION,
            HabitQuestionTrigger.EXPLICIT_PROFILE_REVIEW,
        }:
            raise ValueError(
                "decision-gap Habit triggers must come from ProductEpisodeRunner"
            )
        if (
            self.trigger == HabitQuestionTrigger.EXPLICIT_PROFILE_REVIEW
            and not self.profile_update_requested
        ):
            raise ValueError("Profile review must be display-only until update is chosen")
        return self


class HabitInteractionStartResponse(StrictContract):
    application_version: str = HABIT_APPLICATION_VERSION
    purpose_notice: str
    selection: HabitQuestionSelectionReceipt
    existing_profile: HabitProfileReadResult | None = None


class HabitAnswerSubmitRequest(StrictContract):
    selection: HabitQuestionSelectionReceipt
    answers: tuple[HabitQuestionAnswer, ...]
    suppression_confirmation_ref: str | None = None
    replace_fact_id_by_concept: dict[str, str] = Field(default_factory=dict)


class HabitPendingChangeSet(StrictContract):
    change_set: HabitProfileChangeSet
    confirmation_summary: tuple[dict[str, object], ...]


class HabitPendingChangeSetRecord(StrictContract):
    change_set: HabitProfileChangeSet
    fact_snapshot: FactSnapshot
    state: Literal["pending", "superseded", "committed"] = "pending"
    updated_at: datetime


class HabitPendingChangeSetStore(Protocol):
    def get(self, change_set_id: str) -> HabitPendingChangeSetRecord: ...

    def save(
        self, record: HabitPendingChangeSetRecord
    ) -> HabitPendingChangeSetRecord: ...


class InMemoryHabitPendingChangeSetStore:
    def __init__(self) -> None:
        self._items: dict[str, HabitPendingChangeSetRecord] = {}
        self._lock = RLock()

    def get(self, change_set_id: str) -> HabitPendingChangeSetRecord:
        with self._lock:
            try:
                return self._items[change_set_id].model_copy(deep=True)
            except KeyError as exc:
                raise KeyError(
                    f"Habit change set is unavailable: {change_set_id}"
                ) from exc

    def save(
        self, record: HabitPendingChangeSetRecord
    ) -> HabitPendingChangeSetRecord:
        with self._lock:
            self._items[record.change_set.change_set_id] = record.model_copy(
                deep=True
            )
        return record


class PersistentHabitPendingChangeSetStore:
    def __init__(self, store: Any) -> None:
        self._store = store

    def get(self, change_set_id: str) -> HabitPendingChangeSetRecord:
        (
            _subject_id,
            state,
            payload_json,
            snapshot_json,
            _expires_at,
            _created_at,
            updated_at,
        ) = self._store.get_pending_habit_change_set_row(change_set_id)
        return HabitPendingChangeSetRecord(
            change_set=HabitProfileChangeSet.model_validate_json(payload_json),
            fact_snapshot=FactSnapshot.model_validate_json(snapshot_json),
            state=state,
            updated_at=datetime.fromisoformat(updated_at),
        )

    def save(
        self, record: HabitPendingChangeSetRecord
    ) -> HabitPendingChangeSetRecord:
        changes = record.change_set
        self._store.save_pending_habit_change_set(
            change_set_id=changes.change_set_id,
            subject_id=changes.subject_id,
            state=record.state,
            payload_json=changes.model_dump_json(),
            snapshot_json=record.fact_snapshot.model_dump_json(),
            expires_at=changes.confirmation_expires_at,
            created_at=changes.created_at,
            updated_at=record.updated_at,
        )
        return record


class HabitAnswerSubmitResponse(StrictContract):
    application_version: str = HABIT_APPLICATION_VERSION
    capture: HabitQuestionCapture
    evidence_candidates: tuple[EvidenceClaim, ...] = ()
    pending_change_set: HabitPendingChangeSet | None = None
    next_status: Literal[
        "continue", "waiting_elder_confirmation", "safety_preempted"
    ]


class HabitObserverProposalRequest(StrictContract):
    episode_id: str
    answer_refs: tuple[str, ...] = Field(min_length=1, max_length=3)


class HabitForgetRequest(StrictContract):
    episode_id: str
    fact_id: str


class HabitChangeSetConfirmRequest(StrictContract):
    confirmation_id: str
    change_set_id: str
    change_set_version: int = Field(..., ge=1)
    manifest_hash: str = Field(..., min_length=64, max_length=64)
    idempotency_key: str


class HabitChangeSetPruneRequest(StrictContract):
    change_set_id: str
    candidate_ids_to_remove: tuple[str, ...] = Field(min_length=1, max_length=2)


class HabitCommitResponse(StrictContract):
    application_version: str = HABIT_APPLICATION_VERSION
    tool_receipt: ToolReceipt
    profile_receipt: HabitProfileCommitReceipt | None = None


class HabitProfileApplicationService:
    """Authenticated product facade over the shared Habit runtime and writer."""

    def __init__(
        self,
        *,
        runtime: HabitProfileRuntimeService | None = None,
        commit_controller: DeterministicCommitController | None = None,
        pending_store: HabitPendingChangeSetStore | None = None,
    ) -> None:
        self.runtime = runtime or HabitProfileRuntimeService()
        self.commit_controller = commit_controller or DeterministicCommitController(
            habit_profile_store=self.runtime.store
        )
        if self.commit_controller.habit_profile_store is not self.runtime.store:
            raise ValueError("Habit application requires one shared Profile authority")
        self.pending_store = pending_store or InMemoryHabitPendingChangeSetStore()
        self._lock = RLock()

    def start(
        self,
        request: HabitInteractionStartRequest,
        *,
        binding: AuthenticatedBinding,
        now: datetime | None = None,
    ) -> HabitInteractionStartResponse:
        issued_at = now or datetime.now(timezone.utc)
        if binding.role == "doctor":
            raise PermissionError("doctor cannot answer Habit questions")
        if (
            request.trigger == HabitQuestionTrigger.EXPLICIT_PROFILE_REVIEW
            and binding.role != "elder"
        ):
            raise PermissionError("only the elder may review/update their full Profile")
        existing: HabitProfileReadResult | None = None
        states: dict[str, HabitConceptStatus] = {}
        if request.trigger == HabitQuestionTrigger.EXPLICIT_PROFILE_REVIEW:
            existing = self.runtime.store.read(
                subject_id=binding.subject_id,
                actor_id=binding.actor_id,
                role="elder",
                authorization_scope=binding.authorization_scope,
                purpose="profile_review",
                requested_concept_ids=request.candidate_concept_ids,
                now=issued_at,
                include_stale_for_review=True,
            )
            states.update(
                {
                    item.concept_id: (
                        HabitConceptStatus.DISPUTED
                        if item.effective_status.value == "disputed"
                        else (
                            HabitConceptStatus.STALE
                            if item.effective_status.value == "stale"
                            else HabitConceptStatus.KNOWN
                        )
                    )
                    for item in existing.facts
                }
            )
            states.update(
                {
                    concept_id: HabitConceptStatus.STALE
                    for concept_id in existing.stale_concept_ids
                }
            )
        selection_request = HabitQuestionSelectionRequest(
            request_id=f"habit-api-request:{request.episode_id}",
            episode_id=request.episode_id,
            subject_id=binding.subject_id,
            actor_id=binding.actor_id,
            role=binding.role,
            plan_id=f"habit-user-initiated-plan:{request.episode_id}",
            plan_revision=0,
            plan_step_id="user-initiated-progressive-habit-question",
            trigger=request.trigger,
            decision_kind=(
                "optional_intake"
                if request.trigger == HabitQuestionTrigger.OPTIONAL_LIGHT_INTAKE
                else (
                    "profile_review"
                    if request.trigger
                    == HabitQuestionTrigger.EXPLICIT_PROFILE_REVIEW
                    else "evidence"
                )
            ),
            decision_gap_ref=request.decision_gap_ref,
            concept_states=states,
            alternative_explanations=request.alternative_explanations,
            candidate_concept_ids=request.candidate_concept_ids,
            remaining_episode_budget=self.runtime.questionnaire.remaining_budget(
                episode_id=request.episode_id,
                subject_id=binding.subject_id,
            ),
            max_questions=request.max_questions,
            profile_update_requested=request.profile_update_requested,
        )
        receipt = self.runtime.questionnaire.select(
            selection_request, now=issued_at
        )
        return HabitInteractionStartResponse(
            purpose_notice=(
                "这些问题都可以跳过，只用于当前个性化；"
                "是否长期保存会在回答后另行整体确认。"
            ),
            selection=receipt,
            existing_profile=existing,
        )

    def submit(
        self,
        request: HabitAnswerSubmitRequest,
        *,
        binding: AuthenticatedBinding,
        now: datetime | None = None,
    ) -> HabitAnswerSubmitResponse:
        captured_at = now or datetime.now(timezone.utc)
        capture = self.runtime.questionnaire.capture(
            request.selection,
            request.answers,
            episode_id=request.selection.episode_id,
            subject_id=binding.subject_id,
            actor_id=binding.actor_id,
            role=binding.role,
            suppression_confirmation_ref=request.suppression_confirmation_ref,
            now=captured_at,
        )
        evidence = tuple(
            evidence_claim_from_captured_answer(item) for item in capture.answers
        )
        if capture.safety_events:
            return HabitAnswerSubmitResponse(
                capture=capture,
                evidence_candidates=evidence,
                next_status="safety_preempted",
            )
        if binding.role != "elder":
            return HabitAnswerSubmitResponse(
                capture=capture,
                evidence_candidates=evidence,
                next_status="continue",
            )
        eligible = tuple(
            item for item in capture.answers if item.profile_candidate_eligible
        )
        pending = (
            self._build_pending(
                episode_id=request.selection.episode_id,
                answers=eligible,
                binding=binding,
                replace_fact_id_by_concept=request.replace_fact_id_by_concept,
                now=captured_at,
            )
            if eligible
            else None
        )
        return HabitAnswerSubmitResponse(
            capture=capture,
            evidence_candidates=evidence,
            pending_change_set=pending,
            next_status=(
                "waiting_elder_confirmation" if pending else "continue"
            ),
        )

    def build_observer_proposal(
        self,
        request: HabitObserverProposalRequest,
        *,
        binding: AuthenticatedBinding,
        now: datetime | None = None,
    ) -> HabitPendingChangeSet:
        built_at = now or datetime.now(timezone.utc)
        self._require_elder(binding)
        answers = tuple(
            self.runtime.questionnaire.get_captured_answer(
                ref, subject_id=binding.subject_id, now=built_at
            )
            for ref in request.answer_refs
        )
        if any(item.role != "family" for item in answers):
            raise ValueError("observer proposal accepts only family observations")
        return self._build_pending(
            episode_id=request.episode_id,
            answers=answers,
            binding=binding,
            replace_fact_id_by_concept={},
            now=built_at,
        )

    def read_profile(
        self,
        *,
        binding: AuthenticatedBinding,
        purpose: Literal[
            "evidence",
            "care",
            "profile_review",
            "doctor_material",
            "family_coordination",
        ],
        requested_concept_ids: tuple[str, ...],
        include_stale_for_review: bool = False,
        now: datetime | None = None,
    ) -> HabitProfileReadResult:
        return self.runtime.store.read(
            subject_id=binding.subject_id,
            actor_id=binding.actor_id,
            role=self._human_role(binding.role),
            authorization_scope=binding.authorization_scope,
            purpose=purpose,
            requested_concept_ids=requested_concept_ids,
            now=now,
            include_stale_for_review=include_stale_for_review,
        )

    def request_forget(
        self,
        request: HabitForgetRequest,
        *,
        binding: AuthenticatedBinding,
        now: datetime | None = None,
    ) -> HabitPendingChangeSet:
        built_at = now or datetime.now(timezone.utc)
        self._require_elder(binding)
        state = self.runtime.store.get(binding.subject_id)
        target = next(
            (item for item in state.facts if item.fact_id == request.fact_id),
            None,
        )
        if target is None or target.effective_status(built_at).value == "inactive":
            raise KeyError("Habit fact is unavailable")
        snapshot = self._snapshot(binding, now=built_at)
        candidate = HabitProfileChangeCandidate.create(
            candidate_id=f"habit-forget:{target.fact_id}",
            operation=HabitChangeOperation.FORGET,
            subject_id=binding.subject_id,
            concept_id=target.concept_id,
            concept_version=target.concept_version,
            replace_fact_id=target.fact_id,
        )
        changes = HabitProfileChangeSet.create(
            change_set_id=f"habit-change-set:{request.episode_id}:forget",
            version=1,
            subject_id=binding.subject_id,
            candidates=(candidate,),
            fact_snapshot_hash=snapshot.fact_snapshot_hash,
            expected_memory_version=snapshot.memory_context_version,
            confirmation_expires_at=built_at + timedelta(minutes=30),
            created_at=built_at,
        )
        return self._remember_pending(changes, snapshot)

    def confirm(
        self,
        request: HabitChangeSetConfirmRequest,
        *,
        binding: AuthenticatedBinding,
        now: datetime | None = None,
    ) -> HabitCommitResponse:
        committed_at = now or datetime.now(timezone.utc)
        self._require_elder(binding)
        with self._lock:
            try:
                record = self.pending_store.get(request.change_set_id)
            except KeyError as exc:
                raise KeyError("Habit change set is unavailable") from exc
            changes, snapshot = record.change_set, record.fact_snapshot
            if record.state == "superseded":
                raise ValueError(
                    "Habit change set was superseded and requires new confirmation"
                )
            if (
                changes.version,
                changes.manifest_hash,
            ) != (
                request.change_set_version,
                request.manifest_hash,
            ):
                raise ValueError("Habit confirmation manifest/version mismatch")
            if snapshot.binding != binding:
                raise PermissionError("Habit confirmation authentication changed")
            confirmation = HabitProfileConfirmation(
                confirmation_id=request.confirmation_id,
                actor_id=binding.actor_id,
                actor_role="elder",
                subject_id=binding.subject_id,
                action_scope="write_habit_profile",
                change_set_id=changes.change_set_id,
                change_set_version=changes.version,
                manifest_hash=changes.manifest_hash,
                expires_at=changes.confirmation_expires_at,
            )
            receipt = self.commit_controller.commit_habit_profile(
                change_set=changes,
                confirmation=confirmation,
                fact_snapshot=snapshot,
                idempotency_key=request.idempotency_key,
                now=committed_at,
            )
            profile_receipt = (
                HabitProfileCommitReceipt.model_validate(receipt.output)
                if receipt.output
                else None
            )
            self.pending_store.save(
                record.model_copy(
                    update={"state": "committed", "updated_at": committed_at}
                )
            )
            return HabitCommitResponse(
                tool_receipt=receipt,
                profile_receipt=profile_receipt,
            )

    def prune_change_set(
        self,
        request: HabitChangeSetPruneRequest,
        *,
        binding: AuthenticatedBinding,
        now: datetime | None = None,
    ) -> HabitPendingChangeSet:
        revised_at = now or datetime.now(timezone.utc)
        self._require_elder(binding)
        with self._lock:
            try:
                record = self.pending_store.get(request.change_set_id)
            except KeyError as exc:
                raise KeyError("Habit change set is unavailable") from exc
            changes, snapshot = record.change_set, record.fact_snapshot
            if record.state == "superseded":
                raise ValueError("Habit change set was already superseded")
            if record.state == "committed":
                raise ValueError("Habit change set was already committed")
            if snapshot.binding != binding:
                raise PermissionError("Habit change-set authentication changed")
            requested = set(request.candidate_ids_to_remove)
            known = {item.candidate_id for item in changes.candidates}
            if not requested.issubset(known):
                raise ValueError("Habit change-set removal names unknown candidate")
            revised = changes.without_candidates(
                requested,
                new_change_set_id=(
                    f"{changes.change_set_id}:revised:{int(revised_at.timestamp())}"
                ),
                created_at=revised_at,
            )
            self.pending_store.save(
                record.model_copy(
                    update={"state": "superseded", "updated_at": revised_at}
                )
            )
            return self._remember_pending(revised, snapshot)

    def _build_pending(
        self,
        *,
        episode_id: str,
        answers: tuple[CapturedHabitAnswer, ...],
        binding: AuthenticatedBinding,
        replace_fact_id_by_concept: dict[str, str],
        now: datetime,
    ) -> HabitPendingChangeSet:
        self._require_elder(binding)
        if not 1 <= len(answers) <= 3:
            raise ValueError("Habit change set requires 1-3 eligible answers")
        candidates = []
        for answer in answers:
            self.runtime.questionnaire.verify_captured_answer(answer, now=now)
            if answer.subject_id != binding.subject_id:
                raise PermissionError("Habit answer crosses subject")
            if answer.role == "elder" and answer.actor_id != binding.actor_id:
                raise PermissionError("elder Habit answer actor mismatch")
            replace_fact_id = replace_fact_id_by_concept.get(answer.concept_id)
            candidates.append(
                self.runtime.candidate_builder.from_captured(
                    answer,
                    operation=(
                        HabitChangeOperation.REPLACE
                        if replace_fact_id
                        else HabitChangeOperation.CREATE
                    ),
                    replace_fact_id=replace_fact_id,
                )
            )
        snapshot = self._snapshot(binding, now=now)
        changes = HabitProfileChangeSet.create(
            change_set_id=f"habit-change-set:{episode_id}:pending",
            version=1,
            subject_id=binding.subject_id,
            candidates=tuple(candidates),
            fact_snapshot_hash=snapshot.fact_snapshot_hash,
            expected_memory_version=snapshot.memory_context_version,
            confirmation_expires_at=now + timedelta(minutes=30),
            created_at=now,
        )
        return self._remember_pending(changes, snapshot)

    def _remember_pending(
        self,
        changes: HabitProfileChangeSet,
        snapshot: FactSnapshot,
    ) -> HabitPendingChangeSet:
        with self._lock:
            try:
                prior = self.pending_store.get(changes.change_set_id)
            except KeyError:
                prior = None
            if (
                prior is not None
                and prior.change_set.manifest_hash != changes.manifest_hash
            ):
                values = changes.model_dump(exclude={"manifest_hash"})
                values["candidates"] = changes.candidates
                values["change_set_id"] = (
                    f"{changes.change_set_id}:{changes.manifest_hash[:12]}"
                )
                changes = HabitProfileChangeSet.create(
                    **values
                )
            self.pending_store.save(
                HabitPendingChangeSetRecord(
                    change_set=changes,
                    fact_snapshot=snapshot,
                    state="pending",
                    updated_at=changes.created_at,
                )
            )
        return HabitPendingChangeSet(
            change_set=changes,
            confirmation_summary=tuple(
                {
                    "candidate_id": item.candidate_id,
                    "operation": item.operation.value,
                    "concept_id": item.concept_id,
                    "value": item.value,
                    "origin_semantic": item.origin_semantic,
                    "observation_date_start": item.observation_date_start,
                    "observation_date_end": item.observation_date_end,
                }
                for item in changes.candidates
            ),
        )

    def _snapshot(
        self,
        binding: AuthenticatedBinding,
        *,
        now: datetime,
    ) -> FactSnapshot:
        memory_version = self.runtime.store.get(binding.subject_id).version
        return FactSnapshot.create(
            fact_snapshot_id=(
                f"habit-profile-snapshot:{binding.subject_id}:"
                f"{memory_version}:{int(now.timestamp() * 1_000_000)}"
            ),
            binding=binding,
            source_scope=SourceScope(
                kind=SourceScopeKind.GENERAL_KNOWLEDGE,
                as_of=now,
                timezone_name="UTC",
            ),
            canonical_data_version="habit-profile-application.v1",
            memory_context_version=memory_version,
            created_at=now,
        )

    @staticmethod
    def _require_elder(binding: AuthenticatedBinding) -> None:
        if binding.role != "elder":
            raise PermissionError("only the authenticated elder may change Profile")

    @staticmethod
    def _human_role(role: str) -> Literal["elder", "family", "doctor"]:
        if role not in {"elder", "family", "doctor"}:
            raise PermissionError("system role cannot use Habit Profile UI")
        return role


__all__ = [
    "HABIT_APPLICATION_VERSION",
    "HabitAnswerSubmitRequest",
    "HabitAnswerSubmitResponse",
    "HabitChangeSetConfirmRequest",
    "HabitChangeSetPruneRequest",
    "HabitCommitResponse",
    "HabitForgetRequest",
    "HabitInteractionStartRequest",
    "HabitInteractionStartResponse",
    "HabitObserverProposalRequest",
    "HabitPendingChangeSet",
    "HabitProfileApplicationService",
]
