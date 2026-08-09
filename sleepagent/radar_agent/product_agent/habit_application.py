from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Literal, Protocol

from pydantic import Field, model_validator

from sleepagent.radar_agent.product_agent.contracts import (
    AuthenticatedBinding,
    EvidenceClaim,
    FactSnapshot,
    InvocationOutcome,
    SourceScope,
    SourceScopeKind,
    StrictContract,
    ToolReceipt,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.governance import (
    DeterministicCommitController,
)
from sleepagent.radar_agent.product_agent.habit_profile import (
    HabitChangeOperation,
    HabitProfileChangeCandidate,
    HabitProfileChangeSet,
    HabitProfileCommitReceipt,
    HabitProfileReadResult,
    evidence_claim_from_captured_answer,
)
from sleepagent.radar_agent.product_agent.habit_runtime import (
    HabitProfileRuntimeService,
)
from sleepagent.radar_agent.product_agent.hitl import (
    HITL_POLICY_VERSION,
    ActionProposal,
    DecisionExplanation,
    HumanDecisionChoice,
    HumanDecisionService,
    HumanDecisionStatus,
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


HABIT_APPLICATION_VERSION = "sleepagent-habit-application.v7"
HABIT_PENDING_PAYLOAD_VERSION = "sleepagent-habit-pending.v2"


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
    replace_fact_id_by_concept: dict[str, str] = Field(default_factory=dict)


class HabitPendingChangeSet(StrictContract):
    decision_id: str = Field(..., min_length=1)
    change_set: HabitProfileChangeSet
    confirmation_summary: tuple[dict[str, object], ...]


class HabitPendingChangeSetRecord(StrictContract):
    decision_id: str = Field(..., min_length=1)
    change_set: HabitProfileChangeSet
    fact_snapshot: FactSnapshot
    state: Literal[
        "pending",
        "superseded",
        "committed",
        "execution_failed",
        "outcome_unknown",
    ] = "pending"
    execution_idempotency_key: str | None = None
    updated_at: datetime


class _PersistentHabitPendingPayload(StrictContract):
    schema_version: Literal["sleepagent-habit-pending.v2"] = (
        HABIT_PENDING_PAYLOAD_VERSION
    )
    decision_id: str = Field(..., min_length=1)
    change_set: HabitProfileChangeSet
    execution_idempotency_key: str | None = None


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
        try:
            payload = _PersistentHabitPendingPayload.model_validate_json(
                payload_json
            )
        except ValueError as exc:
            # Rows created before Habit/HITL unification have no authoritative
            # decision binding and therefore cannot be confirmed.
            raise KeyError(
                f"Habit change set requires a new decision: {change_set_id}"
            ) from exc
        return HabitPendingChangeSetRecord(
            decision_id=payload.decision_id,
            change_set=payload.change_set,
            fact_snapshot=FactSnapshot.model_validate_json(snapshot_json),
            state=state,
            execution_idempotency_key=payload.execution_idempotency_key,
            updated_at=(
                updated_at
                if isinstance(updated_at, datetime)
                else datetime.fromisoformat(updated_at)
            ),
        )

    def save(
        self, record: HabitPendingChangeSetRecord
    ) -> HabitPendingChangeSetRecord:
        changes = record.change_set
        payload = _PersistentHabitPendingPayload(
            decision_id=record.decision_id,
            change_set=changes,
            execution_idempotency_key=record.execution_idempotency_key,
        )
        self._store.save_pending_habit_change_set(
            change_set_id=changes.change_set_id,
            subject_id=changes.subject_id,
            state=record.state,
            payload_json=payload.model_dump_json(),
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
    decision_id: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)


class HabitChangeSetPruneRequest(StrictContract):
    change_set_id: str
    candidate_ids_to_remove: tuple[str, ...] = Field(min_length=1, max_length=2)


class HabitCommitResponse(StrictContract):
    application_version: str = HABIT_APPLICATION_VERSION
    decision_id: str
    tool_receipt: ToolReceipt
    profile_receipt: HabitProfileCommitReceipt | None = None


class HabitProfileApplicationService:
    """Authenticated product facade over the shared Habit runtime and writer."""

    def __init__(
        self,
        *,
        human_decisions: HumanDecisionService,
        runtime: HabitProfileRuntimeService,
        commit_controller: DeterministicCommitController,
        pending_store: HabitPendingChangeSetStore,
    ) -> None:
        self.runtime = runtime
        self.human_decisions = human_decisions
        self.commit_controller = commit_controller
        if self.commit_controller.habit_profile_store is not self.runtime.store:
            raise ValueError("Habit application requires one shared Profile authority")
        self.pending_store = pending_store
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
        interaction_hash = stable_hash(
            {
                "request": request.model_dump(mode="json"),
                "binding": binding.model_dump(mode="json"),
            }
        )
        selection_request = HabitQuestionSelectionRequest(
            request_id=(
                f"habit-api-request:{request.episode_id}:"
                f"{interaction_hash}"
            ),
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
        return self._remember_pending(
            changes,
            snapshot,
            episode_id=request.episode_id,
        )

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
            decision = self.human_decisions.get(request.decision_id)
            proposal = decision.proposal
            if (
                proposal.subject_id != binding.subject_id
                or proposal.proposer_actor_id != binding.actor_id
            ):
                raise PermissionError("Habit decision authentication changed")
            try:
                record = self.pending_store.get(proposal.target_id)
            except KeyError as exc:
                raise KeyError("Habit change set is unavailable") from exc
            changes, snapshot = record.change_set, record.fact_snapshot
            self._validate_decision_binding(
                decision_id=request.decision_id,
                proposal=proposal,
                record=record,
            )
            if snapshot.binding != binding:
                raise PermissionError("Habit decision authentication changed")

            if record.state == "committed":
                if (
                    decision.status != HumanDecisionStatus.COMMITTED
                    or record.execution_idempotency_key
                    != request.idempotency_key
                ):
                    raise ValueError("Habit commit replay binding mismatch")
                receipt = self.commit_controller.get_commit_receipt(
                    request.idempotency_key
                )
                if receipt is None:
                    raise ValueError("Habit commit receipt is unavailable")
                return self._commit_response(
                    decision_id=request.decision_id,
                    receipt=receipt,
                    record=record,
                )
            if record.state != "pending":
                raise ValueError(
                    f"Habit change set is {record.state.replace('_', ' ')}"
                )

            if decision.status == HumanDecisionStatus.PENDING:
                decision = self.human_decisions.decide(
                    request.decision_id,
                    actor_id=binding.actor_id,
                    actor_role="elder",
                    choice=HumanDecisionChoice.APPROVE,
                    target_hash=changes.manifest_hash,
                    reason="Authenticated elder confirmed the exact Habit manifest.",
                    now=committed_at,
                )
            if decision.status not in {
                HumanDecisionStatus.APPROVED,
                HumanDecisionStatus.EXECUTING,
                HumanDecisionStatus.COMMITTED,
            }:
                raise ValueError(
                    f"Habit decision is {decision.status.value}"
                )
            if (
                record.execution_idempotency_key is not None
                and record.execution_idempotency_key
                != request.idempotency_key
            ):
                raise ValueError("Habit decision execution binding mismatch")
            if decision.status in {
                HumanDecisionStatus.EXECUTING,
                HumanDecisionStatus.COMMITTED,
            } and (
                decision.active_grant is None
                or decision.active_grant.idempotency_key
                != request.idempotency_key
            ):
                raise ValueError("Habit decision execution binding mismatch")

            capability = self.human_decisions.acquire_verified_capability(
                request.decision_id,
                expected_proposal_id=proposal.proposal_id,
                expected_subject_id=changes.subject_id,
                expected_target_id=changes.change_set_id,
                expected_target_hash=changes.manifest_hash,
                expected_action_scope=changes.action_scope,
                expected_fact_snapshot_id=snapshot.fact_snapshot_id,
                expected_fact_snapshot_hash=snapshot.fact_snapshot_hash,
                expected_policy_version=proposal.policy_version,
                idempotency_key=request.idempotency_key,
                now=committed_at,
            )
            execution_at = max(committed_at, capability.grant.issued_at)
            record = self.pending_store.save(
                record.model_copy(
                    update={
                        "execution_idempotency_key": request.idempotency_key,
                        "updated_at": execution_at,
                    }
                )
            )
            try:
                receipt = self.commit_controller.commit_habit_profile(
                    change_set=changes,
                    approval_capability=capability,
                    fact_snapshot=snapshot,
                    idempotency_key=request.idempotency_key,
                    now=execution_at,
                )
            except Exception as exc:
                self.human_decisions.record_execution_result(
                    capability,
                    status=HumanDecisionStatus.EXECUTION_FAILED,
                    failure_reason=str(exc)[:1200],
                    now=execution_at,
                )
                self.pending_store.save(
                    record.model_copy(
                        update={
                            "state": "execution_failed",
                            "updated_at": execution_at,
                        }
                    )
                )
                raise

            if receipt.outcome == InvocationOutcome.SUCCEEDED:
                authority_status = HumanDecisionStatus.COMMITTED
                pending_state = "committed"
            elif receipt.outcome == InvocationOutcome.UNKNOWN:
                authority_status = HumanDecisionStatus.OUTCOME_UNKNOWN
                pending_state = "outcome_unknown"
            else:
                authority_status = HumanDecisionStatus.EXECUTION_FAILED
                pending_state = "execution_failed"
            self.human_decisions.record_execution_result(
                capability,
                status=authority_status,
                receipt_ref=receipt.tool_invocation_id,
                failure_reason=(
                    receipt.error_code
                    if authority_status != HumanDecisionStatus.COMMITTED
                    else None
                ),
                now=receipt.observed_at,
            )
            self.pending_store.save(
                record.model_copy(
                    update={"state": pending_state, "updated_at": receipt.observed_at}
                )
            )
            return self._commit_response(
                decision_id=request.decision_id,
                receipt=receipt,
                record=record,
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
            if record.state != "pending":
                raise ValueError(
                    f"Habit change set is {record.state.replace('_', ' ')}"
                )
            if snapshot.binding != binding:
                raise PermissionError("Habit change-set authentication changed")
            decision = self.human_decisions.get(record.decision_id)
            self._validate_decision_binding(
                decision_id=record.decision_id,
                proposal=decision.proposal,
                record=record,
            )
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
            self.human_decisions.revoke(
                record.decision_id,
                actor_id=binding.actor_id,
                actor_role="elder",
                now=revised_at,
            )
            self.pending_store.save(
                record.model_copy(
                    update={"state": "superseded", "updated_at": revised_at}
                )
            )
            return self._remember_pending(
                revised,
                snapshot,
                episode_id=decision.proposal.episode_id,
            )

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
        return self._remember_pending(
            changes,
            snapshot,
            episode_id=episode_id,
        )

    def _remember_pending(
        self,
        changes: HabitProfileChangeSet,
        snapshot: FactSnapshot,
        *,
        episode_id: str,
    ) -> HabitPendingChangeSet:
        with self._lock:
            try:
                prior = self.pending_store.get(changes.change_set_id)
            except KeyError:
                prior = None
            if (
                prior is not None
                and prior.change_set.manifest_hash == changes.manifest_hash
            ):
                if prior.state != "pending":
                    raise ValueError(
                        f"Habit change set is {prior.state.replace('_', ' ')}"
                    )
                decision = self.human_decisions.get(prior.decision_id)
                self._validate_decision_binding(
                    decision_id=prior.decision_id,
                    proposal=decision.proposal,
                    record=prior,
                )
                return self._pending_response(prior)
            if (
                prior is not None
                and prior.change_set.manifest_hash != changes.manifest_hash
            ):
                values = changes.model_dump(exclude={"manifest_hash"})
                values["candidates"] = changes.candidates
                values["change_set_id"] = (
                    f"{changes.change_set_id}:{changes.manifest_hash[:12]}"
                )
                changes = HabitProfileChangeSet.create(**values)
            decision = self.human_decisions.create(
                ActionProposal(
                    proposal_id=(
                        f"habit-proposal:{changes.change_set_id}:"
                        f"{changes.manifest_hash[:16]}"
                    ),
                    episode_id=episode_id,
                    subject_id=changes.subject_id,
                    proposer_actor_id=snapshot.binding.actor_id,
                    action_kind="habit_profile",
                    action_scope=changes.action_scope,
                    target_id=changes.change_set_id,
                    target_hash=changes.manifest_hash,
                    fact_snapshot_id=snapshot.fact_snapshot_id,
                    fact_snapshot_hash=snapshot.fact_snapshot_hash,
                    policy_version=HITL_POLICY_VERSION,
                    payload={
                        "change_set": changes.model_dump(mode="json"),
                    },
                    explanation=DecisionExplanation(
                        what_will_change=(
                            "The reviewed Habit Profile manifest will be written "
                            "to the elder's confirmed profile."
                        ),
                        why_now=(
                            "The elder requested a profile update based on the "
                            "answers shown in this review."
                        ),
                        who_will_receive_or_be_affected=changes.subject_id,
                        duration_or_frequency=(
                            "The profile remains active until it expires, is "
                            "replaced, or is forgotten."
                        ),
                        how_to_revoke=(
                            "Cancel this pending decision or use the Habit forget "
                            "flow after commit."
                        ),
                        exact_changes=[
                            f"{item.operation.value}:{item.concept_id}:"
                            f"{item.candidate_id}"
                            for item in changes.candidates
                        ],
                    ),
                    created_at=changes.created_at,
                    expires_at=changes.confirmation_expires_at,
                    metadata={
                        "change_set_version": changes.version,
                        "candidate_count": len(changes.candidates),
                    },
                )
            )
            record = self.pending_store.save(
                HabitPendingChangeSetRecord(
                    decision_id=decision.decision_id,
                    change_set=changes,
                    fact_snapshot=snapshot,
                    state="pending",
                    updated_at=changes.created_at,
                )
            )
        return self._pending_response(record)

    @staticmethod
    def _confirmation_summary(
        changes: HabitProfileChangeSet,
    ) -> tuple[dict[str, object], ...]:
        return tuple(
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
        )

    @classmethod
    def _pending_response(
        cls,
        record: HabitPendingChangeSetRecord,
    ) -> HabitPendingChangeSet:
        return HabitPendingChangeSet(
            decision_id=record.decision_id,
            change_set=record.change_set,
            confirmation_summary=cls._confirmation_summary(record.change_set),
        )

    @staticmethod
    def _validate_decision_binding(
        *,
        decision_id: str,
        proposal: ActionProposal,
        record: HabitPendingChangeSetRecord,
    ) -> None:
        changes = record.change_set
        snapshot = record.fact_snapshot
        expected = (
            record.decision_id,
            None,
            snapshot.binding.actor_id,
            "habit_profile",
            changes.subject_id,
            changes.change_set_id,
            changes.manifest_hash,
            changes.action_scope,
            snapshot.fact_snapshot_id,
            snapshot.fact_snapshot_hash,
            changes.created_at,
            changes.confirmation_expires_at,
            changes.model_dump(mode="json"),
        )
        actual = (
            decision_id,
            proposal.task_id,
            proposal.proposer_actor_id,
            proposal.action_kind,
            proposal.subject_id,
            proposal.target_id,
            proposal.target_hash,
            proposal.action_scope,
            proposal.fact_snapshot_id,
            proposal.fact_snapshot_hash,
            proposal.created_at,
            proposal.expires_at,
            proposal.payload.get("change_set"),
        )
        if expected != actual:
            raise ValueError("Habit decision/change-set binding mismatch")
        if changes.fact_snapshot_hash != snapshot.fact_snapshot_hash:
            raise ValueError("Habit change set FactSnapshot mismatch")

    @staticmethod
    def _commit_response(
        *,
        decision_id: str,
        receipt: ToolReceipt,
        record: HabitPendingChangeSetRecord,
    ) -> HabitCommitResponse:
        snapshot = record.fact_snapshot
        changes = record.change_set
        if (
            receipt.tool_name != "state.commit_habit_profile"
            or receipt.idempotency_key != record.execution_idempotency_key
            or receipt.fact_snapshot_id != snapshot.fact_snapshot_id
            or receipt.fact_snapshot_hash != snapshot.fact_snapshot_hash
            or f"human-decision:{decision_id}" not in receipt.source_refs
        ):
            raise ValueError("Habit commit receipt binding mismatch")
        profile_receipt = (
            HabitProfileCommitReceipt.model_validate(receipt.output)
            if receipt.output
            else None
        )
        if profile_receipt is not None and (
            profile_receipt.change_set_id,
            profile_receipt.manifest_hash,
            profile_receipt.subject_id,
            profile_receipt.idempotency_key,
        ) != (
            changes.change_set_id,
            changes.manifest_hash,
            changes.subject_id,
            record.execution_idempotency_key,
        ):
            raise ValueError("Habit Profile receipt binding mismatch")
        return HabitCommitResponse(
            decision_id=decision_id,
            tool_receipt=receipt,
            profile_receipt=profile_receipt,
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
