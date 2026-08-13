# 本模块负责持久化队列的一项 Worker 职责，并以租约和围栏保护提交。
"""Trusted adapters from durable worker claims to the PostgreSQL B3 slice.

The historical ``ingestion`` worker queue consumes an already-durable
``normalization_work`` row.  Raw ingress itself remains an explicit seed/API
boundary and ``ReplayIngressHandler`` is intentionally not registered here.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from typing import Any, Callable, Mapping, Protocol, cast

from sleepagent.config import BackendKeyProvider
from sleepagent.config import DataMode, ProcessRole, SleepBackendSettings
from sleepagent.persistence.uow import UnitOfWorkFactory, UowScope
from sleepagent.workers.retention import (
    LocalTestRetentionKeyEnvelope,
    PostgresRetentionKeyCoordinator,
)
from sleepagent.domain.postgres_slice import (
    FastPathCommitResult,
    FastPathHandler,
    FastPathLease,
    NormalizationHandler,
    NormalizationLease,
    NormalizationResult,
    RawPayloadCipher,
    SleepSliceConflict,
    SleepSliceInvariantError,
    SleepSliceLeaseLost,
    SleepSliceStaleRevision,
)
from sleepagent.workers.runtime import (
    B3ClaimInvariantError,
    B3WorkerCompositionError,
    DurableWorkStoreError,
    LeaseClaim,
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
    WorkHandler,
    WorkResult,
    exact_worker_scope,
    worker_uow_factory,
)


class NormalizationProcessor(Protocol):
    def process(
        self,
        scope: UowScope,
        lease: NormalizationLease,
    ) -> NormalizationResult: ...


class FastPathProcessor(Protocol):
    def process(
        self,
        scope: UowScope,
        lease: FastPathLease,
    ) -> FastPathCommitResult: ...


class NormalizationWorkHandlerAdapter:
    """Run normalization and trust its atomic work-row handoff on success."""

    def __init__(
        self,
        *,
        processor: NormalizationProcessor | None = None,
        processor_factory: Callable[[UnitOfWorkFactory[Any]], NormalizationProcessor]
        | None = None,
    ) -> None:
        if (processor is None) == (processor_factory is None):
            raise ValueError("provide exactly one normalization processor source")
        self._processor = processor
        self._processor_factory = processor_factory

    def __call__(self, context: WorkContext) -> WorkResult:
        try:
            _require_normalization_claim(context.claim)
            scope = exact_worker_scope(
                context,
                allowed_handler="normalization",
            )
        except B3ClaimInvariantError:
            return _terminal("invalid_normalization_claim_scope")

        try:
            result = self._resolve_processor(context).process(
                scope,
                NormalizationLease(
                    work_id=context.claim.work_id,
                    lease_generation=context.claim.lease_generation,
                    fencing_token=context.claim.fencing_token,
                    worker_instance=context.claim.worker_instance,
                ),
            )
        except SleepSliceStaleRevision:
            return _terminal("sleep_slice_stale_revision")
        except SleepSliceLeaseLost:
            context.mark_lease_lost()
            return _reconciliation("sleep_slice_lease_lost")
        except SleepSliceInvariantError:
            return _terminal("sleep_slice_invariant_violation")
        except SleepSliceConflict:
            return _retryable("sleep_slice_conflict")

        return WorkResult(
            disposition=WorkDisposition.SUCCEEDED,
            result=asdict(result),
            finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
        )

    def _resolve_processor(self, context: WorkContext) -> NormalizationProcessor:
        if self._processor is not None:
            return self._processor
        assert self._processor_factory is not None
        return self._processor_factory(worker_uow_factory(context))


class FastPathWorkHandlerAdapter:
    """Run deterministic fast path and trust its atomic Operation handoff."""

    def __init__(
        self,
        *,
        processor: FastPathProcessor | None = None,
        processor_factory: Callable[[UnitOfWorkFactory[Any]], FastPathProcessor]
        | None = None,
    ) -> None:
        if (processor is None) == (processor_factory is None):
            raise ValueError("provide exactly one fast-path processor source")
        self._processor = processor
        self._processor_factory = processor_factory

    def __call__(self, context: WorkContext) -> WorkResult:
        try:
            operation_id = _require_fast_path_claim(context.claim)
            scope = exact_worker_scope(context, allowed_handler="fast_path")
        except B3ClaimInvariantError:
            return _terminal("invalid_fast_path_claim_scope")

        try:
            result = self._resolve_processor(context).process(
                scope,
                FastPathLease(
                    operation_id=operation_id,
                    lease_generation=context.claim.lease_generation,
                    fencing_token=context.claim.fencing_token,
                    worker_instance=context.claim.worker_instance,
                ),
            )
        except SleepSliceStaleRevision:
            return _terminal("sleep_slice_stale_revision")
        except SleepSliceLeaseLost:
            context.mark_lease_lost()
            return _reconciliation("sleep_slice_lease_lost")
        except SleepSliceInvariantError:
            return _terminal("sleep_slice_invariant_violation")
        except SleepSliceConflict:
            return _retryable("sleep_slice_conflict")

        return WorkResult(
            disposition=WorkDisposition.SUCCEEDED,
            result=asdict(result),
            finalization_mode=WorkFinalizationMode.HANDLER_OWNED,
        )

    def _resolve_processor(self, context: WorkContext) -> FastPathProcessor:
        if self._processor is not None:
            return self._processor
        assert self._processor_factory is not None
        return self._processor_factory(worker_uow_factory(context))


def build_b3_worker_handlers(
    settings: SleepBackendSettings,
) -> dict[str, WorkHandler]:
    """Build only the B3 handlers that are present in the configured queues."""

    if settings.process_role != ProcessRole.WORKER:
        raise B3WorkerCompositionError("B3 handlers require a worker profile")

    handlers: dict[str, WorkHandler] = {}
    cipher: RawPayloadCipher | None = None
    retention_keys: PostgresRetentionKeyCoordinator | None = None
    if {"ingestion", "replay_journey"}.intersection(settings.worker_queues):
        if settings.data_mode != DataMode.REPLAY:
            raise B3WorkerCompositionError(
                "the current normalization adapter accepts replay ingress only"
            )
        key = BackendKeyProvider(settings.deployment_mode).encryption_key(
            settings.encryption_key_ref
        )
        cipher = RawPayloadCipher(key, key_id=settings.encryption_key_ref)
        retention_keys = PostgresRetentionKeyCoordinator(
            LocalTestRetentionKeyEnvelope(
                key,
                key_id=settings.encryption_key_ref,
            )
        )

    if "replay_journey" in settings.worker_queues:
        assert cipher is not None
        from sleepagent.simulation.journey import ReplayJourneyWorkHandler

        journey_handler: ReplayJourneyWorkHandler | None = None

        def journey_handler_factory(context: WorkContext) -> WorkResult:
            nonlocal journey_handler
            if journey_handler is None:
                journey_handler = ReplayJourneyWorkHandler(
                    uow_factory=worker_uow_factory(context),
                    cipher=cipher,
                    retention_keys=retention_keys,
                    retention=timedelta(
                        seconds=settings.raw_retention_seconds
                    ),
                )
            return journey_handler(context)

        handlers["replay_journey"] = journey_handler_factory

    if "ingestion" in settings.worker_queues:
        assert cipher is not None

        def normalization_processor_factory(
            uow_factory: UnitOfWorkFactory[Any],
        ) -> NormalizationProcessor:
            return cast(
                NormalizationProcessor,
                NormalizationHandler(
                    uow_factory,
                    cipher=cipher,
                    retention_keys=retention_keys,
                ),
            )

        handlers["ingestion"] = NormalizationWorkHandlerAdapter(
            processor_factory=normalization_processor_factory
        )
    if "fast_path" in settings.worker_queues:
        handlers["fast_path"] = FastPathWorkHandlerAdapter(
            processor_factory=FastPathHandler
        )
    return handlers


def _require_normalization_claim(claim: LeaseClaim) -> None:
    if (
        claim.queue != "ingestion"
        or claim.operation_id is not None
        or claim.metadata.get("work_kind") != "normalization"
    ):
        raise B3ClaimInvariantError("claim is not normalization work")


def _require_fast_path_claim(claim: LeaseClaim) -> str:
    if (
        claim.queue != "fast_path"
        or claim.operation_id is None
        or claim.operation_id != claim.work_id
        or claim.metadata.get("work_kind") != "operation"
        or claim.metadata.get("operation_type") != "fast_path"
        or claim.metadata.get("queue_name") != "fast_path"
    ):
        raise B3ClaimInvariantError("claim is not a fast-path operation")
    return claim.operation_id


def _terminal(error_code: str) -> WorkResult:
    return WorkResult(
        disposition=WorkDisposition.TERMINAL,
        error_code=error_code,
    )


def _retryable(error_code: str) -> WorkResult:
    return WorkResult(
        disposition=WorkDisposition.RETRYABLE,
        error_code=error_code,
    )


def _reconciliation(error_code: str) -> WorkResult:
    return WorkResult(
        disposition=WorkDisposition.OUTCOME_UNKNOWN,
        error_code=f"{error_code}_reconciliation_required",
    )


__all__ = [
    "B3ClaimInvariantError",
    "B3WorkerCompositionError",
    "FastPathWorkHandlerAdapter",
    "NormalizationWorkHandlerAdapter",
    "build_b3_worker_handlers",
    "exact_worker_scope",
    "worker_uow_factory",
]
