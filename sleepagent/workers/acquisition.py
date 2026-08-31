"""Worker-owned execution for durable acquisition schedule operations."""

from __future__ import annotations

import importlib
from typing import Any, Mapping, Protocol

from sleepagent.application.acquisition import (
    AcquisitionJobType,
    AcquisitionScheduleService,
)
from sleepagent.application.night_finalization import (
    NightFinalizationPending,
    NightFinalizationService,
)
from sleepagent.config import (
    DataMode,
    ProcessRole,
    ProviderMode,
    SleepBackendSettings,
)
from sleepagent.persistence.uow import UnitOfWorkFactory
from sleepagent.workers.kernel import (
    RetryableWorkError,
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


SCHEDULED_JOB_QUEUES = tuple(item.value for item in AcquisitionJobType)


class AcquisitionExecutor(Protocol):
    def execute(
        self,
        *,
        context: WorkContext,
        job_type: AcquisitionJobType,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class ScheduledAcquisitionWorkHandler:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
        executor: AcquisitionExecutor,
    ) -> None:
        self.uow_factory = uow_factory
        self.executor = executor
        self.schedules = AcquisitionScheduleService(uow_factory)

    def __call__(self, context: WorkContext) -> WorkResult:
        claim = context.claim
        try:
            job_type = AcquisitionJobType(claim.queue)
        except ValueError as exc:
            raise B3ClaimInvariantError("unknown scheduled acquisition queue") from exc
        if (
            claim.operation_id != claim.work_id
            or claim.metadata.get("work_kind") != "operation"
            or claim.metadata.get("operation_type") != job_type.value
            or claim.payload.get("job_type") != job_type.value
            or not claim.payload.get("schedule_id")
            or not claim.payload.get("fire_id")
        ):
            raise B3ClaimInvariantError("scheduled acquisition claim is malformed")
        scope = exact_worker_scope(context, allowed_handler=job_type.value)
        fire_id = str(claim.payload["fire_id"])
        if self.schedules.fire_succeeded(scope, fire_id=fire_id):
            return WorkResult(
                disposition=WorkDisposition.SUCCEEDED,
                result={"schedule_fire_replayed": True},
            )
        try:
            result = self.executor.execute(
                context=context,
                job_type=job_type,
                payload=claim.payload,
            )
            self.schedules.record_result(
                scope,
                fire_id=fire_id,
                succeeded=True,
            )
        except NightFinalizationPending as exc:
            self.schedules.record_result(
                scope,
                fire_id=fire_id,
                succeeded=False,
                error_code="night_finalization_pending",
            )
            raise RetryableWorkError(
                "night_finalization_pending", retry_after_seconds=300
            ) from exc
        except RetryableWorkError as exc:
            self.schedules.record_result(
                scope,
                fire_id=fire_id,
                succeeded=False,
                error_code=exc.code,
            )
            raise
        except Exception as exc:
            self.schedules.record_result(
                scope,
                fire_id=fire_id,
                succeeded=False,
                error_code="scheduled_acquisition_failed",
            )
            raise RetryableWorkError(
                "scheduled_acquisition_failed", retry_after_seconds=60
            ) from exc
        return WorkResult(
            disposition=WorkDisposition.SUCCEEDED,
            result=dict(result),
        )


def build_acquisition_worker_handlers(
    settings: SleepBackendSettings,
) -> dict[str, WorkHandler]:
    selected = set(settings.worker_queues).intersection(SCHEDULED_JOB_QUEUES)
    if not selected:
        return {}
    if settings.process_role is not ProcessRole.WORKER:
        raise ValueError("acquisition handlers require a worker profile")
    if not settings.acquisition_scheduler_enabled:
        raise ValueError("scheduled acquisition queues require the feature gate")
    pull_queues = {
        AcquisitionJobType.HISTORY_OVERLAP_PULL.value,
        AcquisitionJobType.SLEEP_REPORT_PULL.value,
    }
    if selected.intersection(pull_queues) and (
        settings.data_mode is not DataMode.LIVE
        or settings.provider_mode is not ProviderMode.LIVE
    ):
        raise ValueError("Perceptor scheduled Pull requires live provider mode")

    handler: ScheduledAcquisitionWorkHandler | None = None

    def factory(context: WorkContext) -> WorkResult:
        nonlocal handler
        if handler is None:
            uow_factory = worker_uow_factory(context)
            if selected.intersection(pull_queues):
                module = importlib.import_module(
                    "sleepagent.integrations.perceptor.scheduled"
                )
                executor: AcquisitionExecutor = module.PerceptorAcquisitionExecutor(
                    settings, uow_factory
                )
            else:
                executor = _NightFinalizationExecutor(uow_factory)
            handler = ScheduledAcquisitionWorkHandler(
                uow_factory,
                executor,
            )
        return handler(context)

    return {queue: factory for queue in selected}


class _NightFinalizationExecutor:
    def __init__(self, uow_factory: UnitOfWorkFactory[Any]) -> None:
        self.service = NightFinalizationService(uow_factory)

    def execute(
        self,
        *,
        context: WorkContext,
        job_type: AcquisitionJobType,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if job_type is not AcquisitionJobType.NIGHT_FINALIZATION_SCAN:
            raise B3ClaimInvariantError("provider executor is unavailable")
        scope = exact_worker_scope(context, allowed_handler=job_type.value)
        from datetime import datetime

        result = self.service.finalize_latest_for_binding(
            scope,
            device_binding_id=str(payload["device_binding_id"]),
            evaluated_at=datetime.fromisoformat(str(payload["scheduled_for"])),
        )
        return result.model_dump(mode="json")


__all__ = [
    "AcquisitionExecutor",
    "SCHEDULED_JOB_QUEUES",
    "ScheduledAcquisitionWorkHandler",
    "build_acquisition_worker_handlers",
]
