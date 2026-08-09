from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from threading import RLock
from typing import Any, Iterable, Literal, Protocol

from pydantic import Field, model_validator

from sleepagent.radar_agent.product_agent.contracts import (
    EvidenceClaim,
    EvidenceSemantic,
    EvidenceSourceKind,
    FrozenContract,
    StrictContract,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.hitl import (
    ApprovalGrant,
    VerifiedApprovalCapability,
)
from sleepagent.radar_agent.questionnaire import (
    DEFAULT_HABIT_CONCEPTS,
    CapturedHabitAnswer,
    HabitAnswerDisposition,
    HabitConceptDefinition,
    HabitPersistenceEligibility,
    ObservationOpportunity,
)


HABIT_PROFILE_VERSION = "sleepagent-habit-profile.v4"


class BaselineMaturity(str, Enum):
    UNAVAILABLE = "unavailable"
    PROVISIONAL = "provisional"
    ESTABLISHED = "established"


class HabitFactState(str, Enum):
    CONFIRMED = "confirmed"
    STALE = "stale"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"


class HabitEffectiveStatus(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    DISPUTED = "disputed"
    INACTIVE = "inactive"


class NightOutOfBedBaselineValue(StrictContract):
    events_per_valid_night: float = Field(..., ge=0)
    usual_event_count_range: tuple[int, int]
    usual_local_time_windows: tuple[str, ...] = ()
    median_duration_minutes: float = Field(..., ge=0)
    p90_duration_minutes: float = Field(..., ge=0)
    nights_with_events: int = Field(..., ge=0)
    recent_change: Literal[
        "stable", "increasing", "decreasing", "unknown"
    ] = "unknown"

    @model_validator(mode="after")
    def validate_distribution(self) -> "NightOutOfBedBaselineValue":
        lower, upper = self.usual_event_count_range
        if lower < 0 or lower > upper:
            raise ValueError("night-out-of-bed count range is invalid")
        if self.p90_duration_minutes < self.median_duration_minutes:
            raise ValueError("p90 duration cannot be below median duration")
        return self


class HabitChangeOperation(str, Enum):
    CREATE = "create"
    REPLACE = "replace"
    EXPIRE = "expire"
    FORGET = "forget"


class ObjectiveBaselineArtifact(FrozenContract):
    artifact_id: str
    subject_id: str
    metric_id: str
    window_start: date
    window_end: date
    timezone_name: str
    valid_night_count: int = Field(..., ge=0)
    coverage_ratio: float = Field(..., ge=0, le=1)
    quality_status: Literal["usable", "limited", "unusable"]
    algorithm_id: str
    algorithm_version: str
    data_version: str
    measurement_cohort_ref: str | None = None
    policy_version: str | None = None
    source_hash: str | None = Field(default=None, min_length=64, max_length=64)
    maturity: BaselineMaturity
    value: Any = None
    source_refs: tuple[str, ...] = ()
    generated_at: datetime

    @model_validator(mode="after")
    def keep_baseline_non_habit(self) -> "ObjectiveBaselineArtifact":
        if self.window_start > self.window_end:
            raise ValueError("baseline window is reversed")
        if (
            self.maturity == BaselineMaturity.UNAVAILABLE
            and self.value is not None
        ):
            raise ValueError("unavailable baseline cannot carry a value")
        if self.metric_id in {
            "baseline.night_out_of_bed",
            "night_out_of_bed",
        } and self.value is not None:
            typed_value = NightOutOfBedBaselineValue.model_validate(self.value)
            object.__setattr__(self, "value", typed_value)
            if typed_value.nights_with_events > self.valid_night_count:
                raise ValueError(
                    "night-out-of-bed nights exceed valid-night count"
                )
        return self


class HabitProfileFact(StrictContract):
    fact_id: str
    fact_version: int = Field(..., ge=1)
    fact_hash: str = Field(..., min_length=64, max_length=64)
    subject_id: str
    concept_id: str
    concept_version: str
    value: Any
    unit: str | None = None
    disposition: Literal["answered", "variable", "not_applicable"]
    origin_semantic: Literal["elder_self_report", "family_observation"]
    source_actor_id: str
    source_role: Literal["elder", "family"]
    observation_date_start: date
    observation_date_end: date
    timezone_name: str
    day_type: Literal["all_days", "weekday", "weekend", "variable"]
    sleep_day_rule: Literal["wake_date", "bed_date"]
    observation_opportunity: ObservationOpportunity | None = None
    captured_at: datetime
    confirmed_at: datetime
    last_verified_at: datetime
    valid_until: datetime
    state: HabitFactState = HabitFactState.CONFIRMED
    source_answer_ref: str
    confirmation_ref: str
    causal_change_set_id: str
    replaces_fact_id: str | None = None
    access_scopes: tuple[str, ...] = ("elder_self",)
    forbidden_uses: tuple[str, ...] = (
        "diagnosis",
        "composite_score",
        "fixed_person_type",
        "causal_claim",
    )
    trust_label: Literal["user_data"] = "user_data"

    @classmethod
    def create(cls, **values: Any) -> "HabitProfileFact":
        material = dict(values)
        material.pop("fact_hash", None)
        unsigned = cls(fact_hash="0" * 64, **material)
        return unsigned.model_copy(
            update={
                "fact_hash": stable_hash(
                    unsigned.model_dump(mode="json", exclude={"fact_hash"})
                )
            }
        )

    @model_validator(mode="after")
    def validate_origin_and_window(self) -> "HabitProfileFact":
        if self.observation_date_start > self.observation_date_end:
            raise ValueError("Habit fact observation window is reversed")
        if (
            self.origin_semantic == "elder_self_report"
            and self.source_role != "elder"
        ):
            raise ValueError("elder self-report requires elder source role")
        if (
            self.origin_semantic == "family_observation"
            and self.source_role != "family"
        ):
            raise ValueError("family observation requires family source role")
        if self.valid_until <= self.confirmed_at:
            raise ValueError("Habit fact validity must extend beyond confirmation")
        return self

    def effective_status(self, now: datetime) -> HabitEffectiveStatus:
        if self.state in {
            HabitFactState.SUPERSEDED,
            HabitFactState.FORGOTTEN,
        }:
            return HabitEffectiveStatus.INACTIVE
        if self.state == HabitFactState.DISPUTED:
            return HabitEffectiveStatus.DISPUTED
        if self.state == HabitFactState.STALE or now >= self.valid_until:
            return HabitEffectiveStatus.STALE
        return HabitEffectiveStatus.CURRENT


class HabitProfileChangeCandidate(StrictContract):
    candidate_id: str
    candidate_hash: str = Field(..., min_length=64, max_length=64)
    operation: HabitChangeOperation
    subject_id: str
    concept_id: str
    concept_version: str
    value: Any = None
    unit: str | None = None
    disposition: Literal["answered", "variable", "not_applicable"] | None = None
    origin_semantic: Literal["elder_self_report", "family_observation"] | None = None
    source_actor_id: str | None = None
    source_role: Literal["elder", "family"] | None = None
    observation_date_start: date | None = None
    observation_date_end: date | None = None
    timezone_name: str | None = None
    day_type: Literal["all_days", "weekday", "weekend", "variable"] | None = None
    sleep_day_rule: Literal["wake_date", "bed_date"] | None = None
    observation_opportunity: ObservationOpportunity | None = None
    captured_at: datetime | None = None
    valid_until: datetime | None = None
    source_answer_ref: str | None = None
    replace_fact_id: str | None = None
    access_scopes: tuple[str, ...] = ("elder_self",)

    @classmethod
    def create(cls, **values: Any) -> "HabitProfileChangeCandidate":
        material = dict(values)
        material.pop("candidate_hash", None)
        unsigned = cls(candidate_hash="0" * 64, **material)
        return unsigned.model_copy(
            update={
                "candidate_hash": stable_hash(
                    unsigned.model_dump(
                        mode="json", exclude={"candidate_hash"}
                    )
                )
            }
        )

    @model_validator(mode="after")
    def validate_operation_shape(self) -> "HabitProfileChangeCandidate":
        if self.operation in {
            HabitChangeOperation.CREATE,
            HabitChangeOperation.REPLACE,
        }:
            required = (
                self.disposition,
                self.origin_semantic,
                self.source_actor_id,
                self.source_role,
                self.observation_date_start,
                self.observation_date_end,
                self.timezone_name,
                self.day_type,
                self.sleep_day_rule,
                self.captured_at,
                self.valid_until,
                self.source_answer_ref,
            )
            if any(item is None for item in required):
                raise ValueError("create/replace Habit candidate is incomplete")
        if (
            self.operation
            in {
                HabitChangeOperation.REPLACE,
                HabitChangeOperation.EXPIRE,
                HabitChangeOperation.FORGET,
            }
            and not self.replace_fact_id
        ):
            raise ValueError("Habit mutation requires replace_fact_id")
        if self.operation == HabitChangeOperation.CREATE and self.replace_fact_id:
            raise ValueError("Habit create cannot name a replacement target")
        if (
            self.observation_date_start
            and self.observation_date_end
            and self.observation_date_start > self.observation_date_end
        ):
            raise ValueError("Habit candidate observation window is reversed")
        if (
            self.captured_at
            and self.valid_until
            and self.valid_until <= self.captured_at
        ):
            raise ValueError("Habit candidate validity is not positive")
        return self


class HabitProfileChangeSet(StrictContract):
    change_set_id: str
    version: int = Field(..., ge=1)
    subject_id: str
    candidates: tuple[HabitProfileChangeCandidate, ...] = Field(
        min_length=1, max_length=3
    )
    manifest_hash: str = Field(..., min_length=64, max_length=64)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    expected_memory_version: int = Field(..., ge=0)
    action_scope: Literal["write_habit_profile"] = "write_habit_profile"
    confirmation_expires_at: datetime
    created_at: datetime

    @classmethod
    def create(cls, **values: Any) -> "HabitProfileChangeSet":
        material = dict(values)
        material.pop("manifest_hash", None)
        candidates = tuple(material["candidates"])
        manifest = {
            "change_set_id": material["change_set_id"],
            "version": material["version"],
            "subject_id": material["subject_id"],
            "candidate_refs": [
                f"{item.candidate_id}:{item.candidate_hash}" for item in candidates
            ],
            "fact_snapshot_hash": material["fact_snapshot_hash"],
            "expected_memory_version": material["expected_memory_version"],
            "action_scope": material.get(
                "action_scope", "write_habit_profile"
            ),
            "confirmation_expires_at": material["confirmation_expires_at"],
        }
        return cls(manifest_hash=stable_hash(manifest), **material)

    def without_candidates(
        self,
        candidate_ids: set[str],
        *,
        new_change_set_id: str,
        created_at: datetime,
    ) -> "HabitProfileChangeSet":
        kept = tuple(
            item for item in self.candidates if item.candidate_id not in candidate_ids
        )
        if not kept:
            raise ValueError("Habit change set cannot become empty")
        return HabitProfileChangeSet.create(
            change_set_id=new_change_set_id,
            version=self.version + 1,
            subject_id=self.subject_id,
            candidates=kept,
            fact_snapshot_hash=self.fact_snapshot_hash,
            expected_memory_version=self.expected_memory_version,
            action_scope=self.action_scope,
            confirmation_expires_at=self.confirmation_expires_at,
            created_at=created_at,
        )

    @model_validator(mode="after")
    def validate_manifest_shape(self) -> "HabitProfileChangeSet":
        if self.confirmation_expires_at <= self.created_at:
            raise ValueError("Habit change-set confirmation window is invalid")
        if len({item.candidate_id for item in self.candidates}) != len(
            self.candidates
        ):
            raise ValueError("Habit change set has duplicate candidate ids")
        if any(item.subject_id != self.subject_id for item in self.candidates):
            raise ValueError("Habit change set crosses subject")
        return self


class HabitProfileConfirmation(StrictContract):
    """Legacy wire/audit shape; it is not accepted as commit authority."""

    confirmation_id: str
    actor_id: str
    actor_role: Literal["elder"]
    subject_id: str
    action_scope: Literal["write_habit_profile"]
    change_set_id: str
    change_set_version: int = Field(..., ge=1)
    manifest_hash: str = Field(..., min_length=64, max_length=64)
    expires_at: datetime

    def validate_for(
        self,
        change_set: HabitProfileChangeSet,
        *,
        now: datetime,
    ) -> None:
        expected = (
            change_set.subject_id,
            change_set.action_scope,
            change_set.change_set_id,
            change_set.version,
            change_set.manifest_hash,
        )
        actual = (
            self.subject_id,
            self.action_scope,
            self.change_set_id,
            self.change_set_version,
            self.manifest_hash,
        )
        if expected != actual:
            raise ValueError("Habit Profile confirmation binding mismatch")
        if self.expires_at <= now or change_set.confirmation_expires_at <= now:
            raise ValueError("Habit Profile confirmation expired")


class HabitProfileCommitReceipt(StrictContract):
    receipt_id: str
    change_set_id: str
    manifest_hash: str = Field(..., min_length=64, max_length=64)
    subject_id: str
    memory_version_before: int = Field(..., ge=0)
    memory_version_after: int = Field(..., ge=1)
    fact_ids: tuple[str, ...]
    idempotency_key: str
    committed_at: datetime
    audit_retained: bool = True


class HabitProfileState(StrictContract):
    subject_id: str
    version: int = Field(default=0, ge=0)
    facts: tuple[HabitProfileFact, ...] = ()
    audit_receipts: tuple[HabitProfileCommitReceipt, ...] = ()


class HabitProfileContextFact(StrictContract):
    fact_ref: str
    concept_id: str
    concept_version: str
    value: Any
    unit: str | None = None
    disposition: str
    origin_semantic: Literal["elder_self_report", "family_observation"]
    source_role: Literal["elder", "family"]
    source_actor_ref: str
    observation_date_start: date
    observation_date_end: date
    timezone_name: str
    day_type: str
    observation_opportunity: ObservationOpportunity | None = None
    effective_status: HabitEffectiveStatus
    confirmed_at: datetime
    trust_label: Literal["user_data"] = "user_data"


class HabitProfileReadResult(StrictContract):
    subject_id: str
    memory_version: int = Field(..., ge=0)
    purpose: str
    facts: tuple[HabitProfileContextFact, ...]
    stale_concept_ids: tuple[str, ...] = ()
    disputed_concept_ids: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    retention_notice: str = (
        "Forgotten or superseded facts are excluded from personalization; "
        "minimal audit events may remain under governance retention."
    )


class HabitProfileStore(Protocol):
    lock: RLock

    def get(self, subject_id: str) -> HabitProfileState: ...

    def commit(
        self,
        change_set: HabitProfileChangeSet,
        approval_capability: VerifiedApprovalCapability,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> HabitProfileCommitReceipt: ...

    def read(
        self,
        *,
        subject_id: str,
        actor_id: str,
        role: Literal["elder", "family", "doctor", "system"],
        authorization_scope: Iterable[str],
        purpose: Literal[
            "evidence",
            "care",
            "profile_review",
            "doctor_material",
            "family_coordination",
        ],
        requested_concept_ids: Iterable[str],
        now: datetime | None = None,
        include_stale_for_review: bool = False,
    ) -> HabitProfileReadResult: ...


class ObjectiveBaselineStore(Protocol):
    def put(self, artifact: ObjectiveBaselineArtifact) -> None: ...

    def read(
        self,
        *,
        subject_id: str,
        metric_ids: Iterable[str],
    ) -> tuple[ObjectiveBaselineArtifact, ...]: ...


class InMemoryObjectiveBaselineStore:
    """Typed latest-artifact view; not part of subjective Habit Profile."""

    def __init__(
        self,
        artifacts: Iterable[ObjectiveBaselineArtifact] = (),
    ) -> None:
        self._artifacts: dict[
            tuple[str, str], ObjectiveBaselineArtifact
        ] = {}
        self.lock = RLock()
        for artifact in artifacts:
            self.put(artifact)

    def put(self, artifact: ObjectiveBaselineArtifact) -> None:
        with self.lock:
            key = (artifact.subject_id, artifact.metric_id)
            current = self._artifacts.get(key)
            if current and current.generated_at > artifact.generated_at:
                raise ValueError("cannot replace baseline with an older artifact")
            self._artifacts[key] = artifact.model_copy(deep=True)

    def read(
        self,
        *,
        subject_id: str,
        metric_ids: Iterable[str],
    ) -> tuple[ObjectiveBaselineArtifact, ...]:
        requested = tuple(dict.fromkeys(metric_ids))
        with self.lock:
            return tuple(
                artifact.model_copy(deep=True)
                for metric_id in requested
                if (
                    artifact := self._artifacts.get((subject_id, metric_id))
                )
                is not None
            )


class HabitProfileCandidateBuilder:
    def __init__(
        self,
        concepts: Iterable[HabitConceptDefinition],
    ) -> None:
        self.concepts = {
            (item.concept_id, item.version): item for item in concepts
        }

    def from_captured(
        self,
        answer: CapturedHabitAnswer,
        *,
        operation: HabitChangeOperation = HabitChangeOperation.CREATE,
        replace_fact_id: str | None = None,
    ) -> HabitProfileChangeCandidate:
        concept = self.concepts.get((answer.concept_id, answer.concept_version))
        if concept is None or concept.domain_review_status != "approved":
            raise ValueError("Habit answer has no approved concept definition")
        if concept.persistence != HabitPersistenceEligibility.PROFILE_ELIGIBLE:
            raise ValueError("episode-only answer cannot become Profile candidate")
        if not answer.profile_candidate_eligible:
            raise ValueError("captured answer is not Profile candidate eligible")
        if answer.disposition not in {
            HabitAnswerDisposition.ANSWERED,
            HabitAnswerDisposition.VARIABLE,
            HabitAnswerDisposition.NOT_APPLICABLE,
        }:
            raise ValueError("non-answer cannot become Habit Profile fact")
        if _contains_clinical_context(answer.normalized_value):
            raise ValueError("clinical/safety context cannot enter Habit Profile")
        start = answer.observation_date_start or answer.captured_at.date()
        end = answer.observation_date_end or answer.captured_at.date()
        if start > end:
            raise ValueError("Habit answer observation window is reversed")
        value = answer.normalized_value
        unit = None
        if isinstance(value, dict) and set(value) == {"value", "unit"}:
            unit = value["unit"]
            value = value["value"]
        scopes = ["elder_self"]
        if concept.domain in {"observable_night_behavior", "night_activity"}:
            scopes.append("family_observable")
            scopes.append("doctor_material")
        return HabitProfileChangeCandidate.create(
            candidate_id=f"habit-candidate:{answer.answer_ref}",
            operation=operation,
            subject_id=answer.subject_id,
            concept_id=answer.concept_id,
            concept_version=answer.concept_version,
            value=value,
            unit=unit,
            disposition=answer.disposition.value,
            origin_semantic=answer.origin_semantic,
            source_actor_id=answer.actor_id,
            source_role=answer.role,
            observation_date_start=start,
            observation_date_end=end,
            timezone_name=answer.timezone_name,
            day_type=answer.day_type,
            sleep_day_rule=answer.sleep_day_rule,
            observation_opportunity=answer.observation_opportunity,
            captured_at=answer.captured_at,
            valid_until=answer.captured_at
            + timedelta(days=concept.valid_for_days),
            source_answer_ref=answer.answer_ref,
            replace_fact_id=replace_fact_id,
            access_scopes=tuple(scopes),
        )


class InMemoryHabitProfileStore:
    """Single typed authority for Product Agent Habit Profile state."""

    def __init__(
        self,
        concepts: Iterable[HabitConceptDefinition] | None = None,
    ) -> None:
        reviewed = tuple(concepts or DEFAULT_HABIT_CONCEPTS)
        self._concepts = reviewed
        self._current_concept_versions = {
            item.concept_id: item.version
            for item in reviewed
            if item.domain_review_status == "approved"
        }
        self._states: dict[str, HabitProfileState] = {}
        self._idempotent: dict[str, tuple[str, HabitProfileCommitReceipt]] = {}
        self._consumed_approval_grants: set[str] = set()
        self.lock = RLock()

    def get(self, subject_id: str) -> HabitProfileState:
        with self.lock:
            state = self._states.get(
                subject_id, HabitProfileState(subject_id=subject_id)
            )
            self._validate_state_integrity(state)
            return state.model_copy(deep=True)

    def commit(
        self,
        change_set: HabitProfileChangeSet,
        approval_capability: VerifiedApprovalCapability,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> HabitProfileCommitReceipt:
        committed_at = now or datetime.now(timezone.utc)
        self._validate_change_set_integrity(change_set)
        payload_hash = habit_commit_payload_hash(
            change_set,
            approval_capability,
        )
        approval_grant = approval_capability.grant
        with self.lock:
            replay = self._idempotent.get(idempotency_key)
            if replay:
                if replay[0] != payload_hash:
                    raise ValueError("Habit Profile idempotency-key collision")
                return replay[1].model_copy(deep=True)
            approval_grant = validate_habit_approval_capability(
                change_set,
                approval_capability,
                idempotency_key=idempotency_key,
                now=committed_at,
            )
            if approval_grant.grant_id in self._consumed_approval_grants:
                raise ValueError("Habit Profile approval grant already consumed")
            state = self.get(change_set.subject_id)
            if state.version != change_set.expected_memory_version:
                raise ValueError("stale Habit Profile memory version")
            facts = [item.model_copy(deep=True) for item in state.facts]
            produced_ids: list[str] = []
            for candidate in change_set.candidates:
                self._validate_candidate_against_state(
                    candidate, facts, now=committed_at
                )
            for candidate in change_set.candidates:
                produced_ids.extend(
                    self._apply_candidate(
                        candidate,
                        facts,
                        change_set=change_set,
                        approval_audit_ref=approval_grant.grant_id,
                        committed_at=committed_at,
                    )
                )
            receipt = HabitProfileCommitReceipt(
                receipt_id=f"habit-commit:{change_set.change_set_id}",
                change_set_id=change_set.change_set_id,
                manifest_hash=change_set.manifest_hash,
                subject_id=change_set.subject_id,
                memory_version_before=state.version,
                memory_version_after=state.version + 1,
                fact_ids=tuple(produced_ids),
                idempotency_key=idempotency_key,
                committed_at=committed_at,
            )
            self._states[change_set.subject_id] = HabitProfileState(
                subject_id=change_set.subject_id,
                version=state.version + 1,
                facts=tuple(facts),
                audit_receipts=(*state.audit_receipts, receipt),
            )
            self._consumed_approval_grants.add(approval_grant.grant_id)
            self._idempotent[idempotency_key] = (payload_hash, receipt)
            return receipt.model_copy(deep=True)

    @staticmethod
    def _validate_state_integrity(state: HabitProfileState) -> None:
        for fact in state.facts:
            rebuilt = HabitProfileFact.create(
                **fact.model_dump(exclude={"fact_hash"})
            )
            if rebuilt.fact_hash != fact.fact_hash:
                raise ValueError("Habit Profile fact integrity mismatch")

    def read(
        self,
        *,
        subject_id: str,
        actor_id: str,
        role: Literal["elder", "family", "doctor", "system"],
        authorization_scope: Iterable[str],
        purpose: Literal[
            "evidence",
            "care",
            "profile_review",
            "doctor_material",
            "family_coordination",
        ],
        requested_concept_ids: Iterable[str],
        now: datetime | None = None,
        include_stale_for_review: bool = False,
    ) -> HabitProfileReadResult:
        read_at = now or datetime.now(timezone.utc)
        state = self.get(subject_id)
        requested = set(requested_concept_ids)
        scopes = set(authorization_scope)
        if role == "family" and "read_habit_profile_family" not in scopes:
            raise PermissionError("family Habit Profile scope is missing")
        if role == "family" and purpose not in {
            "evidence",
            "family_coordination",
        }:
            raise PermissionError("family Habit Profile purpose is not allowed")
        if role == "doctor" and (
            purpose != "doctor_material"
            or "read_habit_profile_doctor" not in scopes
        ):
            raise PermissionError("doctor Habit Profile scope/purpose is missing")
        if role == "system" and (
            purpose not in {"evidence", "care"}
            or "read_habit_profile_system" not in scopes
        ):
            raise PermissionError("system Habit Profile scope/purpose is missing")
        current = self._current_views(state.facts, read_at)
        selected: list[HabitProfileContextFact] = []
        stale: set[str] = set()
        disputed: set[str] = set()
        for fact, effective in current:
            if (
                self._current_concept_versions.get(fact.concept_id)
                != fact.concept_version
                and effective == HabitEffectiveStatus.CURRENT
            ):
                effective = HabitEffectiveStatus.STALE
            if requested and fact.concept_id not in requested:
                continue
            if role == "family" and "family_observable" not in fact.access_scopes:
                continue
            if role == "doctor" and "doctor_material" not in fact.access_scopes:
                continue
            if effective == HabitEffectiveStatus.STALE:
                stale.add(fact.concept_id)
                if not include_stale_for_review:
                    continue
            if effective == HabitEffectiveStatus.DISPUTED:
                disputed.add(fact.concept_id)
            if effective == HabitEffectiveStatus.INACTIVE:
                continue
            selected.append(
                HabitProfileContextFact(
                    fact_ref=(
                        fact.fact_id
                        if fact.fact_id.startswith("habit-fact:")
                        else f"habit-fact:{fact.fact_id}"
                    ),
                    concept_id=fact.concept_id,
                    concept_version=fact.concept_version,
                    value=deepcopy(fact.value),
                    unit=fact.unit,
                    disposition=fact.disposition,
                    origin_semantic=fact.origin_semantic,
                    source_role=fact.source_role,
                    source_actor_ref=(
                        f"actor-ref:{stable_hash(fact.source_actor_id)[:16]}"
                    ),
                    observation_date_start=fact.observation_date_start,
                    observation_date_end=fact.observation_date_end,
                    timezone_name=fact.timezone_name,
                    day_type=fact.day_type,
                    observation_opportunity=fact.observation_opportunity,
                    effective_status=effective,
                    confirmed_at=fact.confirmed_at,
                )
            )
        return HabitProfileReadResult(
            subject_id=subject_id,
            memory_version=state.version,
            purpose=purpose,
            facts=tuple(selected),
            stale_concept_ids=tuple(sorted(stale)),
            disputed_concept_ids=tuple(sorted(disputed)),
            source_refs=tuple(item.fact_ref for item in selected),
        )

    @staticmethod
    def _validate_change_set_integrity(
        change_set: HabitProfileChangeSet,
    ) -> None:
        values = change_set.model_dump(exclude={"manifest_hash"})
        values["candidates"] = change_set.candidates
        rebuilt = HabitProfileChangeSet.create(
            **values
        )
        if rebuilt.manifest_hash != change_set.manifest_hash:
            raise ValueError("Habit Profile change-set manifest was altered")
        for candidate in change_set.candidates:
            rebuilt_candidate = HabitProfileChangeCandidate.create(
                **candidate.model_dump(exclude={"candidate_hash"})
            )
            if rebuilt_candidate.candidate_hash != candidate.candidate_hash:
                raise ValueError("Habit Profile candidate was altered")
            if candidate.subject_id != change_set.subject_id:
                raise ValueError("Habit Profile candidate crosses subject")

    @staticmethod
    def _validate_candidate_against_state(
        candidate: HabitProfileChangeCandidate,
        facts: list[HabitProfileFact],
        *,
        now: datetime,
    ) -> None:
        if candidate.operation in {
            HabitChangeOperation.REPLACE,
            HabitChangeOperation.EXPIRE,
            HabitChangeOperation.FORGET,
        }:
            target = next(
                (item for item in facts if item.fact_id == candidate.replace_fact_id),
                None,
            )
            if target is None:
                raise ValueError("Habit Profile mutation target does not exist")
            if target.subject_id != candidate.subject_id:
                raise ValueError("Habit Profile mutation crosses subject")
            if (
                target.concept_id,
                target.concept_version,
            ) != (
                candidate.concept_id,
                candidate.concept_version,
            ):
                raise ValueError("Habit mutation target concept/version mismatch")
            if target.effective_status(now) == HabitEffectiveStatus.INACTIVE:
                raise ValueError("Habit Profile mutation target is inactive")
        if candidate.operation == HabitChangeOperation.CREATE:
            overlapping = [
                item
                for item in facts
                if item.concept_id == candidate.concept_id
                and _day_types_overlap(item.day_type, candidate.day_type)
                and item.state
                not in {HabitFactState.SUPERSEDED, HabitFactState.FORGOTTEN}
                and _windows_overlap(
                    item.observation_date_start,
                    item.observation_date_end,
                    candidate.observation_date_start,
                    candidate.observation_date_end,
                )
                and item.source_actor_id == candidate.source_actor_id
            ]
            if overlapping:
                raise ValueError(
                    "overlapping current Habit fact requires explicit replace"
                )

    @staticmethod
    def _apply_candidate(
        candidate: HabitProfileChangeCandidate,
        facts: list[HabitProfileFact],
        *,
        change_set: HabitProfileChangeSet,
        approval_audit_ref: str,
        committed_at: datetime,
    ) -> list[str]:
        if candidate.operation in {
            HabitChangeOperation.REPLACE,
            HabitChangeOperation.EXPIRE,
            HabitChangeOperation.FORGET,
        }:
            index = next(
                index
                for index, item in enumerate(facts)
                if item.fact_id == candidate.replace_fact_id
            )
            old = facts[index]
            state = {
                HabitChangeOperation.REPLACE: HabitFactState.SUPERSEDED,
                HabitChangeOperation.EXPIRE: HabitFactState.STALE,
                HabitChangeOperation.FORGET: HabitFactState.FORGOTTEN,
            }[candidate.operation]
            facts[index] = _fact_with_state(old, state)
            if candidate.operation != HabitChangeOperation.REPLACE:
                return [old.fact_id]
        fact_id = (
            f"habit-fact:{change_set.change_set_id}:{candidate.candidate_id}"
        )
        fact = HabitProfileFact.create(
            fact_id=fact_id,
            fact_version=1,
            subject_id=candidate.subject_id,
            concept_id=candidate.concept_id,
            concept_version=candidate.concept_version,
            value=deepcopy(candidate.value),
            unit=candidate.unit,
            disposition=candidate.disposition,
            origin_semantic=candidate.origin_semantic,
            source_actor_id=candidate.source_actor_id,
            source_role=candidate.source_role,
            observation_date_start=candidate.observation_date_start,
            observation_date_end=candidate.observation_date_end,
            timezone_name=candidate.timezone_name,
            day_type=candidate.day_type,
            sleep_day_rule=candidate.sleep_day_rule,
            observation_opportunity=candidate.observation_opportunity,
            captured_at=candidate.captured_at,
            confirmed_at=committed_at,
            last_verified_at=committed_at,
            valid_until=candidate.valid_until,
            source_answer_ref=candidate.source_answer_ref,
            confirmation_ref=approval_audit_ref,
            causal_change_set_id=change_set.change_set_id,
            replaces_fact_id=candidate.replace_fact_id,
            access_scopes=candidate.access_scopes,
        )
        facts.append(fact)
        _mark_overlapping_conflicts(fact, facts)
        return [fact.fact_id]

    @staticmethod
    def _current_views(
        facts: tuple[HabitProfileFact, ...],
        now: datetime,
    ) -> list[tuple[HabitProfileFact, HabitEffectiveStatus]]:
        return [(item, item.effective_status(now)) for item in facts]


def evidence_claim_from_captured_answer(
    answer: CapturedHabitAnswer,
) -> EvidenceClaim:
    semantic = (
        EvidenceSemantic.USER_REPORTED
        if answer.origin_semantic == "elder_self_report"
        else EvidenceSemantic.OBSERVER_REPORTED
    )
    source_kind = (
        EvidenceSourceKind.USER_REPORT
        if answer.origin_semantic == "elder_self_report"
        else EvidenceSourceKind.AUTHORIZED_OBSERVER_REPORT
    )
    statement = (
        f"{answer.concept_id}={answer.normalized_value}"
        if answer.disposition == HabitAnswerDisposition.ANSWERED
        else f"{answer.concept_id}={answer.disposition.value}"
    )
    return EvidenceClaim(
        claim_id=f"claim:{answer.answer_ref}",
        semantic=semantic,
        statement=statement,
        source_kind=source_kind,
        evidence_refs=[answer.answer_ref],
        confidence=(
            answer.observation_opportunity.confidence
            if answer.observation_opportunity
            else 1
        ),
        date_start=answer.observation_date_start,
        date_end=answer.observation_date_end,
    )


def habit_commit_payload_hash(
    change_set: HabitProfileChangeSet,
    approval_capability: VerifiedApprovalCapability,
) -> str:
    if type(approval_capability) is not VerifiedApprovalCapability:
        raise TypeError(
            "Habit Profile commit requires a verified approval capability"
        )
    approval_grant = approval_capability.grant
    return stable_hash(
        {
            "change_set": change_set.model_dump(mode="json"),
            # This persisted projection is audit evidence only. The sealed
            # capability, not these serializable fields, authorizes the write.
            "approval_grant": approval_grant.model_dump(mode="json"),
        }
    )


def validate_habit_approval_capability(
    change_set: HabitProfileChangeSet,
    approval_capability: VerifiedApprovalCapability,
    *,
    idempotency_key: str,
    now: datetime,
) -> ApprovalGrant:
    """Validate the sealed HITL capability against the exact Habit manifest."""

    if type(approval_capability) is not VerifiedApprovalCapability:
        raise TypeError(
            "Habit Profile commit requires a verified approval capability"
        )
    grant = approval_capability.grant
    if grant.approver_role != "elder":
        raise PermissionError("only an elder approval may commit Habit Profile")
    if grant.expires_at != change_set.confirmation_expires_at:
        raise ValueError("Habit Profile approval expiry binding mismatch")
    approval_capability.validate_exact_binding(
        decision_id=grant.decision_id,
        proposal_id=grant.proposal_id,
        actor_id=grant.approver_actor_id,
        actor_role=grant.approver_role,
        subject_id=change_set.subject_id,
        target_id=change_set.change_set_id,
        target_hash=change_set.manifest_hash,
        action_scope=change_set.action_scope,
        fact_snapshot_id=grant.fact_snapshot_id,
        fact_snapshot_hash=change_set.fact_snapshot_hash,
        policy_version=grant.policy_version,
        idempotency_key=idempotency_key,
        now=now,
    )
    return grant


def evidence_claim_from_profile_fact(
    fact: HabitProfileContextFact,
) -> EvidenceClaim:
    semantic = (
        EvidenceSemantic.USER_REPORTED
        if fact.origin_semantic == "elder_self_report"
        else EvidenceSemantic.OBSERVER_REPORTED
    )
    source_kind = (
        EvidenceSourceKind.CONFIRMED_MEMORY
        if fact.origin_semantic == "elder_self_report"
        else EvidenceSourceKind.AUTHORIZED_OBSERVER_REPORT
    )
    return EvidenceClaim(
        claim_id=f"claim:{fact.fact_ref}",
        semantic=semantic,
        statement=f"{fact.concept_id}={fact.value}",
        source_kind=source_kind,
        evidence_refs=[fact.fact_ref],
        confidence=0.8 if semantic == EvidenceSemantic.OBSERVER_REPORTED else 1,
        date_start=fact.observation_date_start,
        date_end=fact.observation_date_end,
    )


def _contains_clinical_context(value: Any) -> bool:
    text = str(value or "").lower()
    markers = (
        "过敏",
        "疾病",
        "诊断",
        "用药",
        "药物",
        "剂量",
        "高血压",
        "糖尿病",
        "呼吸暂停",
        "跌倒",
        "不可控制嗜睡",
        "psqi",
        "isi",
        "ess",
        "allergy",
        "diagnosis",
        "medication",
        "dose",
        "apnea",
    )
    return any(marker in text for marker in markers)


def _windows_overlap(
    left_start: date,
    left_end: date,
    right_start: date,
    right_end: date,
) -> bool:
    return max(left_start, right_start) <= min(left_end, right_end)


def _day_types_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    if "all_days" in {left, right}:
        return True
    if "variable" in {left, right}:
        return True
    return False


def _mark_overlapping_conflicts(
    new_fact: HabitProfileFact,
    facts: list[HabitProfileFact],
) -> None:
    for index, item in enumerate(facts):
        if item.fact_id == new_fact.fact_id:
            continue
        if (
            item.concept_id == new_fact.concept_id
            and _day_types_overlap(item.day_type, new_fact.day_type)
            and item.value != new_fact.value
            and item.state
            in {HabitFactState.CONFIRMED, HabitFactState.DISPUTED}
            and _windows_overlap(
                item.observation_date_start,
                item.observation_date_end,
                new_fact.observation_date_start,
                new_fact.observation_date_end,
            )
        ):
            facts[index] = _fact_with_state(item, HabitFactState.DISPUTED)
            facts[-1] = _fact_with_state(
                facts[-1], HabitFactState.DISPUTED
            )


def _fact_with_state(
    fact: HabitProfileFact,
    state: HabitFactState,
) -> HabitProfileFact:
    return HabitProfileFact.create(
        **fact.model_dump(exclude={"fact_hash", "state"}),
        state=state,
    )


__all__ = [
    "HABIT_PROFILE_VERSION",
    "BaselineMaturity",
    "HabitChangeOperation",
    "HabitEffectiveStatus",
    "HabitFactState",
    "HabitProfileCandidateBuilder",
    "HabitProfileChangeCandidate",
    "HabitProfileChangeSet",
    "HabitProfileCommitReceipt",
    "HabitProfileConfirmation",
    "HabitProfileContextFact",
    "HabitProfileFact",
    "HabitProfileReadResult",
    "HabitProfileState",
    "HabitProfileStore",
    "InMemoryHabitProfileStore",
    "InMemoryObjectiveBaselineStore",
    "NightOutOfBedBaselineValue",
    "ObjectiveBaselineArtifact",
    "ObjectiveBaselineStore",
    "evidence_claim_from_captured_answer",
    "evidence_claim_from_profile_fact",
    "habit_commit_payload_hash",
]
