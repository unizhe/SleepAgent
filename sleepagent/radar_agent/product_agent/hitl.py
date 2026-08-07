"""Deterministic Human-in-the-Loop governance for SleepAgent.

HITL is deliberately a protocol around the Agent runtime, not another model
identity.  Agents may propose work; this module decides whether execution is
automatic, requires an accountable human decision, or is blocked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any, Callable, Protocol
from uuid import uuid4

from pydantic import Field, model_validator

from sleepagent.radar_agent.product_agent.contracts import StrictContract, stable_hash


HITL_POLICY_VERSION = "sleepagent-hitl-policy.v1"


class HumanDecisionError(ValueError):
    """Raised when a decision is unauthorized, stale, or invalid."""


class HumanRiskLevel(str, Enum):
    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    R4 = "R4"


class DecisionRoute(str, Enum):
    AUTO = "auto"
    INFORM = "inform"
    SINGLE_CONFIRM = "single_confirm"
    DUAL_REVIEW = "dual_review"
    PROFESSIONAL_REVIEW = "professional_review"
    HARD_BLOCK = "hard_block"


class HumanDecisionStatus(str, Enum):
    PENDING = "pending"
    PARTIALLY_APPROVED = "partially_approved"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    EXECUTING = "executing"
    COMMITTED = "committed"
    EXECUTION_FAILED = "execution_failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    HARD_BLOCKED = "hard_blocked"


class HumanDecisionChoice(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class DecisionExplanation(StrictContract):
    """The minimum explanation a person needs to make an accountable choice."""

    what_will_change: str = Field(..., min_length=1, max_length=1200)
    why_now: str = Field(..., min_length=1, max_length=1200)
    who_will_receive_or_be_affected: str = Field(..., min_length=1, max_length=600)
    duration_or_frequency: str = Field(..., min_length=1, max_length=600)
    how_to_revoke: str = Field(..., min_length=1, max_length=600)
    exact_changes: list[str] = Field(default_factory=list, max_length=50)


class ApprovalRequirement(StrictContract):
    requirement_id: str = Field(..., min_length=1)
    role: str = Field(..., min_length=1)
    count: int = Field(default=1, ge=1, le=3)
    professional: bool = False


class ActionProposal(StrictContract):
    proposal_id: str = Field(..., min_length=1)
    task_id: str | None = None
    episode_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    proposer_actor_id: str = Field(..., min_length=1)
    action_kind: str = Field(..., min_length=1)
    action_scope: str = Field(..., min_length=1)
    target_id: str = Field(..., min_length=1)
    target_hash: str = Field(..., min_length=64, max_length=64)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    policy_version: str = HITL_POLICY_VERSION
    payload: dict[str, Any]
    explanation: DecisionExplanation
    created_at: datetime
    expires_at: datetime
    authorization_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def expiration_follows_creation(self) -> "ActionProposal":
        if self.expires_at <= self.created_at:
            raise ValueError("proposal expiration must follow creation")
        return self


class HumanDecisionRecord(StrictContract):
    decision_record_id: str = Field(..., min_length=1)
    actor_id: str = Field(..., min_length=1)
    actor_role: str = Field(..., min_length=1)
    choice: HumanDecisionChoice
    target_hash: str = Field(..., min_length=64, max_length=64)
    policy_version: str = Field(..., min_length=1)
    decided_at: datetime
    reason: str | None = Field(default=None, max_length=1000)
    role_binding_id: str | None = None
    authorization_id: str | None = None


class HumanDecisionRequest(StrictContract):
    decision_id: str = Field(..., min_length=1)
    proposal: ActionProposal
    risk_level: HumanRiskLevel
    route: DecisionRoute
    requirements: list[ApprovalRequirement] = Field(default_factory=list, max_length=4)
    status: HumanDecisionStatus
    decisions: list[HumanDecisionRecord] = Field(default_factory=list, max_length=10)
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    execution_receipt_ref: str | None = None
    failure_reason: str | None = Field(default=None, max_length=1200)
    superseded_by: str | None = None

    @model_validator(mode="after")
    def route_and_state_are_consistent(self) -> "HumanDecisionRequest":
        if self.route in {DecisionRoute.AUTO, DecisionRoute.INFORM} and self.requirements:
            raise ValueError("automatic routes cannot require approval")
        if self.route == DecisionRoute.HARD_BLOCK and (
            self.status != HumanDecisionStatus.HARD_BLOCKED or self.requirements
        ):
            raise ValueError("hard-block decisions cannot be click-through")
        if self.route not in {
            DecisionRoute.AUTO,
            DecisionRoute.INFORM,
            DecisionRoute.HARD_BLOCK,
        } and not self.requirements:
            raise ValueError("human-routed decisions require approval requirements")
        return self


class ApprovalGrant(StrictContract):
    """One-time exact-target capability consumed by the Commit Controller."""

    grant_id: str = Field(..., min_length=1)
    decision_id: str = Field(..., min_length=1)
    target_id: str = Field(..., min_length=1)
    target_hash: str = Field(..., min_length=64, max_length=64)
    subject_id: str = Field(..., min_length=1)
    action_scope: str = Field(..., min_length=1)
    approver_actor_id: str = Field(..., min_length=1)
    approver_role: str = Field(..., min_length=1)
    policy_version: str = Field(..., min_length=1)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    authorization_id: str | None = None
    role_binding_id: str | None = None
    issued_at: datetime
    expires_at: datetime
    grant_hash: str = Field(..., min_length=64, max_length=64)


class HumanDecisionEvent(StrictContract):
    event_id: str = Field(..., min_length=1)
    decision_id: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    actor_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class HumanDecisionRepository(Protocol):
    def save(self, request: HumanDecisionRequest) -> HumanDecisionRequest: ...

    def get(self, decision_id: str) -> HumanDecisionRequest: ...

    def list(
        self,
        *,
        task_id: str | None = None,
        subject_id: str | None = None,
        status: HumanDecisionStatus | None = None,
    ) -> list[HumanDecisionRequest]: ...

    def append_event(self, event: HumanDecisionEvent) -> None: ...


class InMemoryHumanDecisionRepository:
    def __init__(self) -> None:
        self._items: dict[str, HumanDecisionRequest] = {}
        self._events: list[HumanDecisionEvent] = []
        self._lock = RLock()

    def save(self, request: HumanDecisionRequest) -> HumanDecisionRequest:
        with self._lock:
            self._items[request.decision_id] = request.model_copy(deep=True)
        return request

    def get(self, decision_id: str) -> HumanDecisionRequest:
        with self._lock:
            try:
                return self._items[decision_id].model_copy(deep=True)
            except KeyError as exc:
                raise KeyError(f"human decision not found: {decision_id}") from exc

    def list(
        self,
        *,
        task_id: str | None = None,
        subject_id: str | None = None,
        status: HumanDecisionStatus | None = None,
    ) -> list[HumanDecisionRequest]:
        with self._lock:
            values = list(self._items.values())
        return [
            value.model_copy(deep=True)
            for value in sorted(values, key=lambda item: (item.created_at, item.decision_id))
            if (task_id is None or value.proposal.task_id == task_id)
            and (subject_id is None or value.proposal.subject_id == subject_id)
            and (status is None or value.status == status)
        ]

    def append_event(self, event: HumanDecisionEvent) -> None:
        with self._lock:
            self._events.append(event.model_copy(deep=True))

    def list_events(self, decision_id: str) -> list[HumanDecisionEvent]:
        with self._lock:
            return [
                item.model_copy(deep=True)
                for item in self._events
                if item.decision_id == decision_id
            ]


class PersistentHumanDecisionRepository:
    """Adapter over RadarPersistenceStore without coupling that layer to product models."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def save(self, request: HumanDecisionRequest) -> HumanDecisionRequest:
        self._store.save_product_human_decision(
            decision_id=request.decision_id,
            task_id=request.proposal.task_id,
            episode_id=request.proposal.episode_id,
            subject_id=request.proposal.subject_id,
            status=request.status.value,
            target_hash=request.proposal.target_hash,
            decision_json=request.model_dump_json(),
            created_at=request.created_at,
            updated_at=request.updated_at,
        )
        return request

    def get(self, decision_id: str) -> HumanDecisionRequest:
        return HumanDecisionRequest.model_validate_json(
            self._store.get_product_human_decision_json(decision_id)
        )

    def list(
        self,
        *,
        task_id: str | None = None,
        subject_id: str | None = None,
        status: HumanDecisionStatus | None = None,
    ) -> list[HumanDecisionRequest]:
        return [
            HumanDecisionRequest.model_validate_json(item)
            for item in self._store.list_product_human_decision_json(
                task_id=task_id,
                subject_id=subject_id,
                status=status.value if status is not None else None,
            )
        ]

    def append_event(self, event: HumanDecisionEvent) -> None:
        self._store.append_product_human_decision_event(
            event_id=event.event_id,
            decision_id=event.decision_id,
            event_type=event.event_type,
            actor_id=event.actor_id,
            event_json=event.model_dump_json(),
            created_at=event.created_at,
        )


class HumanDecisionPolicy:
    """Versioned deterministic risk and responsibility routing."""

    version = HITL_POLICY_VERSION
    _hard_block_scopes = frozenset(
        {
            "override_emergency_block",
            "override_permission_denial",
            "bypass_professional_review",
        }
    )

    def classify(
        self, proposal: ActionProposal
    ) -> tuple[HumanRiskLevel, DecisionRoute, list[ApprovalRequirement]]:
        scope = proposal.action_scope
        kind = proposal.action_kind
        if scope in self._hard_block_scopes or proposal.metadata.get("hard_block"):
            return HumanRiskLevel.R4, DecisionRoute.HARD_BLOCK, []
        if kind in {"read_only", "explanation", "trend_view"}:
            return HumanRiskLevel.R0, DecisionRoute.AUTO, []
        if kind in {"notification_preview", "low_impact_suggestion"}:
            return HumanRiskLevel.R1, DecisionRoute.INFORM, []
        if kind in {"memory", "habit_profile", "care"}:
            return (
                HumanRiskLevel.R2,
                DecisionRoute.SINGLE_CONFIRM,
                [ApprovalRequirement(requirement_id="elder-owner", role="elder")],
            )
        if kind == "external_action":
            requirements = [
                ApprovalRequirement(requirement_id="elder-owner", role="elder")
            ]
            if proposal.metadata.get("professional_review_required"):
                requirements.append(
                    ApprovalRequirement(
                        requirement_id="professional-review",
                        role="doctor",
                        professional=True,
                    )
                )
                return HumanRiskLevel.R3, DecisionRoute.DUAL_REVIEW, requirements
            return HumanRiskLevel.R3, DecisionRoute.SINGLE_CONFIRM, requirements
        if kind in {"skill_release", "model_switch", "gateway_enable", "production_release"}:
            return (
                HumanRiskLevel.R3,
                DecisionRoute.DUAL_REVIEW,
                [
                    ApprovalRequirement(requirement_id="operator", role="admin"),
                    ApprovalRequirement(
                        requirement_id="independent-reviewer", role="reviewer"
                    ),
                ],
            )
        return HumanRiskLevel.R2, DecisionRoute.SINGLE_CONFIRM, [
            ApprovalRequirement(requirement_id="elder-owner", role="elder")
        ]


DecisionAuthorityValidator = Callable[
    [HumanDecisionRequest, str, str, str | None, str | None], None
]


class HumanDecisionService:
    def __init__(
        self,
        repository: HumanDecisionRepository | None = None,
        *,
        policy: HumanDecisionPolicy | None = None,
        authority_validator: DecisionAuthorityValidator | None = None,
    ) -> None:
        self.repository = repository or InMemoryHumanDecisionRepository()
        self.policy = policy or HumanDecisionPolicy()
        self.authority_validator = authority_validator
        self._lock = RLock()

    def create(self, proposal: ActionProposal) -> HumanDecisionRequest:
        if proposal.policy_version != self.policy.version:
            raise HumanDecisionError("proposal policy version is stale")
        risk, route, requirements = self.policy.classify(proposal)
        now = proposal.created_at
        status = {
            DecisionRoute.AUTO: HumanDecisionStatus.APPROVED,
            DecisionRoute.INFORM: HumanDecisionStatus.APPROVED,
            DecisionRoute.HARD_BLOCK: HumanDecisionStatus.HARD_BLOCKED,
        }.get(route, HumanDecisionStatus.PENDING)
        request = HumanDecisionRequest(
            decision_id=f"decision-{uuid4().hex}",
            proposal=proposal,
            risk_level=risk,
            route=route,
            requirements=requirements,
            status=status,
            created_at=now,
            updated_at=now,
            resolved_at=now if status != HumanDecisionStatus.PENDING else None,
        )
        self.repository.save(request)
        self._event(request, "decision.requested", proposal.proposer_actor_id)
        return request

    def decide(
        self,
        decision_id: str,
        *,
        actor_id: str,
        actor_role: str,
        choice: HumanDecisionChoice,
        target_hash: str,
        reason: str | None = None,
        role_binding_id: str | None = None,
        authorization_id: str | None = None,
        now: datetime | None = None,
    ) -> HumanDecisionRequest:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            request = self.repository.get(decision_id)
            self._require_pending(request, now)
            if request.proposal.target_hash != target_hash:
                raise HumanDecisionError("decision target is stale or was changed")
            matching = [item for item in request.requirements if item.role == actor_role]
            if not matching:
                raise HumanDecisionError(
                    f"role {actor_role!r} is not authorized for this decision"
                )
            if self.authority_validator is not None:
                self.authority_validator(
                    request,
                    actor_id,
                    actor_role,
                    role_binding_id,
                    authorization_id,
                )
            if any(
                item.actor_id == actor_id or item.actor_role == actor_role
                for item in request.decisions
            ):
                raise HumanDecisionError("this actor or required role already decided")
            record = HumanDecisionRecord(
                decision_record_id=f"record-{uuid4().hex}",
                actor_id=actor_id,
                actor_role=actor_role,
                choice=choice,
                target_hash=target_hash,
                policy_version=request.proposal.policy_version,
                decided_at=now,
                reason=reason,
                role_binding_id=role_binding_id,
                authorization_id=authorization_id,
            )
            decisions = [*request.decisions, record]
            if choice == HumanDecisionChoice.REJECT:
                status = HumanDecisionStatus.REJECTED
            elif self._requirements_satisfied(request.requirements, decisions):
                status = HumanDecisionStatus.APPROVED
            else:
                status = HumanDecisionStatus.PARTIALLY_APPROVED
            updated = request.model_copy(
                update={
                    "decisions": decisions,
                    "status": status,
                    "updated_at": now,
                    "resolved_at": (
                        now
                        if status
                        in {HumanDecisionStatus.APPROVED, HumanDecisionStatus.REJECTED}
                        else None
                    ),
                }
            )
            self.repository.save(updated)
            self._event(
                updated,
                (
                    "decision.approved"
                    if choice == HumanDecisionChoice.APPROVE
                    else "decision.rejected"
                ),
                actor_id,
            )
            return updated

    def revoke(
        self,
        decision_id: str,
        *,
        actor_id: str,
        actor_role: str,
        role_binding_id: str | None = None,
        authorization_id: str | None = None,
        now: datetime | None = None,
    ) -> HumanDecisionRequest:
        now = now or datetime.now(timezone.utc)
        with self._lock:
            request = self.repository.get(decision_id)
            if request.status not in {
                HumanDecisionStatus.PENDING,
                HumanDecisionStatus.PARTIALLY_APPROVED,
                HumanDecisionStatus.APPROVED,
                HumanDecisionStatus.EXECUTION_FAILED,
            }:
                raise HumanDecisionError("decision can no longer be revoked")
            owns_decision = any(
                item.actor_id == actor_id for item in request.decisions
            )
            if actor_role != "elder" and not owns_decision:
                raise HumanDecisionError("only the elder owner or approver may revoke")
            if self.authority_validator is not None:
                self.authority_validator(
                    request,
                    actor_id,
                    actor_role,
                    role_binding_id,
                    authorization_id,
                )
            updated = request.model_copy(
                update={
                    "status": HumanDecisionStatus.REVOKED,
                    "updated_at": now,
                    "resolved_at": now,
                }
            )
            self.repository.save(updated)
            self._event(updated, "decision.revoked", actor_id)
            return updated

    def expire(
        self, decision_id: str, *, now: datetime | None = None
    ) -> HumanDecisionRequest:
        now = now or datetime.now(timezone.utc)
        request = self.repository.get(decision_id)
        if request.status in {
            HumanDecisionStatus.PENDING,
            HumanDecisionStatus.PARTIALLY_APPROVED,
            HumanDecisionStatus.APPROVED,
        } and request.proposal.expires_at <= now:
            request = request.model_copy(
                update={
                    "status": HumanDecisionStatus.EXPIRED,
                    "updated_at": now,
                    "resolved_at": now,
                }
            )
            self.repository.save(request)
            self._event(request, "decision.expired", None)
        return request

    def approval_grant(
        self, decision_id: str, *, now: datetime | None = None
    ) -> ApprovalGrant:
        now = now or datetime.now(timezone.utc)
        request = self.repository.get(decision_id)
        self._require_exact_approved(request, now)
        elder_decision = next(
            (
                item
                for item in request.decisions
                if item.choice == HumanDecisionChoice.APPROVE
                and item.actor_role == "elder"
            ),
            next(
                item
                for item in request.decisions
                if item.choice == HumanDecisionChoice.APPROVE
            ),
        )
        if self.authority_validator is not None:
            for decision in request.decisions:
                if decision.choice == HumanDecisionChoice.APPROVE:
                    self.authority_validator(
                        request,
                        decision.actor_id,
                        decision.actor_role,
                        decision.role_binding_id,
                        decision.authorization_id,
                    )
        values = {
            "decision_id": request.decision_id,
            "target_id": request.proposal.target_id,
            "target_hash": request.proposal.target_hash,
            "subject_id": request.proposal.subject_id,
            "action_scope": request.proposal.action_scope,
            "approver_actor_id": elder_decision.actor_id,
            "approver_role": elder_decision.actor_role,
            "policy_version": request.proposal.policy_version,
            "fact_snapshot_hash": request.proposal.fact_snapshot_hash,
            "authorization_id": (
                elder_decision.authorization_id or request.proposal.authorization_id
            ),
            "role_binding_id": elder_decision.role_binding_id,
            "issued_at": now,
            "expires_at": request.proposal.expires_at,
        }
        return ApprovalGrant(
            grant_id=f"grant-{uuid4().hex}",
            grant_hash=stable_hash(values),
            **values,
        )

    def mark_execution(
        self,
        decision_id: str,
        *,
        status: HumanDecisionStatus,
        receipt_ref: str | None = None,
        failure_reason: str | None = None,
        now: datetime | None = None,
    ) -> HumanDecisionRequest:
        if status not in {
            HumanDecisionStatus.EXECUTING,
            HumanDecisionStatus.COMMITTED,
            HumanDecisionStatus.EXECUTION_FAILED,
            HumanDecisionStatus.OUTCOME_UNKNOWN,
        }:
            raise HumanDecisionError("invalid execution status")
        now = now or datetime.now(timezone.utc)
        request = self.repository.get(decision_id)
        if status == HumanDecisionStatus.EXECUTING:
            self._require_exact_approved(request, now)
        elif request.status not in {
            HumanDecisionStatus.EXECUTING,
            HumanDecisionStatus.APPROVED,
        }:
            raise HumanDecisionError("decision is not executable")
        updated = request.model_copy(
            update={
                "status": status,
                "execution_receipt_ref": receipt_ref,
                "failure_reason": failure_reason,
                "updated_at": now,
            }
        )
        self.repository.save(updated)
        self._event(updated, f"execution.{status.value}", None)
        return updated

    def _require_pending(
        self, request: HumanDecisionRequest, now: datetime
    ) -> None:
        if request.proposal.expires_at <= now:
            self.expire(request.decision_id, now=now)
            raise HumanDecisionError("decision request expired")
        if request.status not in {
            HumanDecisionStatus.PENDING,
            HumanDecisionStatus.PARTIALLY_APPROVED,
        }:
            raise HumanDecisionError(f"decision is {request.status.value}")

    @staticmethod
    def _requirements_satisfied(
        requirements: list[ApprovalRequirement],
        decisions: list[HumanDecisionRecord],
    ) -> bool:
        return all(
            sum(
                1
                for item in decisions
                if item.actor_role == requirement.role
                and item.choice == HumanDecisionChoice.APPROVE
            )
            >= requirement.count
            for requirement in requirements
        )

    def _require_exact_approved(
        self, request: HumanDecisionRequest, now: datetime
    ) -> None:
        if request.proposal.expires_at <= now:
            self.expire(request.decision_id, now=now)
            raise HumanDecisionError("approval expired before execution")
        if request.proposal.policy_version != self.policy.version:
            raise HumanDecisionError("approval policy version is stale")
        if request.status != HumanDecisionStatus.APPROVED:
            raise HumanDecisionError("decision is not approved for execution")
        if not self._requirements_satisfied(request.requirements, request.decisions):
            raise HumanDecisionError("approval requirements are incomplete")

    def _event(
        self,
        request: HumanDecisionRequest,
        event_type: str,
        actor_id: str | None,
    ) -> None:
        self.repository.append_event(
            HumanDecisionEvent(
                event_id=f"decision-event-{uuid4().hex}",
                decision_id=request.decision_id,
                event_type=event_type,
                actor_id=actor_id,
                detail={
                    "status": request.status.value,
                    "target_hash": request.proposal.target_hash,
                    "policy_version": request.proposal.policy_version,
                },
                created_at=request.updated_at,
            )
        )


__all__ = [
    "HITL_POLICY_VERSION",
    "ActionProposal",
    "ApprovalGrant",
    "ApprovalRequirement",
    "DecisionExplanation",
    "DecisionRoute",
    "HumanDecisionChoice",
    "HumanDecisionError",
    "HumanDecisionEvent",
    "HumanDecisionPolicy",
    "HumanDecisionRecord",
    "HumanDecisionRepository",
    "HumanDecisionRequest",
    "HumanDecisionService",
    "HumanDecisionStatus",
    "HumanRiskLevel",
    "InMemoryHumanDecisionRepository",
    "PersistentHumanDecisionRepository",
]
