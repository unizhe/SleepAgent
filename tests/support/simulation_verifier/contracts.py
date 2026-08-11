"""Strict human-action and expected-oracle contracts for client verification."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sleepagent.sleep_domain.contracts import ObservationType


NonEmptyStr = Annotated[str, Field(min_length=1)]


class VerifierContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def require_aware_datetimes(self) -> "VerifierContract":
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            if isinstance(value, datetime) and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError(f"{field_name} must be timezone-aware")
        return self


ActorRole = Literal["elder", "family", "doctor", "demo_controller"]


class ActionBase(VerifierContract):
    schema_version: Literal["simulation_action.v1"] = "simulation_action.v1"
    event_id: NonEmptyStr
    scenario_id: NonEmptyStr
    checkpoint: int = Field(ge=0, le=15)
    actor: ActorRole
    occurred_at: datetime


class AdvanceAction(ActionBase):
    action: Literal["advance"] = "advance"
    actor: Literal["demo_controller"] = "demo_controller"
    to_checkpoint: int = Field(ge=1, le=15)


class AskAction(ActionBase):
    action: Literal["ask"] = "ask"
    actor: Literal["elder"] = "elder"
    question_kind: Literal["general_knowledge", "personalized_morning"]
    text: NonEmptyStr


class HabitAnswerAction(ActionBase):
    action: Literal["habit_answer"] = "habit_answer"
    actor: Literal["elder", "family"]
    concept_id: NonEmptyStr
    value: str | int | float | bool | tuple[NonEmptyStr, ...]
    provenance: Literal["elder_self_report", "family_observer_report"]

    @model_validator(mode="after")
    def provenance_matches_actor(self) -> "HabitAnswerAction":
        expected = (
            "elder_self_report" if self.actor == "elder" else "family_observer_report"
        )
        if self.provenance != expected:
            raise ValueError("habit answer provenance must match its actor")
        return self


class HabitSkipAction(ActionBase):
    action: Literal["habit_skip"] = "habit_skip"
    actor: Literal["elder"] = "elder"
    concept_id: NonEmptyStr


class HabitUnknownAction(ActionBase):
    action: Literal["habit_unknown"] = "habit_unknown"
    actor: Literal["elder"] = "elder"
    concept_id: NonEmptyStr


class ConfirmAction(ActionBase):
    action: Literal["confirm"] = "confirm"
    actor: Literal["elder"] = "elder"
    target_kind: Literal["habit_profile", "care_action"]
    target_label: NonEmptyStr


class DeclineAction(ActionBase):
    action: Literal["decline"] = "decline"
    actor: Literal["elder"] = "elder"
    target_kind: Literal["habit_profile", "care_action"]
    target_label: NonEmptyStr


class FeedbackAction(ActionBase):
    action: Literal["feedback"] = "feedback"
    actor: Literal["elder"] = "elder"
    target_label: NonEmptyStr
    text: NonEmptyStr
    provenance: Literal["user_reported"] = "user_reported"


class ReanalysisAction(ActionBase):
    action: Literal["reanalysis"] = "reanalysis"
    actor: Literal["elder"] = "elder"
    target_night: int = Field(ge=1, le=15)


class RestartAction(ActionBase):
    action: Literal["restart"] = "restart"
    actor: Literal["demo_controller"] = "demo_controller"


class EnableSyntheticMemoryAction(ActionBase):
    action: Literal["memory_enable_synthetic"] = "memory_enable_synthetic"
    actor: Literal["demo_controller"] = "demo_controller"
    ttl_seconds: int = Field(ge=1, le=3600)


class MemoryReadAction(ActionBase):
    action: Literal["memory_read"] = "memory_read"
    actor: Literal["elder"] = "elder"
    target_label: NonEmptyStr
    canonical_revalidation_required: Literal[True] = True


class ForgetAction(ActionBase):
    action: Literal["forget"] = "forget"
    actor: Literal["elder"] = "elder"
    target_kind: Literal["memory", "profile"]
    target_label: NonEmptyStr


class WithdrawAction(ActionBase):
    action: Literal["withdraw"] = "withdraw"
    actor: Literal["elder"] = "elder"
    target_kind: Literal["memory", "profile", "authorization"]
    target_label: NonEmptyStr


class DoctorRequestAction(ActionBase):
    action: Literal["doctor_request"] = "doctor_request"
    actor: Literal["elder"] = "elder"
    text: NonEmptyStr


class UrgentTextAction(ActionBase):
    action: Literal["urgent_text"] = "urgent_text"
    actor: Literal["elder", "family"]
    text: NonEmptyStr


SimulationAction: TypeAlias = Annotated[
    AdvanceAction
    | AskAction
    | HabitAnswerAction
    | HabitSkipAction
    | HabitUnknownAction
    | ConfirmAction
    | DeclineAction
    | FeedbackAction
    | ReanalysisAction
    | RestartAction
    | EnableSyntheticMemoryAction
    | MemoryReadAction
    | ForgetAction
    | WithdrawAction
    | DoctorRequestAction
    | UrgentTextAction,
    Field(discriminator="action"),
]


class SimulationActionTimeline(VerifierContract):
    schema_version: Literal["simulation_action_timeline.v1"] = (
        "simulation_action_timeline.v1"
    )
    scenario_id: NonEmptyStr
    actions: tuple[SimulationAction, ...]

    @model_validator(mode="after")
    def validate_timeline(self) -> "SimulationActionTimeline":
        if not self.actions:
            raise ValueError("action timeline cannot be empty")
        if any(action.scenario_id != self.scenario_id for action in self.actions):
            raise ValueError("every action must match the timeline scenario_id")
        ids = [action.event_id for action in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError("action event_id values must be unique")
        checkpoints = [action.checkpoint for action in self.actions]
        if checkpoints != sorted(checkpoints):
            raise ValueError("action checkpoints must be monotonic")
        timestamps = [action.occurred_at for action in self.actions]
        if timestamps != sorted(timestamps):
            raise ValueError("action timestamps must be monotonic")
        return self


class GeneratorOracle(VerifierContract):
    schema_version: Literal["generator_oracle.v1"] = "generator_oracle.v1"
    night_count: int = Field(ge=1)
    minimum_observation_count: int = Field(ge=1)
    required_observation_types: tuple[ObservationType, ...]
    observation_ids_unique: Literal[True] = True
    data_mode: Literal["replay"] = "replay"
    synthetic_non_release: Literal[True] = True
    normal_fast_path_quality: Literal["sufficient", "not_applicable"]

    @model_validator(mode="after")
    def require_unique_observation_types(self) -> "GeneratorOracle":
        if not self.required_observation_types or len(
            self.required_observation_types
        ) != len(set(self.required_observation_types)):
            raise ValueError(
                "required_observation_types must be non-empty and unique"
            )
        return self


class AgentPathOracle(VerifierContract):
    case_id: NonEmptyStr
    required_path: tuple[
        Literal[
            "SleepCareAgent",
            "EvidenceReasoningAgent",
            "CareStrategyAgent",
            "SafetyReviewAgent",
        ],
        ...,
    ]
    forbidden_agents: tuple[
        Literal[
            "SleepCareAgent",
            "EvidenceReasoningAgent",
            "CareStrategyAgent",
            "SafetyReviewAgent",
        ],
        ...,
    ] = ()
    expected_terminal_state: NonEmptyStr


class AgentsOracle(VerifierContract):
    schema_version: Literal["agents_oracle.v1"] = "agents_oracle.v1"
    cases: tuple[AgentPathOracle, ...]
    legacy_runtime_forbidden: Literal[True] = True


class HabitOracle(VerifierContract):
    schema_version: Literal["habit_oracle.v1"] = "habit_oracle.v1"
    required_actions: tuple[
        Literal[
            "habit_answer",
            "habit_skip",
            "habit_unknown",
            "family_observer_report",
            "confirm",
        ],
        ...,
    ]
    persistent_concepts: tuple[NonEmptyStr, ...]
    episode_only_concepts: tuple[NonEmptyStr, ...]
    family_provenance_preserved: Literal[True] = True
    unconfirmed_profile_unchanged: Literal[True] = True


class MemoryOracle(VerifierContract):
    schema_version: Literal["memory_oracle.v1"] = "memory_oracle.v1"
    induction_required: bool
    canonical_revalidation_required: Literal[True] = True
    restart_old_lease_fails_closed: Literal[True] = True
    explicit_reenable_required: Literal[True] = True
    forget_invalidates_old_handle: Literal[True] = True
    withdraw_invalidates_old_slice: Literal[True] = True


class ColdStartCheckpointOracle(VerifierContract):
    valid_nights: Literal[0, 1, 2, 4, 15]
    ceiling: Literal[
        "general_knowledge_only",
        "single_night_description",
        "two_night_difference",
        "provisional_pattern",
        "established_baseline",
    ]


class ColdStartOracle(VerifierContract):
    schema_version: Literal["cold_start_oracle.v1"] = "cold_start_oracle.v1"
    checkpoints: tuple[ColdStartCheckpointOracle, ...]
    per_metric_independent: Literal[True] = True
    degradation_and_recovery_required: Literal[True] = True

    @model_validator(mode="after")
    def exact_boundaries(self) -> "ColdStartOracle":
        if [item.valid_nights for item in self.checkpoints] != [0, 1, 2, 4, 15]:
            raise ValueError("cold-start oracle requires exact 0/1/2/4/15 boundaries")
        return self


class SafetyOracle(VerifierContract):
    schema_version: Literal["safety_oracle.v1"] = "safety_oracle.v1"
    doctor_request_requires_safety: Literal[True] = True
    urgent_preempts_agents: Literal[True] = True
    urgent_model_invocation_count: Literal[0] = 0
    target_hash_binding_required: Literal[True] = True


class BackendOracle(VerifierContract):
    schema_version: Literal["backend_oracle.v1"] = "backend_oracle.v1"
    normal_quality: Literal["sufficient"] = "sufficient"
    poor_quality: Literal["data_insufficient"] = "data_insufficient"
    late_report_creates_new_revision: Literal[True] = True
    correction_lineage_required: Literal[True] = True
    canonical_trace_required: Literal[True] = True


class PersistenceOracle(VerifierContract):
    schema_version: Literal["persistence_oracle.v1"] = "persistence_oracle.v1"
    restart_resources: tuple[NonEmptyStr, ...]
    duplicate_idempotency_returns_original: Literal[True] = True
    changed_body_conflicts: Literal[True] = True
    in_memory_authority_forbidden: Literal[True] = True


class ExpectedOracle(VerifierContract):
    schema_version: Literal["simulation_expected_oracle.v1"] = (
        "simulation_expected_oracle.v1"
    )
    scenario_id: NonEmptyStr
    generator: GeneratorOracle
    agents: AgentsOracle
    habit: HabitOracle
    memory: MemoryOracle
    cold_start: ColdStartOracle
    safety: SafetyOracle
    backend: BackendOracle
    persistence: PersistenceOracle


__all__ = [
    "ExpectedOracle",
    "SimulationAction",
    "SimulationActionTimeline",
]
