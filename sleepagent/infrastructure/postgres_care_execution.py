"""PostgreSQL adapter for the terminal care-plan application boundary."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast

from sleepagent.application.care_execution import (
    CareExecutionCommandResult,
    CareExecutionPrincipal,
    CarePlanFilter,
    CarePlanRepository,
    CarePlanView,
)
from sleepagent.domain.care_execution import (
    CareExecutionError,
    CareExecutionEvent,
    CareExecutionEventType,
    CareExecutionState,
    CareExecutionStatus,
    CarePlanEntry,
)
from sleepagent.observability import log_event
from sleepagent.persistence.uow import UnitOfWorkFactory, UowScope


_VIEW_SELECT = """
SELECT plan.plan_json, execution.state, execution.version,
  execution.started_at, execution.completed_at, execution.cancelled_at,
  execution.invalidated_at, execution.invalidation_reason,
  execution.updated_at, execution.command_conflict_count,
  grant_row.state, proposal.state, plan.valid_until, proposal.expires_at
FROM public.backend_care_plans_v1 AS plan
JOIN public.backend_care_execution_states_v1 AS execution
  ON execution.care_plan_id = plan.care_plan_id
JOIN public.backend_approval_grants_v3 AS grant_row
  ON grant_row.grant_id = plan.approval_grant_id
JOIN public.backend_care_action_proposals_v3 AS proposal
  ON proposal.proposal_id = plan.proposal_id
"""


class PostgresCarePlanRepository(CarePlanRepository):
    def __init__(self, uow_factory: UnitOfWorkFactory[Any]) -> None:
        self.uow_factory = uow_factory

    def list_plans(
        self,
        principal: CareExecutionPrincipal,
        *,
        state: CarePlanFilter | None,
        limit: int,
        now: datetime,
    ) -> tuple[CarePlanView, ...]:
        with self.uow_factory.begin(_scope(principal)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    _VIEW_SELECT
                    + """
                    WHERE plan.namespace_id = %s AND plan.data_mode = %s
                      AND plan.namespace_generation = %s
                      AND plan.run_id IS NOT DISTINCT FROM %s
                      AND plan.arm_id IS NOT DISTINCT FROM %s
                      AND plan.subject_id = %s
                    ORDER BY plan.created_at DESC, plan.care_plan_id DESC
                    LIMIT 500
                    """,
                    (
                        principal.namespace_id,
                        principal.data_mode,
                        principal.namespace_generation,
                        principal.run_id,
                        principal.arm_id,
                        principal.subject_id,
                    ),
                )
                rows = cursor.fetchall()
            finally:
                cursor.close()
        views = tuple(_view(row, now=now) for row in rows)
        if state is not None:
            views = tuple(item for item in views if _matches_filter(item, state))
        return views[:limit]

    def get_plan(
        self,
        principal: CareExecutionPrincipal,
        *,
        care_plan_id: str,
        now: datetime,
    ) -> CarePlanView | None:
        with self.uow_factory.begin(_scope(principal)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    _VIEW_SELECT
                    + """
                    WHERE plan.care_plan_id = %s
                      AND plan.namespace_id = %s AND plan.data_mode = %s
                      AND plan.namespace_generation = %s
                      AND plan.run_id IS NOT DISTINCT FROM %s
                      AND plan.arm_id IS NOT DISTINCT FROM %s
                      AND plan.subject_id = %s
                    """,
                    (
                        care_plan_id,
                        principal.namespace_id,
                        principal.data_mode,
                        principal.namespace_generation,
                        principal.run_id,
                        principal.arm_id,
                        principal.subject_id,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        return None if row is None else _view(row, now=now)

    def history(
        self,
        principal: CareExecutionPrincipal,
        *,
        care_plan_id: str,
        now: datetime,
    ) -> tuple[CareExecutionEvent, ...]:
        del now
        with self.uow_factory.begin(_scope(principal)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT event.event_json
                    FROM public.backend_care_execution_events_v1 AS event
                    WHERE event.care_plan_id = %s
                      AND event.namespace_id = %s AND event.data_mode = %s
                      AND event.namespace_generation = %s
                      AND event.run_id IS NOT DISTINCT FROM %s
                      AND event.arm_id IS NOT DISTINCT FROM %s
                      AND event.subject_id = %s
                    ORDER BY event.occurred_at, event.recorded_at, event.event_id
                    """,
                    (
                        care_plan_id,
                        principal.namespace_id,
                        principal.data_mode,
                        principal.namespace_generation,
                        principal.run_id,
                        principal.arm_id,
                        principal.subject_id,
                    ),
                )
                rows = cursor.fetchall()
            finally:
                cursor.close()
        return tuple(
            CareExecutionEvent.model_validate(_json_object(row[0])) for row in rows
        )

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
    ) -> CareExecutionCommandResult:
        try:
            with self.uow_factory.begin(_scope(principal)) as uow:
                cursor = uow.connection.cursor()
                try:
                    cursor.execute(
                        "SELECT public.sleepagent_execute_care_plan_v1("
                        "%s,%s,%s,%s,%s,%s,%s)",
                        (
                            care_plan_id,
                            expected_version,
                            event_type.value,
                            idempotency_key,
                            principal.actor_binding_id,
                            note,
                            occurred_at,
                        ),
                    )
                    row = cursor.fetchone()
                finally:
                    cursor.close()
                uow.commit()
        except Exception as exc:
            code = str(exc).splitlines()[0][:100]
            log_event("care_execution_command_rejected", reason_code=code)
            raise CareExecutionError(
                "Care execution command was denied by durable authority"
            ) from exc
        if row is None:
            raise CareExecutionError("Care execution command returned no result")
        payload = _json_object(row[0])
        outcome = str(payload.get("outcome", ""))
        event_id = payload.get("event_id")
        view = self.get_plan(principal, care_plan_id=care_plan_id, now=occurred_at)
        if view is None:
            raise CareExecutionError("Care plan disappeared after command commit")
        event = None
        if event_id is not None:
            event = next(
                (
                    item
                    for item in self.history(
                        principal, care_plan_id=care_plan_id, now=occurred_at
                    )
                    if item.event_id == str(event_id)
                ),
                None,
            )
        if outcome == "idempotent":
            log_event("care_execution_command_deduplicated")
        elif outcome == "conflict":
            log_event("care_execution_command_conflict")
        elif outcome == "expired":
            log_event("care_plan_expired")
        elif outcome in {"invalidated", "superseded"}:
            log_event("care_plan_invalidated", reason_code=outcome)
        elif outcome == "applied":
            log_event(
                {
                    CareExecutionEventType.STARTED: "care_execution_started",
                    CareExecutionEventType.COMPLETED: "care_execution_completed",
                    CareExecutionEventType.CANCELLED: "care_execution_cancelled",
                }[event_type]
            )
        return CareExecutionCommandResult(
            outcome=outcome,
            view=view,
            event=event,
        )


def _scope(principal: CareExecutionPrincipal) -> UowScope:
    return UowScope(
        namespace_id=principal.namespace_id,
        data_mode=cast(Any, principal.data_mode),
        process_role="api",
        purpose="sleep_care",
        service_principal_id=principal.service_principal_id,
        namespace_generation=principal.namespace_generation,
        subject_id=principal.subject_id,
        actor_id=principal.actor_id,
        actor_role=principal.actor_role.value,
        run_id=principal.run_id,
        arm_id=principal.arm_id,
        authorization_epoch=principal.authorization_epoch,
        privacy_epoch=principal.privacy_epoch,
        retrieval_policy_epoch=principal.retrieval_policy_epoch,
    )


def _view(row: Any, *, now: datetime) -> CarePlanView:
    plan = CarePlanEntry.model_validate(_json_object(row[0]))
    stored = CareExecutionState(str(row[1]))
    grant_state = str(row[10])
    proposal_state = str(row[11])
    effective = stored
    invalidated_at = row[6]
    invalidation_reason = None if row[7] is None else str(row[7])
    if stored in {CareExecutionState.NOT_STARTED, CareExecutionState.IN_PROGRESS}:
        if proposal_state != "approved":
            effective = (
                CareExecutionState.SUPERSEDED
                if proposal_state == "expired" and row[13] > now
                else CareExecutionState.INVALIDATED
            )
            invalidated_at = now
            invalidation_reason = (
                "source_analysis_superseded"
                if effective is CareExecutionState.SUPERSEDED
                else "proposal_authority_invalidated"
            )
        elif grant_state != "active":
            effective = (
                CareExecutionState.EXPIRED
                if grant_state == "expired"
                else CareExecutionState.INVALIDATED
            )
            invalidated_at = now
            invalidation_reason = f"approval_grant_{grant_state}"
        elif row[12] <= now:
            effective = CareExecutionState.EXPIRED
            invalidated_at = row[12]
            invalidation_reason = "execution_window_expired"
    execution = CareExecutionStatus(
        care_plan_id=plan.care_plan_id,
        state=effective,
        version=int(row[2]),
        started_at=row[3],
        completed_at=row[4],
        cancelled_at=row[5],
        invalidated_at=invalidated_at,
        invalidation_reason=invalidation_reason,
        updated_at=row[8],
        command_conflict_count=int(row[9]),
    )
    executable = (
        effective in {
            CareExecutionState.NOT_STARTED,
            CareExecutionState.IN_PROGRESS,
        }
        and grant_state == "active"
        and proposal_state == "approved"
        and plan.valid_from <= now < plan.valid_until
    )
    return CarePlanView(
        plan=plan,
        execution=execution,
        approval_grant_state=grant_state,
        executable=executable,
    )


def _matches_filter(view: CarePlanView, requested: CarePlanFilter) -> bool:
    if requested is CarePlanFilter.ACTIVE:
        return view.executable
    if requested is CarePlanFilter.INVALIDATED:
        return view.execution.state in {
            CareExecutionState.INVALIDATED,
            CareExecutionState.SUPERSEDED,
        }
    return view.execution.state.value == requested.value


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise CareExecutionError("Care execution persistence payload is invalid")
    return value


__all__ = ["PostgresCarePlanRepository"]
