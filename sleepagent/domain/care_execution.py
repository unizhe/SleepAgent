"""Terminal care-plan and human-attested execution contracts.

Approval authorizes a plan.  Execution events record only what an authorized
human attests; they never prove delivery, efficacy, or a medical outcome.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sleepagent.domain.care_actions import (
    CareActionGovernanceError,
    CareActionProposal,
    CareActionType,
    CareApprovalGrant,
    CareAudience,
    stable_hash,
)


CARE_EXECUTION_POLICY_VERSION = "terminal-care-execution.v1"
CARE_PLAN_RENDERING_VERSION = "terminal-care-plan.zh-CN.v1"
CARE_EXECUTION_SCOPE = "product:sleep:care:execute"
HUMAN_ATTESTED_AUTHORITY = "human_attested"


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CareExecutionMode(str, Enum):
    ONE_TIME = "one_time"
    BOUNDED_PERIOD = "bounded_period"
    FOLLOW_UP_TASK = "follow_up_task"


class CareExecutionState(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    SUPERSEDED = "superseded"


class CareExecutionEventType(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class CareExecutionError(CareActionGovernanceError):
    """Raised when terminal care execution fails closed."""


class CareExecutionPolicy(FrozenContract):
    policy_version: str = CARE_EXECUTION_POLICY_VERSION
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_type: CareActionType
    executor_role: CareAudience
    execution_mode: CareExecutionMode
    start_required: bool
    direct_complete_allowed: bool
    required_parameters: tuple[str, ...] = ()

    @classmethod
    def define(
        cls,
        *,
        action_type: CareActionType,
        executor_role: CareAudience,
        execution_mode: CareExecutionMode,
        start_required: bool,
        direct_complete_allowed: bool,
        required_parameters: tuple[str, ...] = (),
    ) -> "CareExecutionPolicy":
        material = {
            "policy_version": CARE_EXECUTION_POLICY_VERSION,
            "action_type": action_type.value,
            "executor_role": executor_role.value,
            "execution_mode": execution_mode.value,
            "start_required": start_required,
            "direct_complete_allowed": direct_complete_allowed,
            "required_parameters": required_parameters,
        }
        return cls(policy_hash=stable_hash(material), **material)

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None:
        missing = set(self.required_parameters).difference(parameters)
        if missing:
            raise CareExecutionError(
                "Care plan is missing governed action parameters"
            )
        if self.action_type is CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME:
            _bounded_number(parameters, "tolerance_minutes", 0, 60)
        elif self.action_type is CareActionType.RECOMMEND_MORNING_LIGHT:
            _bounded_number(parameters, "minutes", 5, 45)


_EXECUTION_POLICIES = {
    CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME: CareExecutionPolicy.define(
        action_type=CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME,
        executor_role=CareAudience.ELDER,
        execution_mode=CareExecutionMode.BOUNDED_PERIOD,
        start_required=True,
        direct_complete_allowed=False,
        required_parameters=("tolerance_minutes",),
    ),
    CareActionType.RECOMMEND_MORNING_LIGHT: CareExecutionPolicy.define(
        action_type=CareActionType.RECOMMEND_MORNING_LIGHT,
        executor_role=CareAudience.ELDER,
        execution_mode=CareExecutionMode.ONE_TIME,
        start_required=False,
        direct_complete_allowed=True,
        required_parameters=("minutes",),
    ),
    CareActionType.REQUEST_MANUAL_FOLLOW_UP: CareExecutionPolicy.define(
        action_type=CareActionType.REQUEST_MANUAL_FOLLOW_UP,
        executor_role=CareAudience.FAMILY,
        execution_mode=CareExecutionMode.FOLLOW_UP_TASK,
        start_required=False,
        direct_complete_allowed=True,
    ),
    CareActionType.REQUEST_MORNING_REVIEW_FEEDBACK: CareExecutionPolicy.define(
        action_type=CareActionType.REQUEST_MORNING_REVIEW_FEEDBACK,
        executor_role=CareAudience.FAMILY,
        execution_mode=CareExecutionMode.FOLLOW_UP_TASK,
        start_required=False,
        direct_complete_allowed=True,
    ),
}


def execution_policy_for(action_type: CareActionType | str) -> CareExecutionPolicy:
    try:
        normalized = CareActionType(action_type)
        return _EXECUTION_POLICIES[normalized]
    except (ValueError, KeyError) as exc:
        raise CareExecutionError("Unsupported care execution action") from exc


def care_plan_semantic_hash(
    *,
    grant_hash: str,
    candidate_hash: str,
    action_type: CareActionType | str,
    execution_policy_hash: str,
    rendering_version: str = CARE_PLAN_RENDERING_VERSION,
) -> str:
    material = "\x1f".join(
        (
            grant_hash,
            candidate_hash,
            CareActionType(action_type).value,
            execution_policy_hash,
            rendering_version,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def care_plan_id_for(grant_id: str) -> str:
    digest = hashlib.sha256(grant_id.encode("utf-8")).hexdigest()
    return f"care-plan:{digest[:32]}"


class CarePlanEntry(FrozenContract):
    """Immutable execution snapshot created only from an active G8 grant."""

    schema_version: str = "care_plan_entry.v1"
    care_plan_id: str = Field(min_length=1)
    care_plan_semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_grant_id: str = Field(min_length=1)
    approval_grant_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_id: str = Field(min_length=1)
    proposal_semantic_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject_id: str = Field(min_length=1)
    action_type: CareActionType
    executor_role: CareAudience
    source_analysis_revision_id: str = Field(min_length=1)
    source_shared_analysis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_night_finalization_revision_id: str = Field(min_length=1)
    source_care_strategy_invocation_id: str = Field(min_length=1)
    source_care_strategy_version: str = Field(min_length=1)
    source_night_key: str = Field(min_length=1)
    evidence_references: tuple[str, ...] = Field(min_length=1, max_length=20)
    structured_action_parameters: dict[str, Any] = Field(default_factory=dict)
    approval_policy_version: str = Field(min_length=1)
    approval_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_authorization_scope: str = Field(min_length=1)
    approval_authorization_epoch: int = Field(ge=1)
    execution_policy_version: str = CARE_EXECUTION_POLICY_VERSION
    execution_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_mode: CareExecutionMode
    start_required: bool
    direct_complete_allowed: bool
    rendering_version: str = CARE_PLAN_RENDERING_VERSION
    created_at: datetime
    valid_from: datetime
    valid_until: datetime
    version: int = Field(default=1, ge=1, le=1)

    @classmethod
    def create(
        cls,
        grant: CareApprovalGrant,
        proposal: CareActionProposal,
        *,
        source_night_key: str,
        created_at: datetime | None = None,
    ) -> "CarePlanEntry":
        if not grant.is_usable(
            created_at or grant.issued_at,
            subject_id=proposal.candidate.subject_id,
            action_type=proposal.candidate.action_type,
            authorization_scope=proposal.authorization_scope,
            proposal_semantic_hash=proposal.proposal_semantic_hash,
        ):
            raise CareExecutionError("Care plan requires a usable ApprovalGrant")
        if grant.proposal_id != proposal.proposal_id:
            raise CareExecutionError("ApprovalGrant does not bind this proposal")
        policy = execution_policy_for(grant.action_type)
        if policy.executor_role is not grant.audience:
            raise CareExecutionError("Execution policy does not match grant audience")
        policy.validate_parameters(proposal.candidate.parameters)
        semantic_hash = care_plan_semantic_hash(
            grant_hash=grant.grant_hash,
            candidate_hash=grant.candidate_hash,
            action_type=grant.action_type,
            execution_policy_hash=policy.policy_hash,
        )
        return cls(
            care_plan_id=care_plan_id_for(grant.grant_id),
            care_plan_semantic_hash=semantic_hash,
            approval_grant_id=grant.grant_id,
            approval_grant_hash=grant.grant_hash,
            proposal_id=grant.proposal_id,
            proposal_semantic_hash=grant.proposal_semantic_hash,
            candidate_hash=grant.candidate_hash,
            subject_id=grant.subject_id,
            action_type=grant.action_type,
            executor_role=grant.audience,
            source_analysis_revision_id=proposal.candidate.source_analysis_revision_id,
            source_shared_analysis_sha256=(
                proposal.candidate.source_shared_analysis_sha256
            ),
            source_night_finalization_revision_id=(
                proposal.candidate.source_night_finalization_revision_id
            ),
            source_care_strategy_invocation_id=(
                proposal.candidate.source_care_strategy_invocation_id
            ),
            source_care_strategy_version=(
                proposal.candidate.source_care_strategy_version
            ),
            source_night_key=source_night_key,
            evidence_references=proposal.candidate.rationale_evidence_refs,
            structured_action_parameters=dict(proposal.candidate.parameters),
            approval_policy_version=grant.policy_version,
            approval_policy_hash=grant.policy_hash,
            approval_authorization_scope=grant.authorization_scope,
            approval_authorization_epoch=grant.authorization_epoch,
            execution_policy_hash=policy.policy_hash,
            execution_mode=policy.execution_mode,
            start_required=policy.start_required,
            direct_complete_allowed=policy.direct_complete_allowed,
            created_at=created_at or grant.issued_at,
            valid_from=grant.issued_at,
            valid_until=min(grant.expires_at, proposal.expires_at),
        )

    @model_validator(mode="after")
    def validate_plan(self) -> "CarePlanEntry":
        _aware(self.created_at)
        _aware(self.valid_from)
        _aware(self.valid_until)
        if not self.valid_from <= self.created_at < self.valid_until:
            raise ValueError("Care plan timestamps are outside grant authority")
        policy = execution_policy_for(self.action_type)
        policy.validate_parameters(self.structured_action_parameters)
        if (
            policy.executor_role is not self.executor_role
            or policy.policy_hash != self.execution_policy_hash
            or policy.execution_mode is not self.execution_mode
            or policy.start_required != self.start_required
            or policy.direct_complete_allowed != self.direct_complete_allowed
        ):
            raise ValueError("Care plan execution policy snapshot is invalid")
        expected = care_plan_semantic_hash(
            grant_hash=self.approval_grant_hash,
            candidate_hash=self.candidate_hash,
            action_type=self.action_type,
            execution_policy_hash=self.execution_policy_hash,
            rendering_version=self.rendering_version,
        )
        if expected != self.care_plan_semantic_hash:
            raise ValueError("Care plan semantic hash is invalid")
        if self.care_plan_id != care_plan_id_for(self.approval_grant_id):
            raise ValueError("Care plan identity is not grant-idempotent")
        return self


class CareExecutionStatus(FrozenContract):
    schema_version: str = "care_execution_status.v1"
    care_plan_id: str = Field(min_length=1)
    state: CareExecutionState
    version: int = Field(ge=1)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    invalidated_at: datetime | None = None
    invalidation_reason: str | None = Field(default=None, max_length=100)
    updated_at: datetime
    command_conflict_count: int = Field(default=0, ge=0)

    @classmethod
    def initial(cls, plan: CarePlanEntry) -> "CareExecutionStatus":
        return cls(
            care_plan_id=plan.care_plan_id,
            state=CareExecutionState.NOT_STARTED,
            version=1,
            updated_at=plan.created_at,
        )

    def transition(
        self,
        event_type: CareExecutionEventType,
        *,
        occurred_at: datetime,
        direct_complete_allowed: bool,
    ) -> "CareExecutionStatus":
        _aware(occurred_at)
        if event_type is CareExecutionEventType.STARTED:
            if self.state is not CareExecutionState.NOT_STARTED:
                raise CareExecutionError("START is invalid from current state")
            target = CareExecutionState.IN_PROGRESS
            times = {"started_at": occurred_at}
        elif event_type is CareExecutionEventType.COMPLETED:
            if self.state is CareExecutionState.NOT_STARTED:
                if not direct_complete_allowed:
                    raise CareExecutionError("This action requires START before COMPLETE")
            elif self.state is not CareExecutionState.IN_PROGRESS:
                raise CareExecutionError("COMPLETE is invalid from current state")
            target = CareExecutionState.COMPLETED
            times = {"completed_at": occurred_at}
        else:
            if self.state not in {
                CareExecutionState.NOT_STARTED,
                CareExecutionState.IN_PROGRESS,
            }:
                raise CareExecutionError("CANCEL is invalid from current state")
            target = CareExecutionState.CANCELLED
            times = {"cancelled_at": occurred_at}
        return self.model_copy(
            update={
                "state": target,
                "version": self.version + 1,
                "updated_at": occurred_at,
                **times,
            }
        )


class CareExecutionEvent(FrozenContract):
    schema_version: str = "care_execution_event.v1"
    event_id: str = Field(min_length=1)
    care_plan_id: str = Field(min_length=1)
    event_type: CareExecutionEventType
    actor_principal_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    actor_role: CareAudience
    actor_binding_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    source_authority: str = HUMAN_ATTESTED_AUTHORITY
    occurred_at: datetime
    recorded_at: datetime
    idempotency_key: str = Field(min_length=1, max_length=200)
    command_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    note: str | None = Field(default=None, max_length=500)
    authorization_epoch: int = Field(ge=1)
    previous_state: CareExecutionState
    resulting_state: CareExecutionState
    previous_version: int = Field(ge=1)
    resulting_version: int = Field(ge=2)

    @model_validator(mode="after")
    def validate_event(self) -> "CareExecutionEvent":
        _aware(self.occurred_at)
        _aware(self.recorded_at)
        if self.recorded_at < self.occurred_at:
            raise ValueError("Execution event cannot be recorded before it occurred")
        if self.resulting_version != self.previous_version + 1:
            raise ValueError("Execution event version chain is invalid")
        if self.source_authority != HUMAN_ATTESTED_AUTHORITY:
            raise ValueError("G9 execution events must be human-attested")
        return self


def command_fingerprint(
    *,
    care_plan_id: str,
    event_type: CareExecutionEventType | str,
    actor_id: str,
    note: str | None,
) -> str:
    normalized_note = "" if note is None else note
    material = "\x1f".join(
        (care_plan_id, CareExecutionEventType(event_type).value, actor_id, normalized_note)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def render_action_zh_cn(plan: CarePlanEntry) -> str:
    parameters = plan.structured_action_parameters
    if plan.action_type is CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME:
        minutes = int(parameters["tolerance_minutes"])
        return f"保持较固定的起床时间（前后误差不超过 {minutes} 分钟）"
    if plan.action_type is CareActionType.RECOMMEND_MORNING_LIGHT:
        minutes = int(parameters["minutes"])
        return f"早晨接受约 {minutes} 分钟自然光照"
    if plan.action_type is CareActionType.REQUEST_MANUAL_FOLLOW_UP:
        return "由家人进行一次人工照护跟进"
    if plan.action_type is CareActionType.REQUEST_MORNING_REVIEW_FEEDBACK:
        return "由家人在早晨记录一次睡眠回顾反馈"
    raise CareExecutionError("Unsupported terminal care-plan action")


def execution_state_zh_cn(state: CareExecutionState | str) -> str:
    return {
        CareExecutionState.NOT_STARTED: "尚未开始",
        CareExecutionState.IN_PROGRESS: "进行中",
        CareExecutionState.COMPLETED: "已完成（人工确认）",
        CareExecutionState.CANCELLED: "已取消",
        CareExecutionState.EXPIRED: "已过期",
        CareExecutionState.INVALIDATED: "授权已失效",
        CareExecutionState.SUPERSEDED: "已被新分析取代",
    }[CareExecutionState(state)]


def _bounded_number(
    values: Mapping[str, Any], key: str, minimum: float, maximum: float
) -> None:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CareExecutionError(f"Care action parameter {key} must be numeric")
    if not minimum <= float(value) <= maximum:
        raise CareExecutionError(f"Care action parameter {key} is outside policy")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Care execution timestamps must include an offset")
    return value


__all__ = [
    "CARE_EXECUTION_POLICY_VERSION",
    "CARE_EXECUTION_SCOPE",
    "CARE_PLAN_RENDERING_VERSION",
    "HUMAN_ATTESTED_AUTHORITY",
    "CareExecutionError",
    "CareExecutionEvent",
    "CareExecutionEventType",
    "CareExecutionMode",
    "CareExecutionPolicy",
    "CareExecutionState",
    "CareExecutionStatus",
    "CarePlanEntry",
    "care_plan_id_for",
    "care_plan_semantic_hash",
    "command_fingerprint",
    "execution_policy_for",
    "execution_state_zh_cn",
    "render_action_zh_cn",
]
