"""Durable worker boundary for care outcome evaluation."""

from __future__ import annotations

from datetime import datetime, timezone

from sleepagent.config import ProcessRole, SleepBackendSettings
from sleepagent.domain.care_outcomes import CARE_OUTCOME_QUEUE
from sleepagent.infrastructure.postgres_care_outcomes import (
    PostgresCareOutcomeEvaluator,
)
from sleepagent.workers.kernel import (
    WorkContext,
    WorkDisposition,
    WorkHandler,
    WorkResult,
)
from sleepagent.workers.runtime import (
    B3ClaimInvariantError,
    exact_worker_scope,
    worker_uow_factory,
)


class CareOutcomeWorkHandler:
    def __call__(self, context: WorkContext) -> WorkResult:
        claim = context.claim
        if (
            claim.queue != CARE_OUTCOME_QUEUE
            or claim.operation_id != claim.work_id
            or claim.metadata.get("work_kind") != "operation"
            or claim.metadata.get("operation_type") != CARE_OUTCOME_QUEUE
            or claim.payload.get("schema_version")
            != "care_outcome_evaluation_operation.v1"
            or not claim.payload.get("evaluation_registration_id")
        ):
            raise B3ClaimInvariantError("care outcome claim is malformed")
        scope = exact_worker_scope(context, allowed_handler=CARE_OUTCOME_QUEUE)
        evaluator = PostgresCareOutcomeEvaluator(
            worker_uow_factory(context),
            scope,
            lease_generation=claim.lease_generation,
            fencing_token=claim.fencing_token,
        )
        decision = evaluator.evaluate_operation(
            operation_id=claim.operation_id,
            evaluated_at=datetime.now(tz=timezone.utc),
        )
        return WorkResult(
            disposition=WorkDisposition.SUCCEEDED,
            result={
                "lifecycle_state": decision.lifecycle_state.value,
                "reason_code": decision.reason_code,
                "care_outcome_id": (
                    None
                    if decision.outcome is None
                    else decision.outcome.care_outcome_id
                ),
            },
        )


def build_care_outcome_worker_handlers(
    settings: SleepBackendSettings,
) -> dict[str, WorkHandler]:
    if CARE_OUTCOME_QUEUE not in settings.worker_queues:
        return {}
    if settings.process_role is not ProcessRole.WORKER:
        raise ValueError("care outcome handler requires a worker profile")
    return {CARE_OUTCOME_QUEUE: CareOutcomeWorkHandler()}


__all__ = ["CareOutcomeWorkHandler", "build_care_outcome_worker_handlers"]
