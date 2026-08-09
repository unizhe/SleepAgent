"""Trusted adapters from durable worker claims to the PostgreSQL B3 slice.

The historical ``ingestion`` worker queue consumes an already-durable
``normalization_work`` row.  Raw ingress itself remains an explicit seed/API
boundary and ``ReplayIngressHandler`` is intentionally not registered here.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Mapping, Protocol, cast

from sleepagent.backend_keys import BackendKeyProvider
from sleepagent.backend_settings import DataMode, ProcessRole, SleepBackendSettings
from sleepagent.radar_agent.persistence.uow import UnitOfWorkFactory, UowScope
from sleepagent.sleep_domain.postgres_slice import (
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
from sleepagent.worker_runtime import (
    DurableWorkStoreError,
    LeaseClaim,
    WorkContext,
    WorkDisposition,
    WorkFinalizationMode,
    WorkHandler,
    WorkResult,
)


class B3WorkerCompositionError(RuntimeError):
    """The configured runtime cannot safely construct a B3 handler."""


class B3ClaimInvariantError(RuntimeError):
    """A persisted B3 claim does not carry one exact trusted workload scope."""


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
    if "ingestion" in settings.worker_queues:
        if settings.data_mode != DataMode.REPLAY:
            raise B3WorkerCompositionError(
                "the current normalization adapter accepts replay ingress only"
            )
        key = BackendKeyProvider(settings.deployment_mode).encryption_key(
            settings.encryption_key_ref
        )
        cipher = RawPayloadCipher(key, key_id=settings.encryption_key_ref)

        def normalization_processor_factory(
            uow_factory: UnitOfWorkFactory[Any],
        ) -> NormalizationProcessor:
            return cast(
                NormalizationProcessor,
                NormalizationHandler(uow_factory, cipher=cipher),
            )

        handlers["ingestion"] = NormalizationWorkHandlerAdapter(
            processor_factory=normalization_processor_factory
        )
    if "fast_path" in settings.worker_queues:
        handlers["fast_path"] = FastPathWorkHandlerAdapter(
            processor_factory=FastPathHandler
        )
    return handlers


def exact_worker_scope(
    context: WorkContext,
    *,
    allowed_handler: str,
) -> UowScope:
    """Resolve and verify one trusted claim's exact workload scope."""

    claim = context.claim
    try:
        scope = context.store.uow_scope_for_claim(claim)
    except (AttributeError, DurableWorkStoreError, TypeError, ValueError) as exc:
        raise B3ClaimInvariantError("claim cannot form an exact UoW scope") from exc

    exact_claim_fields = {
        "namespace_id": claim.namespace_id,
        "data_mode": claim.data_mode,
        "namespace_generation": claim.namespace_generation,
        "run_id": claim.run_id,
        "arm_id": claim.arm_id,
        "subject_id": claim.subject_id,
        "worker_instance": claim.worker_instance,
    }
    if (
        scope.process_role != "worker"
        or scope.actor_id is not None
        or scope.actor_role is not None
        or any(
            getattr(scope, name) != expected
            for name, expected in exact_claim_fields.items()
        )
    ):
        raise B3ClaimInvariantError("claim and UoW scope differ")

    snapshot = claim.authorization_snapshot
    expected_snapshot: Mapping[str, object] = {
        "schema_version": "workload_authorization_snapshot.v1",
        "workload_principal_id": scope.service_principal_id,
        "namespace_id": scope.namespace_id,
        "namespace_generation": scope.namespace_generation,
        "data_mode": scope.data_mode,
        "run_id": scope.run_id,
        "arm_id": scope.arm_id,
        "subject_id": scope.subject_id,
        "purpose": scope.purpose,
        "allowed_handler": allowed_handler,
        "authorization_epoch": scope.authorization_epoch,
        "privacy_epoch": scope.privacy_epoch,
        "retrieval_policy_epoch": scope.retrieval_policy_epoch,
    }
    if set(expected_snapshot) - set(snapshot) or any(
        type(snapshot[name]) is not type(expected) or snapshot[name] != expected
        for name, expected in expected_snapshot.items()
    ):
        raise B3ClaimInvariantError("workload snapshot and UoW scope differ")
    return scope


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


def worker_uow_factory(context: WorkContext) -> UnitOfWorkFactory[Any]:
    """Return the composition-root UoW factory exposed by the durable store."""

    factory = getattr(context.store, "uow_factory", None)
    if factory is None:
        raise B3WorkerCompositionError(
            "B3 handler store does not expose its UnitOfWorkFactory"
        )
    return factory


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
