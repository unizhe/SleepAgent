"""Deterministic Human-in-the-Loop governance for SleepAgent.

HITL is deliberately a protocol around the Agent runtime, not another model
identity.  Agents may propose work; this module decides whether execution is
automatic, requires an accountable human decision, or is blocked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock
from typing import Any, Callable, Protocol
from uuid import uuid4
from weakref import WeakSet

from pydantic import Field, model_validator

from sleepagent.radar_agent.product_agent.contracts import (
    FrozenContract,
    StrictContract,
    stable_hash,
)


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


class ActionProposal(FrozenContract):
    proposal_id: str = Field(..., min_length=1)
    task_id: str | None = None
    episode_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    proposer_actor_id: str = Field(..., min_length=1)
    action_kind: str = Field(..., min_length=1)
    action_scope: str = Field(..., min_length=1)
    target_id: str = Field(..., min_length=1)
    target_hash: str = Field(..., min_length=64, max_length=64)
    fact_snapshot_id: str = Field(..., min_length=1)
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


class HumanDecisionRecord(FrozenContract):
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


class ApprovalGrant(FrozenContract):
    """Persisted exact-target grant; it is inert until authority verification."""

    grant_id: str = Field(..., min_length=1)
    decision_id: str = Field(..., min_length=1)
    decision_revision: int = Field(..., ge=1)
    proposal_id: str = Field(..., min_length=1)
    proposal_hash: str = Field(..., min_length=64, max_length=64)
    approving_records: tuple[HumanDecisionRecord, ...] = Field(
        min_length=1,
        max_length=10,
    )
    approving_records_hash: str = Field(..., min_length=64, max_length=64)
    target_id: str = Field(..., min_length=1)
    target_hash: str = Field(..., min_length=64, max_length=64)
    subject_id: str = Field(..., min_length=1)
    action_scope: str = Field(..., min_length=1)
    approver_actor_id: str = Field(..., min_length=1)
    approver_role: str = Field(..., min_length=1)
    policy_version: str = Field(..., min_length=1)
    fact_snapshot_id: str = Field(..., min_length=1)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    authorization_id: str | None = None
    role_binding_id: str | None = None
    issued_at: datetime
    expires_at: datetime
    idempotency_key: str = Field(..., min_length=1)
    grant_hash: str = Field(..., min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_integrity(self) -> "ApprovalGrant":
        if self.expires_at <= self.issued_at:
            raise ValueError("grant expiration must follow issuance")
        if len({item.decision_record_id for item in self.approving_records}) != len(
            self.approving_records
        ):
            raise ValueError("grant approval records must be unique")
        if any(
            item.choice != HumanDecisionChoice.APPROVE
            for item in self.approving_records
        ):
            raise ValueError("grant may contain only approving records")
        if any(
            item.target_hash != self.target_hash
            or item.policy_version != self.policy_version
            or item.decided_at > self.issued_at
            for item in self.approving_records
        ):
            raise ValueError("grant approval records are not exactly bound")
        records_hash = _approving_records_hash(self.approving_records)
        if self.approving_records_hash != records_hash:
            raise ValueError("grant approving-record hash mismatch")
        if not any(
            item.actor_id == self.approver_actor_id
            and item.actor_role == self.approver_role
            for item in self.approving_records
        ):
            raise ValueError("grant primary approver is absent from approval records")
        expected_hash = stable_hash(_grant_hash_material(self))
        if self.grant_hash != expected_hash:
            raise ValueError("grant hash mismatch")
        if self.grant_id != f"grant-{expected_hash[:32]}":
            raise ValueError("grant id/hash mismatch")
        return self


class HumanDecisionRequest(FrozenContract):
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
    revision: int = Field(default=0, ge=0)
    active_grant: ApprovalGrant | None = None

    @model_validator(mode="after")
    def route_and_state_are_consistent(self) -> "HumanDecisionRequest":
        if self.updated_at < self.created_at:
            raise ValueError("decision updated_at cannot precede created_at")
        if self.resolved_at is not None and (
            self.resolved_at < self.created_at or self.resolved_at > self.updated_at
        ):
            raise ValueError("decision resolved_at is outside its lifecycle")
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
        if len({item.actor_id for item in self.decisions}) != len(self.decisions):
            raise ValueError("a decision actor may decide only once")
        if len({item.decision_record_id for item in self.decisions}) != len(
            self.decisions
        ):
            raise ValueError("decision record ids must be unique")
        for decision in self.decisions:
            if (
                decision.target_hash != self.proposal.target_hash
                or decision.policy_version != self.proposal.policy_version
                or decision.decided_at < self.created_at
                or decision.decided_at > self.updated_at
            ):
                raise ValueError("decision record is not bound to its proposal")
        execution_states = {
            HumanDecisionStatus.EXECUTING,
            HumanDecisionStatus.COMMITTED,
            HumanDecisionStatus.EXECUTION_FAILED,
            HumanDecisionStatus.OUTCOME_UNKNOWN,
        }
        if (self.status in execution_states) != (self.active_grant is not None):
            raise ValueError("execution state and active grant must align")
        if self.active_grant is not None:
            grant = self.active_grant
            if not all(
                sum(
                    1
                    for item in self.decisions
                    if item.actor_role == requirement.role
                    and item.choice == HumanDecisionChoice.APPROVE
                )
                >= requirement.count
                for requirement in self.requirements
            ):
                raise ValueError("active grant has incomplete approval requirements")
            approving = tuple(
                sorted(
                    (
                        item
                        for item in self.decisions
                        if item.choice == HumanDecisionChoice.APPROVE
                    ),
                    key=lambda item: (
                        item.actor_role,
                        item.actor_id,
                        item.decision_record_id,
                    ),
                )
            )
            primary = next(
                (item for item in approving if item.actor_role == "elder"),
                approving[0] if approving else None,
            )
            expected_revision = (
                self.revision
                if self.status == HumanDecisionStatus.EXECUTING
                else self.revision - 1
            )
            if (
                grant.decision_id != self.decision_id
                or grant.decision_revision != expected_revision
                or grant.proposal_id != self.proposal.proposal_id
                or grant.proposal_hash != stable_hash(self.proposal)
                or grant.approving_records != approving
                or grant.target_id != self.proposal.target_id
                or grant.target_hash != self.proposal.target_hash
                or grant.subject_id != self.proposal.subject_id
                or grant.action_scope != self.proposal.action_scope
                or grant.policy_version != self.proposal.policy_version
                or grant.fact_snapshot_id != self.proposal.fact_snapshot_id
                or grant.fact_snapshot_hash != self.proposal.fact_snapshot_hash
                or grant.expires_at != self.proposal.expires_at
                or primary is None
                or grant.approver_actor_id != primary.actor_id
                or grant.approver_role != primary.actor_role
                or grant.role_binding_id != primary.role_binding_id
                or grant.authorization_id
                != (primary.authorization_id or self.proposal.authorization_id)
                or grant.issued_at < self.created_at
                or grant.issued_at > self.updated_at
            ):
                raise ValueError("active grant is not exactly bound to this decision")
        return self


def _validated_request_update(
    request: HumanDecisionRequest,
    **updates: Any,
) -> HumanDecisionRequest:
    values = request.model_dump(mode="python")
    values.update(updates)
    return HumanDecisionRequest.model_validate(values)


def _approving_records_hash(
    records: tuple[HumanDecisionRecord, ...],
) -> str:
    return stable_hash(
        [item.model_dump(mode="json") for item in records]
    )


def _grant_hash_material(grant: ApprovalGrant) -> dict[str, Any]:
    return grant.model_dump(
        mode="json",
        exclude={"grant_id", "grant_hash"},
    )


def _build_verified_capability_contract() -> tuple[type[Any], Callable[..., Any]]:
    issued: WeakSet[Any] = WeakSet()

    class _VerifiedApprovalCapability:
        """Opaque in-process proof that HDS won the authority acquisition CAS."""

        __slots__ = ("_grant", "__weakref__")

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise TypeError(
                "VerifiedApprovalCapability can only be issued by HumanDecisionService"
            )

        def __setattr__(self, _name: str, _value: object) -> None:
            raise AttributeError("VerifiedApprovalCapability is immutable")

        @property
        def grant(self) -> ApprovalGrant:
            return self._grant.model_copy(deep=True)

        def __reduce__(self) -> object:
            raise TypeError("VerifiedApprovalCapability is not serializable")

        def __reduce_ex__(self, _protocol: int) -> object:
            raise TypeError("VerifiedApprovalCapability is not serializable")

        def validate_exact_binding(
            self,
            *,
            decision_id: str,
            proposal_id: str,
            actor_id: str,
            actor_role: str,
            subject_id: str,
            target_id: str,
            target_hash: str,
            action_scope: str,
            fact_snapshot_id: str,
            fact_snapshot_hash: str,
            policy_version: str,
            idempotency_key: str,
            now: datetime | None = None,
        ) -> None:
            if self not in issued:
                raise HumanDecisionError("approval capability was not issued by HDS")
            grant = self._grant
            expected = (
                grant.decision_id,
                grant.proposal_id,
                grant.approver_actor_id,
                grant.approver_role,
                grant.subject_id,
                grant.target_id,
                grant.target_hash,
                grant.action_scope,
                grant.fact_snapshot_id,
                grant.fact_snapshot_hash,
                grant.policy_version,
                grant.idempotency_key,
            )
            actual = (
                decision_id,
                proposal_id,
                actor_id,
                actor_role,
                subject_id,
                target_id,
                target_hash,
                action_scope,
                fact_snapshot_id,
                fact_snapshot_hash,
                policy_version,
                idempotency_key,
            )
            if expected != actual:
                raise HumanDecisionError(
                    "verified approval capability is not bound to this commit"
                )
            observed_at = _aware_utc(now or grant.issued_at)
            if observed_at < grant.issued_at:
                raise HumanDecisionError("capability cannot predate its acquisition")
            # Expiry and revoke state are checked by HDS before the atomic
            # APPROVED -> EXECUTING transition. EXECUTING is the linearization
            # point, so crash recovery remains valid after wall-clock expiry.
            if grant.expires_at <= grant.issued_at:
                raise HumanDecisionError("capability was acquired after expiry")
            if stable_hash(_grant_hash_material(grant)) != grant.grant_hash:
                raise HumanDecisionError(
                    "verified approval capability integrity mismatch"
                )

    def issue(grant: ApprovalGrant) -> _VerifiedApprovalCapability:
        capability = object.__new__(_VerifiedApprovalCapability)
        object.__setattr__(capability, "_grant", grant.model_copy(deep=True))
        issued.add(capability)
        return capability

    _VerifiedApprovalCapability.__name__ = "VerifiedApprovalCapability"
    _VerifiedApprovalCapability.__qualname__ = "VerifiedApprovalCapability"
    return _VerifiedApprovalCapability, issue


VerifiedApprovalCapability, _CAPABILITY_ISSUER = (
    _build_verified_capability_contract()
)


class HumanDecisionEvent(StrictContract):
    event_id: str = Field(..., min_length=1)
    decision_id: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    actor_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class HumanDecisionRepository(Protocol):
    def create(
        self, request: HumanDecisionRequest
    ) -> tuple[HumanDecisionRequest, bool]: ...

    def compare_and_set(
        self,
        request: HumanDecisionRequest,
        *,
        expected_status: HumanDecisionStatus,
        expected_updated_at: datetime,
        expected_revision: int,
    ) -> HumanDecisionRequest: ...

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

    def create(
        self, request: HumanDecisionRequest
    ) -> tuple[HumanDecisionRequest, bool]:
        request = HumanDecisionRequest.model_validate(
            request.model_dump(mode="python")
        )
        with self._lock:
            current = self._items.get(request.decision_id)
            if current is not None:
                if current.proposal != request.proposal:
                    raise HumanDecisionError(
                        "decision proposal identity already has different content"
                    )
                return current.model_copy(deep=True), False
            self._items[request.decision_id] = request.model_copy(deep=True)
        return request.model_copy(deep=True), True

    def compare_and_set(
        self,
        request: HumanDecisionRequest,
        *,
        expected_status: HumanDecisionStatus,
        expected_updated_at: datetime,
        expected_revision: int,
    ) -> HumanDecisionRequest:
        request = HumanDecisionRequest.model_validate(
            request.model_dump(mode="python")
        )
        if request.updated_at <= _aware_utc(expected_updated_at):
            raise HumanDecisionError("human decision updated_at must advance")
        with self._lock:
            current = self._items.get(request.decision_id)
            if current is None:
                raise KeyError(
                    f"human decision not found: {request.decision_id}"
                )
            if (
                current.status != expected_status
                or current.updated_at != expected_updated_at
                or current.revision != expected_revision
            ):
                raise HumanDecisionError("human decision changed concurrently")
            if request.revision != expected_revision + 1:
                raise HumanDecisionError("human decision revision must advance once")
            self._items[request.decision_id] = request.model_copy(deep=True)
        return request.model_copy(deep=True)

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

    def create(
        self, request: HumanDecisionRequest
    ) -> tuple[HumanDecisionRequest, bool]:
        request = HumanDecisionRequest.model_validate(
            request.model_dump(mode="python")
        )
        created = self._store.create_product_human_decision(
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
        current = self.get(request.decision_id)
        if current.proposal != request.proposal:
            raise HumanDecisionError(
                "decision proposal identity already has different content"
            )
        return current, created

    def compare_and_set(
        self,
        request: HumanDecisionRequest,
        *,
        expected_status: HumanDecisionStatus,
        expected_updated_at: datetime,
        expected_revision: int,
    ) -> HumanDecisionRequest:
        request = HumanDecisionRequest.model_validate(
            request.model_dump(mode="python")
        )
        if request.revision != expected_revision + 1:
            raise HumanDecisionError("human decision revision must advance once")
        if request.updated_at <= _aware_utc(expected_updated_at):
            raise HumanDecisionError("human decision updated_at must advance")
        changed = self._store.compare_and_set_product_human_decision(
            decision_id=request.decision_id,
            expected_status=expected_status.value,
            expected_updated_at=expected_updated_at,
            task_id=request.proposal.task_id,
            episode_id=request.proposal.episode_id,
            subject_id=request.proposal.subject_id,
            status=request.status.value,
            target_hash=request.proposal.target_hash,
            decision_json=request.model_dump_json(),
            updated_at=request.updated_at,
        )
        if not changed:
            current = self.get(request.decision_id)
            if current.revision != expected_revision:
                raise HumanDecisionError("human decision changed concurrently")
            raise HumanDecisionError("human decision compare-and-set failed")
        return request.model_copy(deep=True)

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
    __capability_issuer = staticmethod(_CAPABILITY_ISSUER)

    def __init__(
        self,
        repository: HumanDecisionRepository | None = None,
        *,
        policy: HumanDecisionPolicy | None = None,
        authority_validator: DecisionAuthorityValidator | None = None,
    ) -> None:
        self._repository = repository or InMemoryHumanDecisionRepository()
        self.policy = policy or HumanDecisionPolicy()
        self.authority_validator = authority_validator
        self._lock = RLock()
        self.__issued_capabilities: WeakSet[Any] = WeakSet()

    def __issue_capability(
        self,
        grant: ApprovalGrant,
    ) -> VerifiedApprovalCapability:
        capability = self.__capability_issuer(grant)
        self.__issued_capabilities.add(capability)
        return capability

    def get(self, decision_id: str) -> HumanDecisionRequest:
        return self._repository.get(decision_id)

    def list(
        self,
        *,
        task_id: str | None = None,
        subject_id: str | None = None,
        status: HumanDecisionStatus | None = None,
    ) -> list[HumanDecisionRequest]:
        return self._repository.list(
            task_id=task_id,
            subject_id=subject_id,
            status=status,
        )

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
            decision_id=(
                f"decision-{stable_hash({'proposal_id': proposal.proposal_id})[:32]}"
            ),
            proposal=proposal,
            risk_level=risk,
            route=route,
            requirements=requirements,
            status=status,
            created_at=now,
            updated_at=now,
            resolved_at=now if status != HumanDecisionStatus.PENDING else None,
        )
        persisted, created = self._repository.create(request)
        if created:
            self._event(persisted, "decision.requested", proposal.proposer_actor_id)
        return persisted

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
        now = _aware_utc(now or datetime.now(timezone.utc))
        with self._lock:
            request = self.get(decision_id)
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
            if any(item.actor_id == actor_id for item in request.decisions):
                raise HumanDecisionError("this actor already decided")
            record = HumanDecisionRecord(
                decision_record_id=f"record-{uuid4().hex}",
                actor_id=actor_id,
                actor_role=actor_role,
                choice=choice,
                target_hash=target_hash,
                policy_version=request.proposal.policy_version,
                decided_at=_monotonic_transition_time(request.updated_at, now),
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
            transition_at = _monotonic_transition_time(request.updated_at, now)
            updated = _validated_request_update(
                request,
                decisions=decisions,
                status=status,
                updated_at=transition_at,
                resolved_at=(
                    transition_at
                    if status
                    in {HumanDecisionStatus.APPROVED, HumanDecisionStatus.REJECTED}
                    else None
                ),
                revision=request.revision + 1,
            )
            updated = self._repository.compare_and_set(
                updated,
                expected_status=request.status,
                expected_updated_at=request.updated_at,
                expected_revision=request.revision,
            )
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
        now = _aware_utc(now or datetime.now(timezone.utc))
        with self._lock:
            request = self.get(decision_id)
            if request.status not in {
                HumanDecisionStatus.PENDING,
                HumanDecisionStatus.PARTIALLY_APPROVED,
                HumanDecisionStatus.APPROVED,
            }:
                raise HumanDecisionError("decision can no longer be revoked")
            if request.proposal.expires_at <= _monotonic_transition_time(
                request.updated_at,
                now,
            ):
                self.expire(
                    request.decision_id,
                    now=max(now, request.proposal.expires_at),
                )
                raise HumanDecisionError("decision request expired")
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
            transition_at = _monotonic_transition_time(request.updated_at, now)
            updated = _validated_request_update(
                request,
                status=HumanDecisionStatus.REVOKED,
                updated_at=transition_at,
                resolved_at=transition_at,
                revision=request.revision + 1,
            )
            updated = self._repository.compare_and_set(
                updated,
                expected_status=request.status,
                expected_updated_at=request.updated_at,
                expected_revision=request.revision,
            )
            self._event(updated, "decision.revoked", actor_id)
            return updated

    def expire(
        self, decision_id: str, *, now: datetime | None = None
    ) -> HumanDecisionRequest:
        now = _aware_utc(now or datetime.now(timezone.utc))
        with self._lock:
            request = self.get(decision_id)
            if not (
                request.status
                in {
                    HumanDecisionStatus.PENDING,
                    HumanDecisionStatus.PARTIALLY_APPROVED,
                    HumanDecisionStatus.APPROVED,
                }
                and request.proposal.expires_at <= now
            ):
                return request
            transition_at = _monotonic_transition_time(request.updated_at, now)
            expired = _validated_request_update(
                request,
                status=HumanDecisionStatus.EXPIRED,
                updated_at=transition_at,
                resolved_at=transition_at,
                revision=request.revision + 1,
            )
            expired = self._repository.compare_and_set(
                expired,
                expected_status=request.status,
                expected_updated_at=request.updated_at,
                expected_revision=request.revision,
            )
            self._event(expired, "decision.expired", None)
            return expired

    def acquire_verified_capability(
        self,
        decision_id: str,
        *,
        expected_proposal_id: str,
        expected_subject_id: str,
        expected_target_id: str,
        expected_target_hash: str,
        expected_action_scope: str,
        expected_fact_snapshot_id: str,
        expected_fact_snapshot_hash: str,
        expected_policy_version: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> VerifiedApprovalCapability:
        if not idempotency_key:
            raise HumanDecisionError("grant acquisition requires an idempotency key")
        now = _aware_utc(now or datetime.now(timezone.utc))
        with self._lock:
            request = self.get(decision_id)
            retry = self._verified_retry(
                request,
                expected_proposal_id=expected_proposal_id,
                expected_subject_id=expected_subject_id,
                expected_target_id=expected_target_id,
                expected_target_hash=expected_target_hash,
                expected_action_scope=expected_action_scope,
                expected_fact_snapshot_id=expected_fact_snapshot_id,
                expected_fact_snapshot_hash=expected_fact_snapshot_hash,
                expected_policy_version=expected_policy_version,
                idempotency_key=idempotency_key,
            )
            if retry is not None:
                return retry
            self._require_exact_approved(request, now)
            self._require_expected_binding(
                request,
                proposal_id=expected_proposal_id,
                subject_id=expected_subject_id,
                target_id=expected_target_id,
                target_hash=expected_target_hash,
                action_scope=expected_action_scope,
                fact_snapshot_id=expected_fact_snapshot_id,
                fact_snapshot_hash=expected_fact_snapshot_hash,
                policy_version=expected_policy_version,
            )
            approving = self._approving_records(request)
            self._revalidate_approvers(request, approving)
            transition_at = _monotonic_transition_time(request.updated_at, now)
            grant = self._build_grant(
                request,
                approving=approving,
                issued_at=transition_at,
                idempotency_key=idempotency_key,
            )
            executing = _validated_request_update(
                request,
                status=HumanDecisionStatus.EXECUTING,
                active_grant=grant,
                updated_at=transition_at,
                revision=request.revision + 1,
            )
            try:
                executing = self._repository.compare_and_set(
                    executing,
                    expected_status=request.status,
                    expected_updated_at=request.updated_at,
                    expected_revision=request.revision,
                )
            except HumanDecisionError:
                current = self.get(decision_id)
                retry = self._verified_retry(
                    current,
                    expected_proposal_id=expected_proposal_id,
                    expected_subject_id=expected_subject_id,
                    expected_target_id=expected_target_id,
                    expected_target_hash=expected_target_hash,
                    expected_action_scope=expected_action_scope,
                    expected_fact_snapshot_id=expected_fact_snapshot_id,
                    expected_fact_snapshot_hash=expected_fact_snapshot_hash,
                    expected_policy_version=expected_policy_version,
                    idempotency_key=idempotency_key,
                )
                if retry is not None:
                    return retry
                raise
            self._event(executing, "execution.executing", None)
            return self.__issue_capability(grant)

    def record_execution_result(
        self,
        capability: VerifiedApprovalCapability,
        *,
        status: HumanDecisionStatus,
        receipt_ref: str | None = None,
        failure_reason: str | None = None,
        now: datetime | None = None,
    ) -> HumanDecisionRequest:
        if type(capability) is not VerifiedApprovalCapability:
            raise HumanDecisionError("execution result requires a verified capability")
        if capability not in self.__issued_capabilities:
            raise HumanDecisionError(
                "execution capability was not acquired from this authority service"
            )
        if status not in {
            HumanDecisionStatus.COMMITTED,
            HumanDecisionStatus.EXECUTION_FAILED,
            HumanDecisionStatus.OUTCOME_UNKNOWN,
        }:
            raise HumanDecisionError("invalid execution result status")
        grant = capability.grant
        now = _aware_utc(now or datetime.now(timezone.utc))
        with self._lock:
            request = self.get(grant.decision_id)
            if request.status == status and request.active_grant == grant:
                if (
                    request.execution_receipt_ref == receipt_ref
                    and request.failure_reason == failure_reason
                ):
                    return request
                raise HumanDecisionError(
                    "execution result was already recorded differently"
                )
            if (
                request.status != HumanDecisionStatus.EXECUTING
                or request.active_grant != grant
            ):
                raise HumanDecisionError(
                    "execution capability is not active for this decision"
                )
            capability.validate_exact_binding(
                decision_id=request.decision_id,
                proposal_id=request.proposal.proposal_id,
                actor_id=grant.approver_actor_id,
                actor_role=grant.approver_role,
                subject_id=request.proposal.subject_id,
                target_id=request.proposal.target_id,
                target_hash=request.proposal.target_hash,
                action_scope=request.proposal.action_scope,
                fact_snapshot_id=request.proposal.fact_snapshot_id,
                fact_snapshot_hash=request.proposal.fact_snapshot_hash,
                policy_version=request.proposal.policy_version,
                idempotency_key=grant.idempotency_key,
                now=grant.issued_at,
            )
            transition_at = _monotonic_transition_time(request.updated_at, now)
            updated = _validated_request_update(
                request,
                status=status,
                execution_receipt_ref=receipt_ref,
                failure_reason=failure_reason,
                updated_at=transition_at,
                revision=request.revision + 1,
            )
            updated = self._repository.compare_and_set(
                updated,
                expected_status=request.status,
                expected_updated_at=request.updated_at,
                expected_revision=request.revision,
            )
            self._event(updated, f"execution.{status.value}", None)
            return updated

    def _require_pending(
        self, request: HumanDecisionRequest, now: datetime
    ) -> None:
        if request.proposal.expires_at <= _monotonic_transition_time(
            request.updated_at,
            now,
        ):
            self.expire(
                request.decision_id,
                now=max(now, request.proposal.expires_at),
            )
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
        if request.proposal.expires_at <= _monotonic_transition_time(
            request.updated_at,
            now,
        ):
            self.expire(
                request.decision_id,
                now=max(now, request.proposal.expires_at),
            )
            raise HumanDecisionError("approval expired before execution")
        if request.proposal.policy_version != self.policy.version:
            raise HumanDecisionError("approval policy version is stale")
        if request.status != HumanDecisionStatus.APPROVED:
            raise HumanDecisionError("decision is not approved for execution")
        if not self._requirements_satisfied(request.requirements, request.decisions):
            raise HumanDecisionError("approval requirements are incomplete")
        if request.active_grant is not None:
            raise HumanDecisionError("decision already has an active grant")
        if not self._approving_records(request):
            raise HumanDecisionError("approval has no accountable human record")

    @staticmethod
    def _require_expected_binding(
        request: HumanDecisionRequest,
        *,
        proposal_id: str,
        subject_id: str,
        target_id: str,
        target_hash: str,
        action_scope: str,
        fact_snapshot_id: str,
        fact_snapshot_hash: str,
        policy_version: str,
    ) -> None:
        expected = (
            request.proposal.proposal_id,
            request.proposal.subject_id,
            request.proposal.target_id,
            request.proposal.target_hash,
            request.proposal.action_scope,
            request.proposal.fact_snapshot_id,
            request.proposal.fact_snapshot_hash,
            request.proposal.policy_version,
        )
        actual = (
            proposal_id,
            subject_id,
            target_id,
            target_hash,
            action_scope,
            fact_snapshot_id,
            fact_snapshot_hash,
            policy_version,
        )
        if expected != actual:
            raise HumanDecisionError("grant request is not bound to its proposal")

    @staticmethod
    def _approving_records(
        request: HumanDecisionRequest,
    ) -> tuple[HumanDecisionRecord, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in request.decisions
                    if item.choice == HumanDecisionChoice.APPROVE
                ),
                key=lambda item: (
                    item.actor_role,
                    item.actor_id,
                    item.decision_record_id,
                ),
            )
        )

    def _revalidate_approvers(
        self,
        request: HumanDecisionRequest,
        approving: tuple[HumanDecisionRecord, ...],
    ) -> None:
        if self.authority_validator is None:
            return
        for decision in approving:
            self.authority_validator(
                request,
                decision.actor_id,
                decision.actor_role,
                decision.role_binding_id,
                decision.authorization_id,
            )

    @staticmethod
    def _build_grant(
        request: HumanDecisionRequest,
        *,
        approving: tuple[HumanDecisionRecord, ...],
        issued_at: datetime,
        idempotency_key: str,
    ) -> ApprovalGrant:
        primary = next(
            (item for item in approving if item.actor_role == "elder"),
            approving[0],
        )
        values: dict[str, Any] = {
            "decision_id": request.decision_id,
            "decision_revision": request.revision + 1,
            "proposal_id": request.proposal.proposal_id,
            "proposal_hash": stable_hash(request.proposal),
            "approving_records": approving,
            "approving_records_hash": _approving_records_hash(approving),
            "target_id": request.proposal.target_id,
            "target_hash": request.proposal.target_hash,
            "subject_id": request.proposal.subject_id,
            "action_scope": request.proposal.action_scope,
            "approver_actor_id": primary.actor_id,
            "approver_role": primary.actor_role,
            "policy_version": request.proposal.policy_version,
            "fact_snapshot_id": request.proposal.fact_snapshot_id,
            "fact_snapshot_hash": request.proposal.fact_snapshot_hash,
            "authorization_id": (
                primary.authorization_id or request.proposal.authorization_id
            ),
            "role_binding_id": primary.role_binding_id,
            "issued_at": issued_at,
            "expires_at": request.proposal.expires_at,
            "idempotency_key": idempotency_key,
        }
        draft = ApprovalGrant.model_construct(
            grant_id="unverified",
            grant_hash="0" * 64,
            **values,
        )
        grant_hash = stable_hash(_grant_hash_material(draft))
        return ApprovalGrant(
            grant_id=f"grant-{grant_hash[:32]}",
            grant_hash=grant_hash,
            **values,
        )

    def _verified_retry(
        self,
        request: HumanDecisionRequest,
        *,
        expected_proposal_id: str,
        expected_subject_id: str,
        expected_target_id: str,
        expected_target_hash: str,
        expected_action_scope: str,
        expected_fact_snapshot_id: str,
        expected_fact_snapshot_hash: str,
        expected_policy_version: str,
        idempotency_key: str,
    ) -> VerifiedApprovalCapability | None:
        grant = request.active_grant
        if grant is None:
            return None
        if request.status not in {
            HumanDecisionStatus.EXECUTING,
            HumanDecisionStatus.COMMITTED,
            HumanDecisionStatus.EXECUTION_FAILED,
            HumanDecisionStatus.OUTCOME_UNKNOWN,
        }:
            raise HumanDecisionError("decision grant is not in an execution state")
        if grant.idempotency_key != idempotency_key:
            raise HumanDecisionError("approval grant was already acquired")
        approving = self._approving_records(request)
        if _approving_records_hash(approving) != grant.approving_records_hash:
            raise HumanDecisionError("persisted approval records changed after grant")
        self._require_expected_binding(
            request,
            proposal_id=expected_proposal_id,
            subject_id=expected_subject_id,
            target_id=expected_target_id,
            target_hash=expected_target_hash,
            action_scope=expected_action_scope,
            fact_snapshot_id=expected_fact_snapshot_id,
            fact_snapshot_hash=expected_fact_snapshot_hash,
            policy_version=expected_policy_version,
        )
        capability = self.__issue_capability(grant)
        capability.validate_exact_binding(
            decision_id=request.decision_id,
            proposal_id=expected_proposal_id,
            actor_id=grant.approver_actor_id,
            actor_role=grant.approver_role,
            subject_id=expected_subject_id,
            target_id=expected_target_id,
            target_hash=expected_target_hash,
            action_scope=expected_action_scope,
            fact_snapshot_id=expected_fact_snapshot_id,
            fact_snapshot_hash=expected_fact_snapshot_hash,
            policy_version=expected_policy_version,
            idempotency_key=idempotency_key,
            now=grant.issued_at,
        )
        return capability

    def _event(
        self,
        request: HumanDecisionRequest,
        event_type: str,
        actor_id: str | None,
    ) -> None:
        self._repository.append_event(
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


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HumanDecisionError("human decision clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _monotonic_transition_time(previous: datetime, requested: datetime) -> datetime:
    previous = _aware_utc(previous)
    requested = _aware_utc(requested)
    if requested <= previous:
        return previous + timedelta(microseconds=1)
    return requested


# Capability minting is held only by the authority service class after module
# initialization; there is no module-level raw-grant-to-capability helper.
del _CAPABILITY_ISSUER


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
    "VerifiedApprovalCapability",
]
