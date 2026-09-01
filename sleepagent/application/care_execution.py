"""Application boundary for terminal care plans and execution tracking."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from collections.abc import Callable
from typing import Protocol

from sleepagent.domain.care_actions import CareAudience
from sleepagent.domain.care_execution import (
    CARE_EXECUTION_SCOPE,
    CareExecutionError,
    CareExecutionEvent,
    CareExecutionEventType,
    CareExecutionState,
    CareExecutionStatus,
    CarePlanEntry,
)


UTC = timezone.utc
CARE_READ_SCOPE = "product:sleep:care:read"


class CarePlanFilter(str, Enum):
    ACTIVE = "active"
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


@dataclass(frozen=True, slots=True)
class CareExecutionPrincipal:
    service_principal_id: str
    actor_id: str
    actor_role: CareAudience
    actor_binding_id: str
    subject_id: str
    effective_scopes: frozenset[str]
    authorization_epoch: int
    privacy_epoch: int
    retrieval_policy_epoch: int
    namespace_id: str
    namespace_generation: int
    data_mode: str
    run_id: str | None
    arm_id: str | None

    def require(self, scope: str) -> None:
        if scope not in self.effective_scopes:
            raise CareExecutionError("Human execution authority is missing scope")


@dataclass(frozen=True, slots=True)
class CarePlanView:
    plan: CarePlanEntry
    execution: CareExecutionStatus
    approval_grant_state: str
    executable: bool


@dataclass(frozen=True, slots=True)
class CareExecutionCommandResult:
    outcome: str
    view: CarePlanView
    event: CareExecutionEvent | None


class CarePlanRepository(Protocol):
    def list_plans(
        self,
        principal: CareExecutionPrincipal,
        *,
        state: CarePlanFilter | None,
        limit: int,
        now: datetime,
    ) -> tuple[CarePlanView, ...]: ...

    def get_plan(
        self,
        principal: CareExecutionPrincipal,
        *,
        care_plan_id: str,
        now: datetime,
    ) -> CarePlanView | None: ...

    def history(
        self,
        principal: CareExecutionPrincipal,
        *,
        care_plan_id: str,
        now: datetime,
    ) -> tuple[CareExecutionEvent, ...]: ...

    def execute(
        self,
        principal: CareExecutionPrincipal,
        *,
        care_plan_id: str,
        event_type: CareExecutionEventType,
        expected_version: int,
        idempotency_key: str,
        note: str | None,
        occurred_at: datetime,
    ) -> CareExecutionCommandResult: ...


class CarePlanApplicationService:
    def __init__(
        self,
        repository: CarePlanRepository,
        *,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.now_factory = now_factory or (lambda: datetime.now(tz=UTC))

    def list(
        self,
        principal: CareExecutionPrincipal,
        *,
        state: CarePlanFilter | None = None,
        limit: int = 50,
    ) -> tuple[CarePlanView, ...]:
        principal.require(CARE_READ_SCOPE)
        if not 1 <= limit <= 100:
            raise CareExecutionError("Care plan list limit is outside policy")
        return self.repository.list_plans(
            principal,
            state=state,
            limit=limit,
            now=_aware(self.now_factory()),
        )

    def show(
        self,
        principal: CareExecutionPrincipal,
        *,
        care_plan_id: str,
    ) -> CarePlanView:
        principal.require(CARE_READ_SCOPE)
        view = self.repository.get_plan(
            principal,
            care_plan_id=_identifier(care_plan_id, "care plan"),
            now=_aware(self.now_factory()),
        )
        if view is None:
            raise CareExecutionError("Care plan was not found or is unauthorized")
        self._validate_view_authority(principal, view)
        return view

    def history(
        self,
        principal: CareExecutionPrincipal,
        *,
        care_plan_id: str,
    ) -> tuple[CareExecutionEvent, ...]:
        self.show(principal, care_plan_id=care_plan_id)
        return self.repository.history(
            principal,
            care_plan_id=care_plan_id,
            now=_aware(self.now_factory()),
        )

    def start(
        self,
        principal: CareExecutionPrincipal,
        *,
        care_plan_id: str,
        idempotency_key: str,
        note: str | None = None,
        occurred_at: datetime | None = None,
    ) -> CareExecutionCommandResult:
        return self._execute(
            principal,
            care_plan_id=care_plan_id,
            event_type=CareExecutionEventType.STARTED,
            idempotency_key=idempotency_key,
            note=note,
            occurred_at=occurred_at,
        )

    def complete(
        self,
        principal: CareExecutionPrincipal,
        *,
        care_plan_id: str,
        idempotency_key: str,
        note: str | None = None,
        occurred_at: datetime | None = None,
    ) -> CareExecutionCommandResult:
        return self._execute(
            principal,
            care_plan_id=care_plan_id,
            event_type=CareExecutionEventType.COMPLETED,
            idempotency_key=idempotency_key,
            note=note,
            occurred_at=occurred_at,
        )

    def cancel(
        self,
        principal: CareExecutionPrincipal,
        *,
        care_plan_id: str,
        idempotency_key: str,
        note: str | None = None,
        occurred_at: datetime | None = None,
    ) -> CareExecutionCommandResult:
        return self._execute(
            principal,
            care_plan_id=care_plan_id,
            event_type=CareExecutionEventType.CANCELLED,
            idempotency_key=idempotency_key,
            note=note,
            occurred_at=occurred_at,
        )

    def _execute(
        self,
        principal: CareExecutionPrincipal,
        *,
        care_plan_id: str,
        event_type: CareExecutionEventType,
        idempotency_key: str,
        note: str | None,
        occurred_at: datetime | None,
    ) -> CareExecutionCommandResult:
        principal.require(CARE_EXECUTION_SCOPE)
        plan_id = _identifier(care_plan_id, "care plan")
        command_key = _bounded_text(idempotency_key, "idempotency key", 200)
        normalized_note = sanitize_human_note(note)
        view = self.repository.get_plan(
            principal,
            care_plan_id=plan_id,
            now=_aware(self.now_factory()),
        )
        if view is None:
            raise CareExecutionError("Care plan was not found or is unauthorized")
        self._validate_view_authority(principal, view)
        at = _aware(occurred_at or self.now_factory())
        result = self.repository.execute(
            principal,
            care_plan_id=plan_id,
            event_type=event_type,
            expected_version=view.execution.version,
            idempotency_key=command_key,
            note=normalized_note,
            occurred_at=at,
        )
        self._validate_view_authority(principal, result.view)
        return result

    @staticmethod
    def _validate_view_authority(
        principal: CareExecutionPrincipal,
        view: CarePlanView,
    ) -> None:
        if view.plan.subject_id != principal.subject_id:
            raise CareExecutionError("Care plan subject authority does not match")
        if view.plan.executor_role is not principal.actor_role:
            raise CareExecutionError("Care plan executor role does not match")


def sanitize_human_note(note: str | None) -> str | None:
    if note is None:
        return None
    normalized = " ".join(note.split())
    if not normalized:
        return None
    if len(normalized) > 500:
        raise CareExecutionError("Human execution note is too long")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", normalized):
        raise CareExecutionError("Human execution note contains control characters")
    return normalized


def _bounded_text(value: str, label: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise CareExecutionError(f"{label} is invalid")
    return normalized


def _identifier(value: str, label: str) -> str:
    return _bounded_text(value, label, 200)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CareExecutionError("Care execution timestamp must include an offset")
    return value


__all__ = [
    "CARE_READ_SCOPE",
    "CareExecutionCommandResult",
    "CareExecutionPrincipal",
    "CarePlanApplicationService",
    "CarePlanFilter",
    "CarePlanRepository",
    "CarePlanView",
    "sanitize_human_note",
]
