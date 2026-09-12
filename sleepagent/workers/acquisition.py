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
from sleepagent.observability import log_event
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
            pending_reason = {
                "binding has no finalizable night": "binding_has_no_finalizable_night",
                "night has not reached a deterministic finalization gate": (
                    "deterministic_gate_not_reached"
                ),
                "night episode has no committed date/revision authority": (
                    "episode_authority_incomplete"
                ),
            }.get(str(exc), "unclassified")
            log_event(
                "night_finalization_pending",
                reason=pending_reason,
                queue=job_type.value,
            )
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
            error_code = _scheduled_acquisition_error_code(exc)
            self.schedules.record_result(
                scope,
                fire_id=fire_id,
                succeeded=False,
                error_code=error_code,
            )
            raise RetryableWorkError(
                error_code, retry_after_seconds=60
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

    def handler_factory(queue: str) -> WorkHandler:
        handler: ScheduledAcquisitionWorkHandler | None = None

        def factory(context: WorkContext) -> WorkResult:
            nonlocal handler
            if handler is None:
                uow_factory = worker_uow_factory(context)
                if queue in pull_queues:
                    module = importlib.import_module(
                        "sleepagent.integrations.perceptor.scheduled"
                    )
                    executor: AcquisitionExecutor = (
                        module.PerceptorAcquisitionExecutor(settings, uow_factory)
                    )
                else:
                    executor = _NightFinalizationExecutor(uow_factory)
                handler = ScheduledAcquisitionWorkHandler(
                    uow_factory,
                    executor,
                )
            return handler(context)

        return factory

    return {queue: handler_factory(queue) for queue in selected}


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
        from datetime import datetime, timezone

        evaluated_at = datetime.now(tz=timezone.utc)
        results = self.service.finalize_due_for_binding(
            scope,
            device_binding_id=str(payload["device_binding_id"]),
            evaluated_at=evaluated_at,
        )
        return {
            "schema_version": "night_finalization_scan_result.v1",
            "scheduled_for": datetime.fromisoformat(
                str(payload["scheduled_for"])
            ).isoformat(),
            "evaluated_at": evaluated_at.isoformat(),
            "processed_count": len(results),
            "night_episode_ids": [item.night_episode_id for item in results],
            "finalization_revision_ids": [
                item.night_finalization_revision_id for item in results
            ],
        }


def _scheduled_acquisition_error_code(exc: Exception) -> str:
    current: BaseException | None = exc
    while current is not None:
        if type(current).__name__ == "PerceptorHistoryBackfillRequired":
            return "historical_backfill_required"
        category = getattr(current, "category", None)
        if isinstance(category, str) and category:
            parts = ["platform_api", category.lower()]
            status = getattr(current, "http_status", None)
            if isinstance(status, int):
                parts.append(f"http_{status}")
            vendor_code = getattr(current, "vendor_code", None)
            if isinstance(vendor_code, str) and vendor_code.isascii():
                safe_code = "".join(
                    character
                    for character in vendor_code.lower()
                    if character.isalnum() or character in {"_", "-"}
                )[:24]
                if safe_code:
                    parts.append(f"vendor_{safe_code}")
            return "_".join(parts)[:120]
        current = current.__cause__
    return "scheduled_acquisition_failed"


__all__ = [
    "AcquisitionExecutor",
    "SCHEDULED_JOB_QUEUES",
    "ScheduledAcquisitionWorkHandler",
    "build_acquisition_worker_handlers",
]
