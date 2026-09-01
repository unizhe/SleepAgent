"""Application boundaries for care-outcome evaluation and product reads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Protocol

from sleepagent.application.care_execution import (
    CARE_READ_SCOPE,
    CareExecutionPrincipal,
)
from sleepagent.domain.care_execution import CareExecutionError, CarePlanEntry
from sleepagent.domain.care_outcomes import (
    CareOutcome,
    CareOutcomeEvaluationDecision,
    OutcomeLifecycleState,
    PersonalizationEffectReceipt,
)


UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class CareOutcomeView:
    plan: CarePlanEntry
    execution_state: str
    completed_at: datetime | None
    lifecycle_state: OutcomeLifecycleState
    observation_window_start: datetime | None
    observation_window_end: datetime | None
    reason_code: str | None
    outcome: CareOutcome | None
    personalization_receipt: PersonalizationEffectReceipt | None


class CareOutcomeReadRepository(Protocol):
    def get_outcome_view(
        self,
        principal: CareExecutionPrincipal,
        *,
        care_plan_id: str,
        now: datetime,
    ) -> CareOutcomeView | None: ...

    def list_outcome_views(
        self,
        principal: CareExecutionPrincipal,
        *,
        limit: int,
        now: datetime,
    ) -> tuple[CareOutcomeView, ...]: ...


class CareOutcomeReadService:
    def __init__(
        self,
        repository: CareOutcomeReadRepository,
        *,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.now_factory = now_factory or (lambda: datetime.now(tz=UTC))

    def outcome(
        self,
        principal: CareExecutionPrincipal,
        *,
        care_plan_id: str,
    ) -> CareOutcomeView:
        principal.require(CARE_READ_SCOPE)
        plan_id = care_plan_id.strip()
        if not plan_id or len(plan_id) > 200:
            raise CareExecutionError("Care plan identifier is invalid")
        now = self._now()
        view = self.repository.get_outcome_view(
            principal, care_plan_id=plan_id, now=now
        )
        if view is None:
            raise CareExecutionError("Care outcome was not found or is unauthorized")
        if view.plan.subject_id != principal.subject_id:
            raise CareExecutionError("Care outcome subject authority does not match")
        return view

    def outcomes(
        self,
        principal: CareExecutionPrincipal,
        *,
        limit: int = 50,
    ) -> tuple[CareOutcomeView, ...]:
        principal.require(CARE_READ_SCOPE)
        if not 1 <= limit <= 100:
            raise CareExecutionError("Care outcome list limit is outside policy")
        return self.repository.list_outcome_views(
            principal, limit=limit, now=self._now()
        )

    def _now(self) -> datetime:
        value = self.now_factory()
        if value.tzinfo is None or value.utcoffset() is None:
            raise CareExecutionError("Care outcome clock must be timezone-aware")
        return value


class CareOutcomeEvaluationRepository(Protocol):
    def evaluate_operation(
        self,
        *,
        operation_id: str,
        evaluated_at: datetime,
    ) -> CareOutcomeEvaluationDecision: ...


__all__ = [
    "CareOutcomeEvaluationRepository",
    "CareOutcomeReadRepository",
    "CareOutcomeReadService",
    "CareOutcomeView",
]
