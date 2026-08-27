# 本模块负责持久化队列的一项 Worker 职责，并以租约和围栏保护提交。
"""Durable, fenced worker runtime shared by all backend queues."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Literal, Mapping, Protocol, Self, Sequence, cast
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from sleepagent.process import SleepBackendRuntime, build_backend_runtime
from sleepagent.config import (
    DataMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.observability import (
    log_event,
    record_backend_queue,
    record_backend_signal,
)
from sleepagent.persistence.uow import (
    UnitOfWorkFactory,
    UowScope,
    WorkerClaimScope,
)
from sleepagent.persistence.migrations import (
    COMMAND_AUTHORITY_FUNCTION_SIGNATURE,
    DELIVERY_AUTHORITY_FUNCTION_SIGNATURE,
    SCENARIO_CLOCK_AUTHORITY_FUNCTION_SIGNATURE,
)
from sleepagent.domain.episodes import UUID7Generator


UTC = timezone.utc
DEFAULT_QUEUE_ORDER = (
    "ingestion",
    "fast_path",
    "product_agent",
    "sleep_command",
    "product_interaction",
    "demo_advance",
    # Root journeys poll durable child state.  They must run after every
    # Stage-1 leaf queue so one due root cannot repeatedly starve its children.
    "replay_journey",
    "induction",
    "delivery",
    "retention",
    "demo_reset",
    "reconciliation",
)


class WorkDisposition(str, Enum):
    SUCCEEDED = "succeeded"
    RETRYABLE = "retryable"
    TERMINAL = "terminal"
    OUTCOME_UNKNOWN = "outcome_unknown"


class WorkFinalizationMode(str, Enum):
    """Identify which trusted component atomically finalized the claim row."""

    WORKER_OWNED = "worker_owned"
    HANDLER_OWNED = "handler_owned"


class InvocationState(str, Enum):
    RESERVED = "reserved"
    SEND_STARTED = "send_started"
    OUTCOME_POSSIBLE = "outcome_possible"
    RESPONSE_RECEIVED = "response_received"
    KNOWN_FAILED = "known_failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RECONCILED = "reconciled"


class InvocationKind(str, Enum):
    MODEL = "model"
    PROVIDER = "provider"
    EXTERNAL_SINK = "external_sink"


class WorkKind(str, Enum):
    JOURNEY = "journey"
    NORMALIZATION = "normalization"
    OPERATION = "operation"
    DELIVERY = "delivery"
    RETENTION = "retention"


class LeaseClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_id: str = Field(min_length=1)
    operation_id: str | None = None
    queue: str = Field(min_length=1)
    namespace_id: str = Field(min_length=1)
    data_mode: str = Field(pattern="^(live|replay)$")
    namespace_generation: int = Field(ge=1)
    run_id: str | None = None
    arm_id: str | None = None
    subject_id: str | None = None
    operation_version: int = Field(ge=0)
    lease_generation: int = Field(ge=1)
    fencing_token: str = Field(min_length=32)
    worker_instance: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    lease_deadline: datetime
    payload: dict[str, Any]
    authorization_snapshot: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("lease_deadline")
    @classmethod
    def deadline_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("lease_deadline must include a timezone offset")
        return value

    @model_validator(mode="after")
    def scope_is_exact(self) -> Self:
        if self.data_mode == "live" and (self.run_id or self.arm_id):
            raise ValueError("live work cannot carry replay run/arm identifiers")
        if self.data_mode == "replay" and not (self.run_id and self.arm_id):
            raise ValueError("replay work requires run_id and arm_id")
        return self


class WorkResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    disposition: WorkDisposition
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0, le=86_400)
    finalization_mode: WorkFinalizationMode = WorkFinalizationMode.WORKER_OWNED

    @model_validator(mode="after")
    def handler_owned_result_is_committed_success(self) -> Self:
        if (
            self.finalization_mode == WorkFinalizationMode.HANDLER_OWNED
            and self.disposition
            not in {WorkDisposition.SUCCEEDED, WorkDisposition.RETRYABLE}
        ):
            raise ValueError(
                "handler-owned finalization requires a committed successful "
                "result or committed retry wait"
            )
        return self


class InvocationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: str
    invocation_key: str
    invocation_kind: InvocationKind
    work_id: str
    lease_generation: int
    request_sha256: str
    state: InvocationState
    provider_request_id: str | None = None
    response: dict[str, Any] | None = None
    error_code: str | None = None


class DurableWorkStore(Protocol):
    """Every method is one bounded database transaction."""

    def claim(
        self,
        *,
        queue: str,
        worker_instance: str,
        lease_seconds: int,
    ) -> LeaseClaim | None: ...

    def heartbeat(
        self,
        claim: LeaseClaim,
        *,
        lease_seconds: int,
    ) -> bool: ...

    def checkpoint(
        self,
        claim: LeaseClaim,
        *,
        checkpoint_type: str,
        payload: Mapping[str, Any],
    ) -> bool: ...

    def finalize(
        self,
        claim: LeaseClaim,
        result: WorkResult,
    ) -> bool: ...

    def uow_scope_for_claim(self, claim: LeaseClaim) -> UowScope: ...

    def reserve_invocation(
        self,
        claim: LeaseClaim,
        *,
        invocation_kind: InvocationKind,
        invocation_key: str,
        request_sha256: str,
    ) -> InvocationRecord: ...

    def mark_invocation_send_started(
        self,
        claim: LeaseClaim,
        record: InvocationRecord,
    ) -> bool: ...

    def finalize_invocation(
        self,
        claim: LeaseClaim,
        record: InvocationRecord,
        *,
        state: InvocationState,
        provider_request_id: str | None,
        response: Mapping[str, Any] | None,
        error_code: str | None,
    ) -> bool: ...


class WorkHandler(Protocol):
    def __call__(self, context: "WorkContext") -> WorkResult: ...


@dataclass
class WorkContext:
    claim: LeaseClaim
    store: DurableWorkStore
    _lease_lost: threading.Event

    @property
    def lease_is_valid(self) -> bool:
        return not self._lease_lost.is_set()

    def checkpoint(
        self,
        checkpoint_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        if self._lease_lost.is_set() or not self.store.checkpoint(
            self.claim,
            checkpoint_type=checkpoint_type,
            payload=payload,
        ):
            self._lease_lost.set()
            raise LeaseLostError("lease fence rejected checkpoint")

    def mark_lease_lost(self) -> None:
        """Prevent any outer finalize after a handler observes a rejected fence."""

        self._lease_lost.set()

    def invocation_dispatcher(self) -> "InvocationDispatcher":
        return InvocationDispatcher(
            store=self.store,
            claim=self.claim,
            lease_lost=self._lease_lost,
        )


class WorkerError(RuntimeError):
    pass


class RetryableWorkError(WorkerError):
    def __init__(
        self,
        code: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class TerminalWorkError(WorkerError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OutcomeUnknownError(WorkerError):
    def __init__(self, code: str = "outcome_unknown") -> None:
        super().__init__(code)
        self.code = code


class LeaseLostError(OutcomeUnknownError):
    pass


class DispatchKnownNotSent(WorkerError):
    """A connector proved no external effect could have occurred."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InvocationDispatcher:
    def __init__(
        self,
        *,
        store: DurableWorkStore,
        claim: LeaseClaim,
        lease_lost: threading.Event,
    ) -> None:
        self.store = store
        self.claim = claim
        self.lease_lost = lease_lost

    def dispatch(
        self,
        *,
        invocation_key: str,
        request: Mapping[str, Any],
        sender: Callable[[], tuple[Mapping[str, Any], str | None]],
        invocation_kind: InvocationKind | None = None,
    ) -> Mapping[str, Any]:
        """Dispatch once; ambiguous prior sends always require reconciliation."""

        digest = hashlib.sha256(_canonical_json(request)).hexdigest()
        selected_kind = (
            invocation_kind
            if invocation_kind is not None
            else _default_invocation_kind(self.claim)
        )
        record = self.store.reserve_invocation(
            self.claim,
            invocation_kind=selected_kind,
            invocation_key=invocation_key,
            request_sha256=digest,
        )
        if record.request_sha256 != digest:
            raise TerminalWorkError("invocation_request_conflict")
        if record.invocation_kind != selected_kind:
            raise TerminalWorkError("invocation_kind_conflict")
        if record.state == InvocationState.RESPONSE_RECEIVED:
            return dict(record.response or {})
        if record.state == InvocationState.KNOWN_FAILED:
            raise RetryableWorkError(record.error_code or "invocation_known_failed")
        if record.state in {
            InvocationState.SEND_STARTED,
            InvocationState.OUTCOME_POSSIBLE,
            InvocationState.OUTCOME_UNKNOWN,
        }:
            raise OutcomeUnknownError("prior_send_requires_reconciliation")
        if self.lease_lost.is_set() or not self.store.mark_invocation_send_started(
            self.claim,
            record,
        ):
            self.lease_lost.set()
            raise LeaseLostError("dispatch_permit_rejected")
        try:
            response, provider_request_id = sender()
        except DispatchKnownNotSent as exc:
            self._finish(
                record,
                state=InvocationState.KNOWN_FAILED,
                error_code=exc.code,
            )
            raise RetryableWorkError(exc.code) from exc
        except Exception as exc:
            log_event(
                "backend_invocation_sender_failed",
                queue=self.claim.queue,
                invocation_kind=record.invocation_kind.value,
                error_type=type(exc).__name__,
            )
            self._finish(
                record,
                state=InvocationState.OUTCOME_UNKNOWN,
                error_code="connector_outcome_unknown",
            )
            raise OutcomeUnknownError("connector_outcome_unknown") from exc
        self._finish(
            record,
            state=InvocationState.RESPONSE_RECEIVED,
            provider_request_id=provider_request_id,
            response=response,
        )
        return dict(response)

    def _finish(
        self,
        record: InvocationRecord,
        *,
        state: InvocationState,
        provider_request_id: str | None = None,
        response: Mapping[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        if not self.store.finalize_invocation(
            self.claim,
            record,
            state=state,
            provider_request_id=provider_request_id,
            response=response,
            error_code=error_code,
        ):
            self.lease_lost.set()
            raise LeaseLostError("invocation outcome lost its lease fence")


class DurableWorkStoreError(RuntimeError):
    """The durable work protocol or persisted row shape is invalid."""


class B3WorkerCompositionError(RuntimeError):
    """The configured runtime cannot safely construct a B3 handler."""


class B3ClaimInvariantError(RuntimeError):
    """A persisted B3 claim does not carry one exact trusted workload scope."""


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


def worker_uow_factory(context: WorkContext) -> UnitOfWorkFactory[Any]:
    """Return the composition-root UoW factory exposed by the durable store."""

    factory = getattr(context.store, "uow_factory", None)
    if factory is None:
        raise B3WorkerCompositionError(
            "B3 handler store does not expose its UnitOfWorkFactory"
        )
    return cast(UnitOfWorkFactory[Any], factory)


@dataclass(frozen=True, slots=True)
class _QueueTarget:
    kind: WorkKind
    selector: str | None = None


@dataclass(frozen=True, slots=True)
class _ClaimSeed:
    work_id: str
    operation_id: str | None
    queue: str
    kind: WorkKind
    namespace_id: str
    data_mode: str
    namespace_generation: int
    run_id: str | None
    arm_id: str | None
    subject_id: str
    authorization_epoch: int
    privacy_epoch: int
    retrieval_policy_epoch: int
    lease_generation: int
    fencing_token: str
    worker_instance: str
    metadata: dict[str, Any]


class PostgresDurableWorkStore:
    """PostgreSQL implementation of the fenced durable-work protocol.

    A claim intentionally spans two short transactions.  The first has only a
    :class:`WorkerClaimScope` and may call one audited SECURITY DEFINER claim
    function.  It commits before a second transaction loads the payload under
    the exact namespace/generation/run/arm/subject and governance epochs
    returned by PostgreSQL.  No cross-namespace payload scan is possible.
    """

    def __init__(
        self,
        settings: SleepBackendSettings,
        uow_factory: UnitOfWorkFactory[Any] | Any,
        *,
        purpose: str = "worker",
        id_generator: Callable[[], str] | None = None,
        base_retry_seconds: float = 2.0,
        max_retry_seconds: float = 300.0,
    ) -> None:
        if settings.process_role != ProcessRole.WORKER:
            raise ValueError("Postgres durable store requires a worker profile")
        if not purpose.strip():
            raise ValueError("worker purpose is required")
        if base_retry_seconds <= 0 or max_retry_seconds < base_retry_seconds:
            raise ValueError("retry bounds are invalid")
        self.settings = settings
        self.uow_factory = uow_factory
        self.purpose = purpose
        self.id_generator = id_generator or UUID7Generator()
        self.base_retry_seconds = base_retry_seconds
        self.max_retry_seconds = max_retry_seconds

    def claim(
        self,
        *,
        queue: str,
        worker_instance: str,
        lease_seconds: int,
    ) -> LeaseClaim | None:
        target = _queue_target(queue)
        _validate_lease_seconds(lease_seconds)
        if not worker_instance.strip():
            raise ValueError("worker_instance is required")
        scope = WorkerClaimScope(
            data_mode=self.settings.data_mode.value,
            purpose=self.purpose,
            service_principal_id=self.settings.service_principal_id,
            worker_instance=worker_instance,
        )
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                query, params = self._claim_statement(
                    target,
                    worker_instance=worker_instance,
                    lease_seconds=lease_seconds,
                )
                cursor.execute(query, params)
                row = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if row is None:
            return None
        seed = self._claim_seed(
            queue=queue,
            target=target,
            worker_instance=worker_instance,
            lease_seconds=lease_seconds,
            row=row,
        )
        return self._load_exact_claim(seed)

    def _claim_statement(
        self,
        target: _QueueTarget,
        *,
        worker_instance: str,
        lease_seconds: int,
    ) -> tuple[str, tuple[Any, ...]]:
        if target.kind == WorkKind.NORMALIZATION:
            return (
                "SELECT * FROM public.sleepagent_claim_normalization_work(%s, %s)",
                (worker_instance, lease_seconds),
            )
        if target.kind == WorkKind.JOURNEY:
            return (
                "SELECT * FROM public.sleepagent_claim_demo_journey(%s, %s)",
                (worker_instance, lease_seconds),
            )
        if target.kind == WorkKind.OPERATION:
            assert target.selector is not None
            return (
                "SELECT * FROM public.sleepagent_claim_operation(%s, %s, %s)",
                (target.selector, worker_instance, lease_seconds),
            )
        if target.kind == WorkKind.DELIVERY:
            assert target.selector is not None
            return (
                "SELECT * FROM public.sleepagent_claim_delivery(%s, %s, %s)",
                (target.selector, worker_instance, lease_seconds),
            )
        return (
            "SELECT * FROM public.sleepagent_claim_retention_job(%s, %s)",
            (worker_instance, lease_seconds),
        )

    def _claim_seed(
        self,
        *,
        queue: str,
        target: _QueueTarget,
        worker_instance: str,
        lease_seconds: int,
        row: Sequence[Any],
    ) -> _ClaimSeed:
        values = tuple(row)
        if target.kind == WorkKind.JOURNEY:
            if len(values) != 15:
                raise DurableWorkStoreError("invalid demo journey claim row")
            return _ClaimSeed(
                work_id=str(values[0]),
                operation_id=str(values[1]),
                queue=queue,
                kind=target.kind,
                namespace_id=str(values[2]),
                data_mode=str(values[3]),
                namespace_generation=int(values[4]),
                run_id=str(values[5]),
                arm_id=str(values[6]),
                subject_id=str(values[7]),
                authorization_epoch=int(values[10]),
                privacy_epoch=int(values[11]),
                retrieval_policy_epoch=int(values[12]),
                lease_generation=int(values[13]),
                fencing_token=str(values[14]),
                worker_instance=worker_instance,
                metadata={
                    "work_kind": target.kind.value,
                    "lease_seconds": lease_seconds,
                    "root_operation_id": str(values[1]),
                    "phase": str(values[8]),
                    "journey_version": int(values[9]),
                },
            )
        if target.kind == WorkKind.RETENTION:
            if len(values) != 14:
                raise DurableWorkStoreError("invalid retention claim row")
            epoch_offset = 9
            metadata = {
                "work_kind": target.kind.value,
                "lease_seconds": lease_seconds,
                "retention_domain": str(values[7]),
                "dek_generation": int(values[8]),
            }
            lease_offset = 12
            operation_id = None
        else:
            if len(values) != 13:
                raise DurableWorkStoreError(
                    f"invalid {target.kind.value} claim row"
                )
            epoch_offset = 8
            lease_offset = 11
            operation_id = str(values[0]) if target.kind == WorkKind.OPERATION else None
            metadata = {
                "work_kind": target.kind.value,
                "lease_seconds": lease_seconds,
            }
            if target.kind == WorkKind.NORMALIZATION:
                metadata["raw_ingress_record_id"] = str(values[7])
            elif target.kind == WorkKind.OPERATION:
                metadata["operation_type"] = str(values[7])
            else:
                metadata["handler_name"] = str(values[7])
                metadata["destination"] = target.selector
        return _ClaimSeed(
            work_id=str(values[0]),
            operation_id=operation_id,
            queue=queue,
            kind=target.kind,
            namespace_id=str(values[1]),
            data_mode=str(values[2]),
            namespace_generation=int(values[3]),
            run_id=None if values[4] is None else str(values[4]),
            arm_id=None if values[5] is None else str(values[5]),
            subject_id=str(values[6]),
            authorization_epoch=int(values[epoch_offset]),
            privacy_epoch=int(values[epoch_offset + 1]),
            retrieval_policy_epoch=int(values[epoch_offset + 2]),
            lease_generation=int(values[lease_offset]),
            fencing_token=str(values[lease_offset + 1]),
            worker_instance=worker_instance,
            metadata=metadata,
        )

    def _load_exact_claim(self, seed: _ClaimSeed) -> LeaseClaim | None:
        with self.uow_factory.begin(self._scope_from_seed(seed)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    self._exact_claim_query(seed.kind),
                    (
                        seed.work_id,
                        seed.lease_generation,
                        seed.fencing_token,
                        seed.worker_instance,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            uow.commit()
        if row is None:
            return None
        values = tuple(row)
        metadata = dict(seed.metadata)
        if seed.kind == WorkKind.JOURNEY:
            if len(values) != 8:
                raise DurableWorkStoreError("invalid demo journey payload row")
            operation_version = int(values[0])
            attempt, maximum, deadline = int(values[1]), int(values[2]), values[3]
            payload, snapshot = values[4], values[5]
            metadata.update(
                {
                    "root_operation_id": str(values[6]),
                    "phase": str(values[7]),
                }
            )
            operation_id = str(values[6])
        elif seed.kind == WorkKind.NORMALIZATION:
            if len(values) != 8:
                raise DurableWorkStoreError("invalid normalization payload row")
            operation_version = 0
            attempt, maximum, deadline = int(values[1]), int(values[2]), values[3]
            payload, snapshot = values[4], values[5]
            metadata.update(
                {
                    "work_generation": int(values[0]),
                    "raw_ingress_record_id": str(values[6]),
                    "protocol_version": int(values[7]),
                }
            )
            operation_id = None
        elif seed.kind == WorkKind.OPERATION:
            if len(values) != 8:
                raise DurableWorkStoreError("invalid operation payload row")
            operation_version = int(values[0])
            attempt, maximum, deadline = int(values[1]), int(values[2]), values[3]
            payload, snapshot = values[4], values[5]
            metadata.update(
                {
                    "operation_type": str(values[6]),
                    "queue_name": str(values[7]),
                }
            )
            operation_id = seed.work_id
        elif seed.kind == WorkKind.DELIVERY:
            if len(values) != 10:
                raise DurableWorkStoreError("invalid delivery payload row")
            operation_version = 0
            attempt, maximum, deadline = int(values[0]), int(values[1]), values[2]
            payload, snapshot = values[3], values[4]
            metadata.update(
                {
                    "handler_name": str(values[5]),
                    "destination": str(values[6]),
                    "source_event_id": str(values[7]),
                    "semantic_effect_key": str(values[8]),
                }
            )
            operation_id = None if values[9] is None else str(values[9])
        else:
            if len(values) != 9:
                raise DurableWorkStoreError("invalid retention payload row")
            operation_version = 0
            attempt, maximum, deadline = int(values[0]), int(values[1]), values[2]
            payload, snapshot = values[3], values[4]
            metadata.update(
                {
                    "retention_domain": str(values[5]),
                    "dek_generation": int(values[6]),
                    "job_kind": str(values[7]),
                    "semantic_key": str(values[8]),
                }
            )
            operation_id = None
        authorization_snapshot = _json_mapping(
            snapshot,
            field_name="authorization_snapshot_json",
        )
        self._validate_snapshot(seed, authorization_snapshot)
        return LeaseClaim(
            work_id=seed.work_id,
            operation_id=operation_id,
            queue=seed.queue,
            namespace_id=seed.namespace_id,
            data_mode=cast(Literal["live", "replay"], seed.data_mode),
            namespace_generation=seed.namespace_generation,
            run_id=seed.run_id,
            arm_id=seed.arm_id,
            subject_id=seed.subject_id,
            operation_version=operation_version,
            lease_generation=seed.lease_generation,
            fencing_token=seed.fencing_token,
            worker_instance=seed.worker_instance,
            attempt=attempt,
            max_attempts=maximum,
            lease_deadline=_aware_datetime(deadline, "lease_expires_at"),
            payload=_json_mapping(payload, field_name="work payload"),
            authorization_snapshot=authorization_snapshot,
            metadata=metadata,
        )

    def _exact_claim_query(self, kind: WorkKind) -> str:
        fence = (
            "lease_generation = %s AND fencing_token = %s "
            "AND worker_instance = %s AND lease_expires_at > clock_timestamp()"
        )
        if kind == WorkKind.JOURNEY:
            return (
                "SELECT journey.version, "
                "journey.failure_attempt_count + 1, "
                "journey.max_failure_attempts, journey.lease_expires_at, "
                "jsonb_build_object("
                "'schema_version', 'replay_journey_work.v1', "
                "'journey_id', journey.journey_id, "
                "'root_operation_id', journey.root_operation_id, "
                "'seed_id', journey.seed_id, "
                "'batch_size', (operation.operation_json ->> 'batch_size')::integer, "
                "'phase', journey.phase, "
                "'scenario_sha256', journey.scenario_sha256, "
                "'manifest_sha256', journey.manifest_sha256, "
                "'generator_version', journey.generator_version, "
                "'adapter_version', journey.adapter_version, "
                "'model_version', journey.model_version, "
                "'policy_sha256', journey.policy_sha256, "
                "'schema_manifest_sha256', journey.schema_manifest_sha256), "
                "jsonb_build_object("
                "'schema_version', 'workload_authorization_snapshot.v1', "
                "'workload_principal_id', NULLIF(current_setting("
                "'sleepagent.service_principal_id', TRUE), ''), "
                "'namespace_id', journey.namespace_id, "
                "'namespace_generation', journey.namespace_generation, "
                "'data_mode', journey.data_mode, "
                "'run_id', journey.run_id, 'arm_id', journey.arm_id, "
                "'subject_id', journey.subject_id, "
                "'purpose', NULLIF(current_setting("
                "'sleepagent.purpose', TRUE), ''), "
                "'allowed_handler', 'replay_journey', "
                "'authorization_epoch', epoch.authorization_epoch, "
                "'privacy_epoch', epoch.privacy_epoch, "
                "'retrieval_policy_epoch', epoch.retrieval_policy_epoch), "
                "journey.root_operation_id, journey.phase "
                "FROM public.backend_demo_journeys AS journey "
                "JOIN public.sleep_domain_operations AS operation "
                "ON operation.operation_id = journey.root_operation_id "
                "JOIN public.backend_subject_epochs AS epoch "
                "ON epoch.namespace_id = journey.namespace_id "
                "AND epoch.data_mode = journey.data_mode "
                "AND epoch.subject_id = journey.subject_id "
                "WHERE journey.journey_id = %s "
                "AND journey.lease_generation = %s "
                "AND journey.fencing_token = %s "
                "AND journey.worker_instance = %s "
                "AND journey.lease_expires_at > clock_timestamp()"
            )
        if kind == WorkKind.NORMALIZATION:
            return (
                "SELECT work_generation, attempt_count, max_attempts, "
                "lease_expires_at, work_json, authorization_snapshot_json, "
                "raw_ingress_record_id, protocol_version "
                "FROM public.sleep_domain_normalization_work "
                "WHERE work_id = %s AND status = 'running' AND " + fence
            )
        if kind == WorkKind.OPERATION:
            return (
                "SELECT cas_version, attempt_count, max_attempts, "
                "lease_expires_at, operation_json, COALESCE("
                "authorization_snapshot_json, workload_authorization_snapshot_json), "
                "operation_type, queue_name "
                "FROM public.sleep_domain_operations "
                "WHERE operation_id = %s AND status = 'running' AND " + fence
            )
        if kind == WorkKind.DELIVERY:
            return (
                "SELECT intent.attempt_count, intent.max_attempts, "
                "intent.lease_expires_at, intent.intent_json, "
                "intent.authorization_snapshot_json, intent.handler_name, "
                "intent.destination, intent.source_event_id, "
                "intent.semantic_effect_key, event.operation_id "
                "FROM public.backend_delivery_intents AS intent "
                "JOIN public.sleep_domain_domain_outbox AS event "
                "ON event.event_id = intent.source_event_id "
                "AND event.namespace_id = intent.namespace_id "
                "AND event.data_mode = intent.data_mode "
                "AND event.subject_id = intent.subject_id "
                "WHERE intent.delivery_intent_id = %s "
                "AND intent.status = 'running' "
                "AND intent.lease_generation = %s "
                "AND intent.fencing_token = %s "
                "AND intent.worker_instance = %s "
                "AND intent.lease_expires_at > clock_timestamp()"
            )
        return (
            "SELECT attempt_count, max_attempts, lease_expires_at, job_json, "
            "authorization_snapshot_json, retention_domain, dek_generation, "
            "job_kind, semantic_key FROM public.backend_retention_jobs "
            "WHERE retention_job_id = %s AND status = 'running' AND " + fence
        )

    def _scope_from_seed(self, seed: _ClaimSeed) -> UowScope:
        return UowScope(
            namespace_id=seed.namespace_id,
            data_mode=cast(Literal["live", "replay"], seed.data_mode),
            process_role="worker",
            purpose=self.purpose,
            service_principal_id=self.settings.service_principal_id,
            namespace_generation=seed.namespace_generation,
            subject_id=seed.subject_id,
            run_id=seed.run_id,
            arm_id=seed.arm_id,
            authorization_epoch=seed.authorization_epoch,
            privacy_epoch=seed.privacy_epoch,
            retrieval_policy_epoch=seed.retrieval_policy_epoch,
            worker_instance=seed.worker_instance,
        )

    def _scope_from_claim(self, claim: LeaseClaim) -> UowScope:
        snapshot = claim.authorization_snapshot
        if claim.subject_id is None:
            raise DurableWorkStoreError("durable claim requires subject scope")
        return UowScope(
            namespace_id=claim.namespace_id,
            data_mode=cast(Literal["live", "replay"], claim.data_mode),
            process_role="worker",
            purpose=self.purpose,
            service_principal_id=self.settings.service_principal_id,
            namespace_generation=claim.namespace_generation,
            subject_id=claim.subject_id,
            run_id=claim.run_id,
            arm_id=claim.arm_id,
            authorization_epoch=_snapshot_epoch(snapshot, "authorization_epoch"),
            privacy_epoch=_snapshot_epoch(snapshot, "privacy_epoch"),
            retrieval_policy_epoch=_snapshot_epoch(
                snapshot,
                "retrieval_policy_epoch",
            ),
            worker_instance=claim.worker_instance,
        )

    def uow_scope_for_claim(self, claim: LeaseClaim) -> UowScope:
        """Return the exact subject/governance UoW scope of a fenced claim."""

        return self._scope_from_claim(claim)

    def _validate_snapshot(
        self,
        seed: _ClaimSeed,
        snapshot: Mapping[str, Any],
    ) -> None:
        expected = {
            "authorization_epoch": seed.authorization_epoch,
            "privacy_epoch": seed.privacy_epoch,
            "retrieval_policy_epoch": seed.retrieval_policy_epoch,
        }
        actual = {name: _snapshot_epoch(snapshot, name) for name in expected}
        if actual != expected:
            raise DurableWorkStoreError(
                "claimed governance epochs do not match the payload snapshot"
            )

    def heartbeat(self, claim: LeaseClaim, *, lease_seconds: int) -> bool:
        _validate_lease_seconds(lease_seconds)
        kind = _claim_kind(claim)
        statement = {
            WorkKind.JOURNEY: (
                "SELECT public.sleepagent_heartbeat_demo_journey("
                "%s, %s, %s, %s)"
            ),
            WorkKind.NORMALIZATION: (
                "SELECT public.sleepagent_heartbeat_normalization_work("
                "%s, %s, %s, %s)"
            ),
            WorkKind.OPERATION: (
                "SELECT public.sleepagent_heartbeat_operation(%s, %s, %s, %s)"
            ),
            WorkKind.DELIVERY: (
                "SELECT public.sleepagent_heartbeat_delivery(%s, %s, %s, %s)"
            ),
            WorkKind.RETENTION: (
                "SELECT public.sleepagent_heartbeat_retention_job("
                "%s, %s, %s, %s)"
            ),
        }[kind]
        with self.uow_factory.begin(self._scope_from_claim(claim)) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    statement,
                    (
                        claim.work_id,
                        claim.lease_generation,
                        claim.fencing_token,
                        lease_seconds,
                    ),
                )
                renewed = _boolean_row(cursor.fetchone())
                if renewed and kind == WorkKind.OPERATION:
                    cursor.execute(
                        "SELECT lease_expires_at "
                        "FROM public.sleep_domain_operations "
                        "WHERE operation_id = %s AND lease_generation = %s "
                        "AND fencing_token = %s",
                        (
                            claim.work_id,
                            claim.lease_generation,
                            claim.fencing_token,
                        ),
                    )
                    deadline_row = cursor.fetchone()
                    if deadline_row is None:
                        renewed = False
                    else:
                        cursor.execute(
                            "INSERT INTO public.backend_operation_heartbeats ("
                            "heartbeat_id, namespace_id, data_mode, "
                            "namespace_generation, run_id, arm_id, subject_id, "
                            "operation_id, lease_generation, fencing_token, "
                            "worker_instance, lease_expires_at) VALUES ("
                            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (
                                self.id_generator(),
                                claim.namespace_id,
                                claim.data_mode,
                                claim.namespace_generation,
                                claim.run_id,
                                claim.arm_id,
                                claim.subject_id,
                                claim.work_id,
                                claim.lease_generation,
                                claim.fencing_token,
                                claim.worker_instance,
                                deadline_row[0],
                            ),
                        )
            finally:
                cursor.close()
            uow.commit()
        return renewed

    def checkpoint(
        self,
        claim: LeaseClaim,
        *,
        checkpoint_type: str,
        payload: Mapping[str, Any],
    ) -> bool:
        if not checkpoint_type.strip():
            raise ValueError("checkpoint_type is required")
        if _claim_kind(claim) != WorkKind.OPERATION:
            return False
        with self.uow_factory.begin(self._scope_from_claim(claim)) as uow:
            cursor = uow.connection.cursor()
            try:
                if not self._lock_operation_fence(cursor, claim):
                    return False
                self._insert_operation_checkpoint(
                    cursor,
                    claim,
                    checkpoint_type=checkpoint_type,
                    payload=payload,
                )
            finally:
                cursor.close()
            uow.commit()
        return True

    def finalize(self, claim: LeaseClaim, result: WorkResult) -> bool:
        kind = _claim_kind(claim)
        final_status = _final_status(kind, claim, result)
        retry_at: datetime | None = None
        with self.uow_factory.begin(self._scope_from_claim(claim)) as uow:
            cursor = uow.connection.cursor()
            try:
                if final_status == "retry":
                    delay = _retry_delay_seconds(
                        claim,
                        result,
                        base=self.base_retry_seconds,
                        maximum=self.max_retry_seconds,
                    )
                    cursor.execute(
                        "SELECT clock_timestamp() + make_interval(secs => %s)",
                        (delay,),
                    )
                    retry_row = cursor.fetchone()
                    if retry_row is None:
                        raise DurableWorkStoreError(
                            "PostgreSQL did not return the retry deadline"
                        )
                    retry_at = _aware_datetime(retry_row[0], "retry deadline")

                if kind == WorkKind.JOURNEY:
                    cursor.execute(
                        "SELECT public.sleepagent_finalize_demo_journey_attempt("
                        "%s, %s, %s, %s, %s, %s)",
                        (
                            claim.work_id,
                            claim.lease_generation,
                            claim.fencing_token,
                            final_status,
                            result.error_code or result.disposition.value,
                            retry_at,
                        ),
                    )
                    finalized = _boolean_row(cursor.fetchone())
                elif kind == WorkKind.OPERATION:
                    cursor.execute(
                        "SELECT public.sleepagent_finalize_operation("
                        "%s, %s, %s, %s, %s, %s, %s)",
                        (
                            claim.work_id,
                            claim.operation_version,
                            claim.lease_generation,
                            claim.fencing_token,
                            final_status,
                            result.error_code or result.disposition.value,
                            retry_at,
                        ),
                    )
                    finalized = _boolean_row(cursor.fetchone())
                    if not finalized:
                        return False
                    self._insert_operation_checkpoint(
                        cursor,
                        claim,
                        checkpoint_type="handler_finalize",
                        payload=result.model_dump(mode="json"),
                    )
                elif kind == WorkKind.NORMALIZATION:
                    cursor.execute(
                        "SELECT public.sleepagent_finalize_normalization_work("
                        "%s, %s, %s, %s, %s, %s)",
                        (
                            claim.work_id,
                            claim.lease_generation,
                            claim.fencing_token,
                            final_status,
                            retry_at,
                            result.error_code,
                        ),
                    )
                    finalized = _boolean_row(cursor.fetchone())
                elif kind == WorkKind.DELIVERY:
                    prior_state = self._lock_delivery_fence(cursor, claim)
                    if prior_state is None:
                        return False
                    self._insert_delivery_journal(
                        cursor,
                        claim,
                        from_state=prior_state,
                        to_state=final_status,
                        event={
                            "event": "handler_finalize",
                            "result": result.model_dump(mode="json"),
                        },
                    )
                    cursor.execute(
                        "SELECT public.sleepagent_finalize_delivery("
                        "%s, %s, %s, %s, %s)",
                        (
                            claim.work_id,
                            claim.lease_generation,
                            claim.fencing_token,
                            final_status,
                            retry_at,
                        ),
                    )
                    finalized = _boolean_row(cursor.fetchone())
                    if finalized and final_status == "outcome_unknown":
                        self._insert_delivery_reconciliation_operation(
                            cursor,
                            claim,
                            error_code=(
                                result.error_code
                                or WorkDisposition.OUTCOME_UNKNOWN.value
                            ),
                        )
                else:
                    cursor.execute(
                        "SELECT public.sleepagent_finalize_retention_job("
                        "%s, %s, %s, %s, %s)",
                        (
                            claim.work_id,
                            claim.lease_generation,
                            claim.fencing_token,
                            final_status,
                            retry_at,
                        ),
                    )
                    finalized = _boolean_row(cursor.fetchone())
            finally:
                cursor.close()
            if not finalized:
                return False
            uow.commit()
        return True

    def _insert_delivery_reconciliation_operation(
        self,
        cursor: Any,
        claim: LeaseClaim,
        *,
        error_code: str,
    ) -> None:
        """Create the sole reconciliation claim with the delivery terminal write."""

        if claim.operation_id is None:
            raise DurableWorkStoreError(
                "delivery reconciliation requires an operation origin"
            )
        invocation_key = "replay-delivery:" + str(
            claim.metadata.get("semantic_effect_key", "")
        ) + ":v1"
        cursor.execute(
            "SELECT intent.destination, intent.handler_name, "
            "intent.semantic_effect_key, intent.payload_sha256, "
            "source.policy_sha256 "
            "FROM public.backend_delivery_intents AS intent "
            "JOIN public.sleep_domain_domain_outbox AS event "
            "ON event.event_id = intent.source_event_id "
            "AND event.namespace_id = intent.namespace_id "
            "AND event.data_mode = intent.data_mode "
            "AND event.subject_id = intent.subject_id "
            "JOIN public.sleep_domain_operations AS source "
            "ON source.operation_id = event.operation_id "
            "AND source.namespace_id = intent.namespace_id "
            "AND source.data_mode = intent.data_mode "
            "AND source.subject_id = intent.subject_id "
            "WHERE intent.delivery_intent_id = %s "
            "AND intent.status = 'outcome_unknown' FOR UPDATE OF intent",
            (claim.work_id,),
        )
        row = cursor.fetchone()
        if row is None or not _is_sha256(str(row[3])) or not _is_sha256(str(row[4])):
            raise DurableWorkStoreError(
                "delivery reconciliation source is incomplete"
            )
        semantic_key = hashlib.sha256(
            _canonical_json(
                {
                    "operation_type": "delivery_reconciliation",
                    "delivery_intent_id": claim.work_id,
                }
            )
        ).hexdigest()
        workload = {
            "schema_version": "workload_authorization_snapshot.v1",
            "workload_principal_id": self.settings.service_principal_id,
            "namespace_id": claim.namespace_id,
            "namespace_generation": claim.namespace_generation,
            "data_mode": claim.data_mode,
            "run_id": claim.run_id,
            "arm_id": claim.arm_id,
            "subject_id": claim.subject_id,
            "purpose": self.purpose,
            "allowed_handler": "reconciliation",
            "authorization_epoch": _snapshot_epoch(
                claim.authorization_snapshot, "authorization_epoch"
            ),
            "privacy_epoch": _snapshot_epoch(
                claim.authorization_snapshot, "privacy_epoch"
            ),
            "retrieval_policy_epoch": _snapshot_epoch(
                claim.authorization_snapshot, "retrieval_policy_epoch"
            ),
        }
        payload = {
            "schema_version": "delivery_reconciliation_operation.v1",
            "delivery_intent_id": claim.work_id,
            "source_operation_id": claim.operation_id,
            "destination": str(row[0]),
            "handler_name": str(row[1]),
            "semantic_effect_key": str(row[2]),
            "payload_sha256": str(row[3]),
            "invocation_key": invocation_key,
            "trigger_error_code": error_code,
            "authorization_snapshot": workload,
        }
        encoded = _canonical_json(payload)
        operation_id = self.id_generator()
        cursor.execute(
            "INSERT INTO public.sleep_domain_operations ("
            "operation_id, namespace_id, data_mode, operation_type, subject_id, "
            "service_principal_id, actor_id, target_resource_id, "
            "target_resource_key, idempotency_key, request_sha256, status, "
            "cas_version, operation_json, created_at, updated_at, "
            "protocol_version, namespace_generation, run_id, arm_id, id_scheme, "
            "origin_kind, semantic_key, queue_name, priority, available_at, "
            "max_attempts, workload_authorization_snapshot_json, policy_sha256) "
            "VALUES (%s, %s, %s, 'delivery_reconciliation', %s, %s, NULL, %s, "
            "%s, %s, %s, 'pending', 0, %s::jsonb, clock_timestamp(), "
            "clock_timestamp(), 2, %s, %s, %s, 'uuidv7', 'system', %s, "
            "'reconciliation', 80, clock_timestamp(), 5, %s::jsonb, %s) "
            "ON CONFLICT (namespace_id, data_mode, namespace_generation, "
            "(COALESCE(run_id, '')), (COALESCE(arm_id, '')), operation_type, "
            "semantic_key) WHERE protocol_version >= 2 DO NOTHING",
            (
                operation_id,
                claim.namespace_id,
                claim.data_mode,
                claim.subject_id,
                self.settings.service_principal_id,
                claim.work_id,
                f"delivery:{claim.work_id}",
                f"delivery-reconciliation:{claim.work_id}",
                hashlib.sha256(encoded).hexdigest(),
                encoded.decode("utf-8"),
                claim.namespace_generation,
                claim.run_id,
                claim.arm_id,
                semantic_key,
                _canonical_json(workload).decode("utf-8"),
                str(row[4]),
            ),
        )

    def _lock_operation_fence(self, cursor: Any, claim: LeaseClaim) -> bool:
        cursor.execute(
            "SELECT public.sleepagent_heartbeat_operation(%s, %s, %s, %s)",
            (
                claim.work_id,
                claim.lease_generation,
                claim.fencing_token,
                _claim_lease_seconds(claim),
            ),
        )
        if not _boolean_row(cursor.fetchone()):
            return False
        cursor.execute(
            "SELECT cas_version FROM public.sleep_domain_operations "
            "WHERE operation_id = %s AND status = 'running' "
            "AND lease_generation = %s AND fencing_token = %s "
            "AND worker_instance = %s",
            (
                claim.work_id,
                claim.lease_generation,
                claim.fencing_token,
                claim.worker_instance,
            ),
        )
        row = cursor.fetchone()
        return row is not None and int(row[0]) == claim.operation_version

    def _lock_delivery_fence(
        self,
        cursor: Any,
        claim: LeaseClaim,
    ) -> str | None:
        cursor.execute(
            "SELECT public.sleepagent_heartbeat_delivery(%s, %s, %s, %s)",
            (
                claim.work_id,
                claim.lease_generation,
                claim.fencing_token,
                _claim_lease_seconds(claim),
            ),
        )
        if not _boolean_row(cursor.fetchone()):
            return None
        cursor.execute(
            "SELECT status FROM public.backend_delivery_intents "
            "WHERE delivery_intent_id = %s "
            "AND status IN ('running', 'dispatching') "
            "AND lease_generation = %s AND fencing_token = %s "
            "AND worker_instance = %s",
            (
                claim.work_id,
                claim.lease_generation,
                claim.fencing_token,
                claim.worker_instance,
            ),
        )
        row = cursor.fetchone()
        return None if row is None else str(row[0])

    def _lock_work_fence(self, cursor: Any, claim: LeaseClaim) -> bool:
        kind = _claim_kind(claim)
        if kind == WorkKind.OPERATION:
            return self._lock_operation_fence(cursor, claim)
        if kind == WorkKind.DELIVERY:
            return self._lock_delivery_fence(cursor, claim) is not None
        raise DurableWorkStoreError(
            f"{kind.value} work cannot own an external invocation"
        )

    def _insert_operation_checkpoint(
        self,
        cursor: Any,
        claim: LeaseClaim,
        *,
        checkpoint_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        cursor.execute(
            "SELECT COALESCE(MAX(checkpoint_sequence), 0) + 1 "
            "FROM public.backend_operation_checkpoints "
            "WHERE operation_id = %s",
            (claim.work_id,),
        )
        sequence_row = cursor.fetchone()
        if sequence_row is None:
            raise DurableWorkStoreError("checkpoint sequence was unavailable")
        encoded = _canonical_json(payload)
        cursor.execute(
            "INSERT INTO public.backend_operation_checkpoints ("
            "checkpoint_id, namespace_id, data_mode, namespace_generation, "
            "run_id, arm_id, subject_id, operation_id, checkpoint_sequence, "
            "lease_generation, fencing_token, checkpoint_kind, "
            "checkpoint_sha256, checkpoint_json) VALUES ("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            (
                self.id_generator(),
                claim.namespace_id,
                claim.data_mode,
                claim.namespace_generation,
                claim.run_id,
                claim.arm_id,
                claim.subject_id,
                claim.work_id,
                int(sequence_row[0]),
                claim.lease_generation,
                claim.fencing_token,
                checkpoint_type,
                hashlib.sha256(encoded).hexdigest(),
                encoded.decode("utf-8"),
            ),
        )

    def _insert_delivery_journal(
        self,
        cursor: Any,
        claim: LeaseClaim,
        *,
        from_state: str,
        to_state: str,
        event: Mapping[str, Any],
    ) -> None:
        cursor.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 "
            "FROM public.backend_delivery_journal "
            "WHERE delivery_intent_id = %s",
            (claim.work_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise DurableWorkStoreError("delivery journal sequence unavailable")
        cursor.execute(
            "INSERT INTO public.backend_delivery_journal ("
            "delivery_event_id, delivery_intent_id, namespace_id, data_mode, "
            "namespace_generation, run_id, arm_id, subject_id, sequence, "
            "from_state, to_state, lease_generation, fencing_token, "
            "event_json, occurred_at) VALUES ("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s::jsonb, clock_timestamp())",
            (
                self.id_generator(),
                claim.work_id,
                claim.namespace_id,
                claim.data_mode,
                claim.namespace_generation,
                claim.run_id,
                claim.arm_id,
                claim.subject_id,
                int(row[0]),
                from_state,
                to_state,
                claim.lease_generation,
                claim.fencing_token,
                _canonical_json(event).decode("utf-8"),
            ),
        )

    def reserve_invocation(
        self,
        claim: LeaseClaim,
        *,
        invocation_kind: InvocationKind,
        invocation_key: str,
        request_sha256: str,
    ) -> InvocationRecord:
        if not invocation_key.strip():
            raise ValueError("invocation_key is required")
        if not _is_sha256(request_sha256):
            raise ValueError("request_sha256 must be lowercase SHA-256")
        if claim.operation_id is None:
            raise TerminalWorkError("invocation_requires_operation_origin")
        with self.uow_factory.begin(self._scope_from_claim(claim)) as uow:
            cursor = uow.connection.cursor()
            try:
                if not self._lock_work_fence(cursor, claim):
                    raise LeaseLostError("invocation reservation lost its fence")
                cursor.execute(
                    "SELECT invocation.invocation_id, "
                    "invocation.invocation_kind, invocation.request_sha256, "
                    "invocation.current_state, invocation.lease_generation, "
                    "invocation.fencing_token, "
                    "invocation.provider_request_id, invocation.cas_version, "
                    "latest.event_json "
                    "FROM public.backend_invocations AS invocation "
                    "LEFT JOIN LATERAL ("
                    "SELECT journal.event_json "
                    "FROM public.backend_invocation_journal AS journal "
                    "WHERE journal.invocation_id = invocation.invocation_id "
                    "ORDER BY journal.sequence DESC LIMIT 1"
                    ") AS latest ON TRUE "
                    "WHERE invocation.namespace_id = %s "
                    "AND invocation.data_mode = %s "
                    "AND invocation.operation_id = %s "
                    "AND invocation.invocation_key = %s "
                    "FOR UPDATE OF invocation",
                    (
                        claim.namespace_id,
                        claim.data_mode,
                        claim.operation_id,
                        invocation_key,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    invocation_id = self.id_generator()
                    cursor.execute(
                        "INSERT INTO public.backend_invocations ("
                        "invocation_id, namespace_id, data_mode, "
                        "namespace_generation, run_id, arm_id, subject_id, "
                        "operation_id, invocation_kind, invocation_key, "
                        "request_sha256, current_state, lease_generation, "
                        "fencing_token, reserved_at, updated_at) VALUES ("
                        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                        "'reserved', %s, %s, clock_timestamp(), clock_timestamp())",
                        (
                            invocation_id,
                            claim.namespace_id,
                            claim.data_mode,
                            claim.namespace_generation,
                            claim.run_id,
                            claim.arm_id,
                            claim.subject_id,
                            claim.operation_id,
                            invocation_kind.value,
                            invocation_key,
                            request_sha256,
                            claim.lease_generation,
                            claim.fencing_token,
                        ),
                    )
                    self._insert_invocation_journal(
                        cursor,
                        claim,
                        invocation_id=invocation_id,
                        from_state=None,
                        to_state=InvocationState.RESERVED,
                        event={
                            "event": "reserved",
                            "invocation_kind": invocation_kind.value,
                            "request_sha256": request_sha256,
                        },
                    )
                    record = InvocationRecord(
                        invocation_id=invocation_id,
                        invocation_key=invocation_key,
                        invocation_kind=invocation_kind,
                        work_id=claim.work_id,
                        lease_generation=claim.lease_generation,
                        request_sha256=request_sha256,
                        state=InvocationState.RESERVED,
                    )
                else:
                    record = self._invocation_record(claim, invocation_key, row)
                    invocation_matches = (
                        record.invocation_kind == invocation_kind
                        and record.request_sha256 == request_sha256
                    )
                    can_rebind_reserved = (
                        invocation_matches
                        and record.state == InvocationState.RESERVED
                        and (
                            int(row[4]) != claim.lease_generation
                            or str(row[5]) != claim.fencing_token
                        )
                    )
                    can_reopen_known_failure = (
                        invocation_matches
                        and record.state == InvocationState.KNOWN_FAILED
                        and claim.lease_generation > int(row[4])
                    )
                    latest_event = (
                        {}
                        if row[8] is None
                        else _json_mapping(
                            row[8], field_name="invocation journal event"
                        )
                    )
                    can_reopen_reconciled_not_delivered = (
                        invocation_matches
                        and record.state == InvocationState.RECONCILED
                        and latest_event.get("resolution")
                        == "known_not_delivered"
                        and claim.lease_generation > int(row[4])
                    )
                    if (
                        can_rebind_reserved
                        or can_reopen_known_failure
                        or can_reopen_reconciled_not_delivered
                    ):
                        previous = record.state
                        cursor.execute(
                            "UPDATE public.backend_invocations SET "
                            "current_state = 'reserved', lease_generation = %s, "
                            "fencing_token = %s, provider_request_id = NULL, "
                            "response_sha256 = NULL, cas_version = cas_version + 1, "
                            "updated_at = clock_timestamp() "
                            "WHERE invocation_id = %s AND cas_version = %s "
                            "RETURNING cas_version",
                            (
                                claim.lease_generation,
                                claim.fencing_token,
                                record.invocation_id,
                                int(row[7]),
                            ),
                        )
                        if cursor.fetchone() is None:
                            raise LeaseLostError("invocation reservation CAS lost")
                        self._insert_invocation_journal(
                            cursor,
                            claim,
                            invocation_id=record.invocation_id,
                            from_state=previous,
                            to_state=InvocationState.RESERVED,
                            event={"event": "lease_rebound"},
                        )
                        record = record.model_copy(
                            update={
                                "lease_generation": claim.lease_generation,
                                "state": InvocationState.RESERVED,
                                "provider_request_id": None,
                                "response": None,
                                "error_code": None,
                            }
                        )
            finally:
                cursor.close()
            uow.commit()
        return record

    def mark_invocation_send_started(
        self,
        claim: LeaseClaim,
        record: InvocationRecord,
    ) -> bool:
        with self.uow_factory.begin(self._scope_from_claim(claim)) as uow:
            cursor = uow.connection.cursor()
            try:
                if not self._lock_work_fence(cursor, claim):
                    return False
                cursor.execute(
                    "SELECT current_state, cas_version, request_sha256, "
                    "lease_generation, fencing_token "
                    "FROM public.backend_invocations "
                    "WHERE invocation_id = %s AND invocation_key = %s "
                    "AND operation_id = %s FOR UPDATE",
                    (
                        record.invocation_id,
                        record.invocation_key,
                        claim.operation_id,
                    ),
                )
                row = cursor.fetchone()
                if (
                    row is None
                    or str(row[0]) != InvocationState.RESERVED.value
                    or str(row[2]) != record.request_sha256
                    or int(row[3]) != claim.lease_generation
                    or str(row[4]) != claim.fencing_token
                ):
                    return False
                if _claim_kind(claim) == WorkKind.DELIVERY:
                    cursor.execute(
                        "SELECT public.sleepagent_mark_delivery_dispatching("
                        "%s, %s, %s)",
                        (
                            claim.work_id,
                            claim.lease_generation,
                            claim.fencing_token,
                        ),
                    )
                    if not _boolean_row(cursor.fetchone()):
                        return False
                    self._insert_delivery_journal(
                        cursor,
                        claim,
                        from_state="running",
                        to_state="dispatching",
                        event={
                            "event": "dispatch_permit_committed",
                            "invocation_id": record.invocation_id,
                        },
                    )
                cursor.execute(
                    "UPDATE public.backend_invocations SET "
                    "current_state = 'send_started', "
                    "dispatch_permit_at = clock_timestamp(), "
                    "cas_version = cas_version + 1, "
                    "updated_at = clock_timestamp() "
                    "WHERE invocation_id = %s AND cas_version = %s "
                    "AND current_state = 'reserved' RETURNING cas_version",
                    (record.invocation_id, int(row[1])),
                )
                if cursor.fetchone() is None:
                    return False
                self._insert_invocation_journal(
                    cursor,
                    claim,
                    invocation_id=record.invocation_id,
                    from_state=InvocationState.RESERVED,
                    to_state=InvocationState.SEND_STARTED,
                    event={"event": "dispatch_permit_committed"},
                )
            finally:
                cursor.close()
            uow.commit()
        return True

    def finalize_invocation(
        self,
        claim: LeaseClaim,
        record: InvocationRecord,
        *,
        state: InvocationState,
        provider_request_id: str | None,
        response: Mapping[str, Any] | None,
        error_code: str | None,
    ) -> bool:
        allowed = {
            InvocationState.RESPONSE_RECEIVED,
            InvocationState.KNOWN_FAILED,
            InvocationState.OUTCOME_UNKNOWN,
        }
        if state not in allowed:
            raise ValueError("invocation can only finalize to a terminal outcome")
        if state == InvocationState.RESPONSE_RECEIVED and response is None:
            raise ValueError("response_received requires a response payload")
        encoded_response = (
            None if response is None else _canonical_json(response)
        )
        response_sha256 = (
            None
            if encoded_response is None
            else hashlib.sha256(encoded_response).hexdigest()
        )
        with self.uow_factory.begin(self._scope_from_claim(claim)) as uow:
            cursor = uow.connection.cursor()
            try:
                if not self._lock_work_fence(cursor, claim):
                    return False
                cursor.execute(
                    "SELECT current_state, cas_version, request_sha256, "
                    "lease_generation, fencing_token "
                    "FROM public.backend_invocations "
                    "WHERE invocation_id = %s AND invocation_key = %s "
                    "AND operation_id = %s FOR UPDATE",
                    (
                        record.invocation_id,
                        record.invocation_key,
                        claim.operation_id,
                    ),
                )
                row = cursor.fetchone()
                if (
                    row is None
                    or str(row[0]) not in {
                        InvocationState.SEND_STARTED.value,
                        InvocationState.OUTCOME_POSSIBLE.value,
                    }
                    or str(row[2]) != record.request_sha256
                    or int(row[3]) != claim.lease_generation
                    or str(row[4]) != claim.fencing_token
                ):
                    return False
                cursor.execute(
                    "UPDATE public.backend_invocations SET current_state = %s, "
                    "provider_request_id = COALESCE(%s, provider_request_id), "
                    "response_sha256 = %s, cas_version = cas_version + 1, "
                    "updated_at = clock_timestamp() "
                    "WHERE invocation_id = %s AND cas_version = %s "
                    "AND current_state IN ('send_started', 'outcome_possible') "
                    "RETURNING cas_version",
                    (
                        state.value,
                        provider_request_id,
                        response_sha256,
                        record.invocation_id,
                        int(row[1]),
                    ),
                )
                if cursor.fetchone() is None:
                    return False
                event: dict[str, Any] = {"event": state.value}
                if provider_request_id is not None:
                    event["provider_request_id"] = provider_request_id
                if response is not None:
                    event["response"] = dict(response)
                    event["response_sha256"] = response_sha256
                if error_code is not None:
                    event["error_code"] = error_code
                self._insert_invocation_journal(
                    cursor,
                    claim,
                    invocation_id=record.invocation_id,
                    from_state=InvocationState(str(row[0])),
                    to_state=state,
                    event=event,
                )
            finally:
                cursor.close()
            uow.commit()
        return True

    def _invocation_record(
        self,
        claim: LeaseClaim,
        invocation_key: str,
        row: Sequence[Any],
    ) -> InvocationRecord:
        event = (
            {}
            if row[8] is None
            else _json_mapping(row[8], field_name="invocation journal event")
        )
        response_value = event.get("response")
        response = (
            dict(response_value)
            if isinstance(response_value, Mapping)
            else None
        )
        return InvocationRecord(
            invocation_id=str(row[0]),
            invocation_key=invocation_key,
            invocation_kind=InvocationKind(str(row[1])),
            work_id=claim.work_id,
            lease_generation=int(row[4]),
            request_sha256=str(row[2]),
            state=InvocationState(str(row[3])),
            provider_request_id=(None if row[6] is None else str(row[6])),
            response=response,
            error_code=(
                None
                if event.get("error_code") is None
                else str(event["error_code"])
            ),
        )

    def _insert_invocation_journal(
        self,
        cursor: Any,
        claim: LeaseClaim,
        *,
        invocation_id: str,
        from_state: InvocationState | None,
        to_state: InvocationState,
        event: Mapping[str, Any],
    ) -> None:
        cursor.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 "
            "FROM public.backend_invocation_journal "
            "WHERE invocation_id = %s",
            (invocation_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise DurableWorkStoreError("invocation journal sequence unavailable")
        cursor.execute(
            "INSERT INTO public.backend_invocation_journal ("
            "invocation_event_id, invocation_id, namespace_id, data_mode, "
            "namespace_generation, run_id, arm_id, subject_id, sequence, "
            "from_state, to_state, lease_generation, fencing_token, "
            "event_json, occurred_at) VALUES ("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s::jsonb, clock_timestamp())",
            (
                self.id_generator(),
                invocation_id,
                claim.namespace_id,
                claim.data_mode,
                claim.namespace_generation,
                claim.run_id,
                claim.arm_id,
                claim.subject_id,
                int(row[0]),
                None if from_state is None else from_state.value,
                to_state.value,
                claim.lease_generation,
                claim.fencing_token,
                _canonical_json(event).decode("utf-8"),
            ),
        )

    def healthcheck(self, *, worker_instance: str = "healthcheck") -> bool:
        scope = WorkerClaimScope(
            data_mode=self.settings.data_mode.value,
            purpose=self.purpose,
            service_principal_id=self.settings.service_principal_id,
            worker_instance=worker_instance,
        )
        signatures = _required_function_signatures(
            self.settings.worker_queues
        )
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT public.sleepagent_principal_context_allows()"
                )
                if not _boolean_row(cursor.fetchone()):
                    return False
                for signature in signatures:
                    cursor.execute(
                        "SELECT COALESCE(has_function_privilege("
                        "current_user, to_regprocedure(%s), 'EXECUTE'), false)",
                        (signature,),
                    )
                    if not _boolean_row(cursor.fetchone()):
                        return False
            finally:
                cursor.close()
            uow.commit()
        return True

class _Heartbeat:
    def __init__(
        self,
        *,
        store: DurableWorkStore,
        claim: LeaseClaim,
        lease_seconds: int,
        interval_seconds: float,
        lease_lost: threading.Event,
    ) -> None:
        self.store = store
        self.claim = claim
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self.lease_lost = lease_lost
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"sleepagent-heartbeat-{claim.work_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.interval_seconds * 2))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                renewed = self.store.heartbeat(
                    self.claim,
                    lease_seconds=self.lease_seconds,
                )
            except Exception as exc:
                # A database error (notably lock_timeout) is indeterminate, not
                # durable evidence that ownership changed.  Keep the transport
                # gates fail-closed: reservation, dispatch, completion, and the
                # next successful heartbeat all revalidate the current fence.
                log_event(
                    "backend_worker_heartbeat_transient_failure",
                    queue=self.claim.queue,
                    error_type=type(exc).__name__,
                )
                continue
            if not renewed:
                # The store's fenced heartbeat returned an authoritative
                # rejection: the current generation can no longer renew.
                self.lease_lost.set()
                return


class DurableWorkerRuntime:
    def __init__(
        self,
        runtime: SleepBackendRuntime,
        *,
        store: DurableWorkStore,
        handlers: Mapping[str, WorkHandler],
        lease_seconds: int = 30,
        heartbeat_interval_seconds: float = 10.0,
        idle_poll_seconds: float = 0.25,
        worker_instance: str | None = None,
    ) -> None:
        if runtime.settings.process_role != ProcessRole.WORKER:
            raise ValueError("durable worker requires a worker capability profile")
        if lease_seconds < 3:
            raise ValueError("lease_seconds must be at least three seconds")
        if not 0 < heartbeat_interval_seconds < lease_seconds / 2:
            raise ValueError("heartbeat interval must be less than half the lease")
        configured = set(runtime.settings.worker_queues)
        if configured != set(handlers):
            raise ValueError("worker handler registry must equal configured queues")
        self.runtime = runtime
        self.store = store
        self.handlers = dict(handlers)
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.idle_poll_seconds = idle_poll_seconds
        self.worker_instance = worker_instance or f"worker-{uuid4()}"
        self._stop = threading.Event()
        self._claiming = True
        self._in_flight = 0
        self._state_lock = threading.Lock()

    @property
    def in_flight(self) -> int:
        with self._state_lock:
            return self._in_flight

    def stop_claiming(self) -> None:
        self._claiming = False
        self._stop.set()

    def run_forever(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self.idle_poll_seconds)

    def run_once(self) -> bool:
        if not self._claiming:
            return False
        for queue in self._ordered_queues():
            claim = self.store.claim(
                queue=queue,
                worker_instance=self.worker_instance,
                lease_seconds=self.lease_seconds,
            )
            if claim is not None:
                self._execute(claim, self.handlers[queue])
                return True
        return False

    def drain(self, timeout_seconds: float) -> bool:
        self.stop_claiming()
        deadline = time.monotonic() + timeout_seconds
        while self.in_flight and time.monotonic() < deadline:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return self.in_flight == 0

    def _ordered_queues(self) -> tuple[str, ...]:
        rank = {name: index for index, name in enumerate(DEFAULT_QUEUE_ORDER)}

        def queue_rank(value: str) -> int:
            if value.startswith("delivery:"):
                return rank["delivery"]
            return rank.get(value, len(rank))

        return tuple(
            sorted(
                self.handlers,
                key=lambda value: (queue_rank(value), value),
            )
        )

    def _execute(self, claim: LeaseClaim, handler: WorkHandler) -> None:
        lost = threading.Event()
        heartbeat = _Heartbeat(
            store=self.store,
            claim=claim,
            lease_seconds=self.lease_seconds,
            interval_seconds=self.heartbeat_interval_seconds,
            lease_lost=lost,
        )
        with self._state_lock:
            self._in_flight += 1
        heartbeat.start()
        result: WorkResult | None = None
        try:
            try:
                result = handler(WorkContext(claim, self.store, lost))
            except RetryableWorkError as exc:
                result = WorkResult(
                    disposition=WorkDisposition.RETRYABLE,
                    error_code=exc.code,
                    retry_after_seconds=exc.retry_after_seconds,
                )
            except TerminalWorkError as exc:
                result = WorkResult(
                    disposition=WorkDisposition.TERMINAL,
                    error_code=exc.code,
                )
            except OutcomeUnknownError as exc:
                result = WorkResult(
                    disposition=WorkDisposition.OUTCOME_UNKNOWN,
                    error_code=exc.code,
                )
            except Exception as exc:
                log_event(
                    "backend_worker_handler_failed",
                    queue=claim.queue,
                    error_type=type(exc).__name__,
                )
                result = WorkResult(
                    disposition=WorkDisposition.OUTCOME_UNKNOWN,
                    error_code="unclassified_handler_failure",
                )
            if (
                not lost.is_set()
                and result.finalization_mode == WorkFinalizationMode.WORKER_OWNED
            ):
                if not self.store.finalize(claim, result):
                    lost.set()
        finally:
            if result is not None:
                metric_outcome = (
                    "lease_lost" if lost.is_set() else result.disposition.value
                )
                record_backend_queue(queue=claim.queue, outcome=metric_outcome)
                _record_domain_signal(
                    claim,
                    result=result,
                    lease_lost=lost.is_set(),
                )
                log_event(
                    "backend_worker_completed",
                    queue=claim.queue,
                    outcome=(
                        "lease_lost"
                        if lost.is_set()
                        else result.disposition.value
                    ),
                    attempt=claim.attempt,
                )
            heartbeat.stop()
            with self._state_lock:
                self._in_flight -= 1


def _record_domain_signal(
    claim: LeaseClaim,
    *,
    result: WorkResult,
    lease_lost: bool,
) -> None:
    outcome = "outcome_unknown" if lease_lost else result.disposition.value
    category = None
    if claim.queue in {"product_agent", "product_interaction"}:
        category = "product"
    elif claim.queue == "fast_path":
        category = "safety"
    elif claim.queue in {"retention", "demo_reset"}:
        category = "retention"
    elif claim.queue == "reconciliation":
        category = "reconciliation"
    if category is not None:
        record_backend_signal(category=category, outcome=outcome)
    error_code = (result.error_code or "").lower()
    if "timeout" in error_code:
        record_backend_signal(category="provider", outcome="timeout")
    if result.disposition == WorkDisposition.OUTCOME_UNKNOWN:
        record_backend_signal(
            category="reconciliation",
            outcome="reconciliation_required",
        )


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _queue_target(queue: str) -> _QueueTarget:
    value = queue.strip()
    if not value:
        raise ValueError("queue is required")
    if value == "ingestion":
        return _QueueTarget(WorkKind.NORMALIZATION)
    if value == "replay_journey":
        return _QueueTarget(WorkKind.JOURNEY)
    if value == "retention":
        return _QueueTarget(WorkKind.RETENTION)
    if value == "delivery":
        return _QueueTarget(WorkKind.DELIVERY, "delivery")
    if value.startswith("delivery:"):
        destination = value.partition(":")[2].strip()
        if not destination:
            raise ValueError("delivery queue requires a destination")
        return _QueueTarget(WorkKind.DELIVERY, destination)
    return _QueueTarget(WorkKind.OPERATION, value)


def _claim_kind(claim: LeaseClaim) -> WorkKind:
    stored = claim.metadata.get("work_kind")
    if stored is None:
        return _queue_target(claim.queue).kind
    try:
        return WorkKind(str(stored))
    except ValueError as exc:
        raise DurableWorkStoreError("claim contains an unknown work_kind") from exc


def _default_invocation_kind(claim: LeaseClaim) -> InvocationKind:
    if _claim_kind(claim) == WorkKind.DELIVERY:
        return InvocationKind.EXTERNAL_SINK
    if claim.queue == "product_agent":
        return InvocationKind.MODEL
    return InvocationKind.PROVIDER


def _json_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    parsed = value
    if isinstance(value, bytes):
        parsed = json.loads(value.decode("utf-8"))
    elif isinstance(value, str):
        parsed = json.loads(value)
    if not isinstance(parsed, Mapping):
        raise DurableWorkStoreError(f"{field_name} must be a JSON object")
    return dict(parsed)


def _snapshot_epoch(snapshot: Mapping[str, Any], name: str) -> int:
    try:
        value = int(snapshot[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableWorkStoreError(
            f"authorization snapshot has invalid {name}"
        ) from exc
    if value < 0:
        raise DurableWorkStoreError(
            f"authorization snapshot has negative {name}"
        )
    return value


def _aware_datetime(value: Any, field_name: str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DurableWorkStoreError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _boolean_row(row: Any) -> bool:
    return bool(row is not None and row[0] is True)


def _validate_lease_seconds(value: int) -> None:
    if not 1 <= value <= 3_600:
        raise ValueError("lease_seconds must be between 1 and 3600")


def _claim_lease_seconds(claim: LeaseClaim) -> int:
    value = claim.metadata.get("lease_seconds")
    if isinstance(value, bool) or not isinstance(value, int):
        raise DurableWorkStoreError(
            "durable claim is missing its configured lease_seconds"
        )
    _validate_lease_seconds(value)
    return value


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _retry_delay_seconds(
    claim: LeaseClaim,
    result: WorkResult,
    *,
    base: float,
    maximum: float,
) -> float:
    if result.retry_after_seconds is not None:
        return min(maximum, result.retry_after_seconds)
    exponent = min(max(0, claim.attempt - 1), 16)
    return float(min(maximum, base * (2**exponent)))


def _final_status(
    kind: WorkKind,
    claim: LeaseClaim,
    result: WorkResult,
) -> str:
    if result.disposition == WorkDisposition.RETRYABLE:
        if claim.attempt < claim.max_attempts:
            return "retry"
        # Replay journeys have an explicit terminal receipt/root-operation
        # protocol rather than a generic dead-letter state.
        return "failed" if kind == WorkKind.JOURNEY else "dead_letter"
    statuses = {
        WorkKind.JOURNEY: {
            WorkDisposition.SUCCEEDED: "failed",
            WorkDisposition.TERMINAL: "failed",
            WorkDisposition.OUTCOME_UNKNOWN: "reconciliation_required",
        },
        WorkKind.NORMALIZATION: {
            WorkDisposition.SUCCEEDED: "succeeded",
            WorkDisposition.TERMINAL: "quarantined",
            WorkDisposition.OUTCOME_UNKNOWN: "quarantined",
        },
        WorkKind.OPERATION: {
            WorkDisposition.SUCCEEDED: "succeeded",
            WorkDisposition.TERMINAL: "failed",
            WorkDisposition.OUTCOME_UNKNOWN: "outcome_unknown",
        },
        WorkKind.DELIVERY: {
            WorkDisposition.SUCCEEDED: "delivered",
            WorkDisposition.TERMINAL: "dead_letter",
            WorkDisposition.OUTCOME_UNKNOWN: "outcome_unknown",
        },
        WorkKind.RETENTION: {
            WorkDisposition.SUCCEEDED: "succeeded",
            WorkDisposition.TERMINAL: "failed",
            WorkDisposition.OUTCOME_UNKNOWN: "reconciliation_required",
        },
    }
    return statuses[kind][result.disposition]


def _required_function_signatures(queues: Sequence[str]) -> tuple[str, ...]:
    groups = {
        WorkKind.JOURNEY: (
            "public.sleepagent_claim_demo_journey(text,integer)",
            "public.sleepagent_heartbeat_demo_journey(text,bigint,text,integer)",
            "public.sleepagent_wait_demo_journey(text,bigint,text,text,timestamptz)",
            "public.sleepagent_finalize_demo_journey_attempt(text,bigint,text,text,text,timestamptz)",
            "public.sleepagent_succeed_demo_journey(text,bigint,text,jsonb)",
        ),
        WorkKind.NORMALIZATION: (
            "public.sleepagent_claim_normalization_work(text,integer)",
            "public.sleepagent_heartbeat_normalization_work(text,bigint,text,integer)",
            "public.sleepagent_finalize_normalization_work(text,bigint,text,text,timestamptz,text)",
        ),
        WorkKind.OPERATION: (
            "public.sleepagent_claim_operation(text,text,integer)",
            "public.sleepagent_heartbeat_operation(text,bigint,text,integer)",
            "public.sleepagent_finalize_operation(text,bigint,bigint,text,text,text,timestamptz)",
            "public.sleepagent_operation_fence_allows(text,bigint,text)",
        ),
        WorkKind.DELIVERY: (
            "public.sleepagent_claim_delivery(text,text,integer)",
            "public.sleepagent_heartbeat_delivery(text,bigint,text,integer)",
            "public.sleepagent_mark_delivery_dispatching(text,bigint,text)",
            "public.sleepagent_finalize_delivery(text,bigint,text,text,timestamptz)",
        ),
        WorkKind.RETENTION: (
            "public.sleepagent_claim_retention_job(text,integer)",
            "public.sleepagent_heartbeat_retention_job(text,bigint,text,integer)",
            "public.sleepagent_finalize_retention_job(text,bigint,text,text,timestamptz)",
        ),
    }
    required: list[str] = []
    for queue in queues:
        for signature in groups[_queue_target(queue).kind]:
            if signature not in required:
                required.append(signature)
        if queue in {"sleep_command", "product_interaction"}:
            signature = COMMAND_AUTHORITY_FUNCTION_SIGNATURE
            if signature not in required:
                required.append(signature)
        if queue == "demo_advance":
            signature = SCENARIO_CLOCK_AUTHORITY_FUNCTION_SIGNATURE
            if signature not in required:
                required.append(signature)
        if _queue_target(queue).kind == WorkKind.DELIVERY:
            signature = DELIVERY_AUTHORITY_FUNCTION_SIGNATURE
            if signature not in required:
                required.append(signature)
    return tuple(required)


@dataclass(frozen=True, slots=True)
class _HealthcheckOnlyHandler:
    queue: str

    def __call__(self, context: WorkContext) -> WorkResult:
        del context
        raise DurableWorkStoreError(
            f"healthcheck-only registry cannot execute queue {self.queue!r}"
        )


def build_worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sleepagent-worker",
        description="Run or probe the fenced SleepAgent durable worker",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    run = subparsers.add_parser("run", help="run the durable claim loop")
    run.add_argument("--lease-seconds", type=int, default=30)
    run.add_argument("--heartbeat-seconds", type=float, default=10.0)
    run.add_argument("--idle-poll-seconds", type=float, default=0.25)
    run.add_argument("--drain-seconds", type=float, default=30.0)
    subparsers.add_parser(
        "healthcheck",
        help="verify schema attestation and worker function privileges",
    )
    return parser


def _cli_handlers(settings: SleepBackendSettings) -> dict[str, WorkHandler]:
    # Import lazily: the adapters implement WorkHandler and therefore depend on
    # this module's public runtime contracts.
    from sleepagent.workers.product import (
        build_product_agent_worker_handlers,
    )
    from sleepagent.workers.ingestion import build_b3_worker_handlers
    from sleepagent.workers.commands import build_command_worker_handlers
    from sleepagent.workers.demo import build_demo_worker_handlers
    from sleepagent.workers.effects import build_effect_worker_handlers
    from sleepagent.workers.retention import build_retention_worker_handlers

    handlers = build_b3_worker_handlers(settings)
    product_handlers = build_product_agent_worker_handlers(settings)
    command_handlers = build_command_worker_handlers(settings)
    demo_handlers = build_demo_worker_handlers(settings)
    effect_handlers = build_effect_worker_handlers(settings)
    retention_handlers = build_retention_worker_handlers(settings)
    registries = (
        handlers,
        product_handlers,
        command_handlers,
        demo_handlers,
        effect_handlers,
        retention_handlers,
    )
    overlap: set[str] = set()
    for index, registry in enumerate(registries):
        for other in registries[index + 1 :]:
            overlap.update(set(registry).intersection(other))
    if overlap:
        raise DurableWorkStoreError(
            "worker queue has more than one explicit handler: "
            + ", ".join(sorted(overlap))
        )
    handlers.update(product_handlers)
    handlers.update(command_handlers)
    handlers.update(demo_handlers)
    handlers.update(effect_handlers)
    handlers.update(retention_handlers)
    unsupported = set(settings.worker_queues) - set(handlers)
    if unsupported:
        raise DurableWorkStoreError(
            "worker handlers must be explicitly composed: "
            + ", ".join(sorted(unsupported))
        )
    return {queue: handlers[queue] for queue in settings.worker_queues}


def run_worker_command(
    action: str,
    *,
    settings: SleepBackendSettings,
    handlers: Mapping[str, WorkHandler] | None = None,
    lease_seconds: int = 30,
    heartbeat_seconds: float = 10.0,
    idle_poll_seconds: float = 0.25,
    drain_seconds: float = 30.0,
) -> int:
    if settings.process_role != ProcessRole.WORKER:
        raise DurableWorkStoreError("worker entry point requires PROCESS_ROLE=worker")
    selected_handlers = (
        dict(handlers)
        if handlers is not None
        else (
            _cli_handlers(settings)
            if action == "run"
            else {
                queue: _HealthcheckOnlyHandler(queue)
                for queue in settings.worker_queues
            }
        )
    )
    runtime = build_backend_runtime(
        settings,
        worker_handlers=selected_handlers,
    )
    store = PostgresDurableWorkStore(settings, runtime.uow_factory)
    asyncio.run(runtime.start())
    try:
        if action == "healthcheck":
            ready = store.healthcheck()
            print(json.dumps({"ready": ready, "process_role": "worker"}))
            return 0 if ready else 1
        if action != "run":
            raise ValueError(f"unknown worker action: {action}")
        worker = DurableWorkerRuntime(
            runtime,
            store=store,
            handlers=selected_handlers,
            lease_seconds=lease_seconds,
            heartbeat_interval_seconds=heartbeat_seconds,
            idle_poll_seconds=idle_poll_seconds,
        )

        def request_stop(signum: int, frame: object) -> None:
            del signum, frame
            worker.stop_claiming()

        prior_handlers: dict[signal.Signals, Any] = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            prior_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        try:
            if not store.healthcheck(worker_instance=worker.worker_instance):
                return 1
            worker.run_forever()
            return 0 if worker.drain(drain_seconds) else 4
        finally:
            for signum, previous in prior_handlers.items():
                signal.signal(signum, previous)
    finally:
        asyncio.run(runtime.close())


def main(argv: Sequence[str] | None = None) -> int:
    args = build_worker_parser().parse_args(argv)
    try:
        settings = SleepBackendSettings.from_environment()
        return run_worker_command(
            args.action,
            settings=settings,
            lease_seconds=getattr(args, "lease_seconds", 30),
            heartbeat_seconds=getattr(args, "heartbeat_seconds", 10.0),
            idle_poll_seconds=getattr(args, "idle_poll_seconds", 0.25),
            drain_seconds=getattr(args, "drain_seconds", 30.0),
        )
    except (ValueError, RuntimeError) as exc:
        print(f"worker {args.action} failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised as a process.
    raise SystemExit(main())


__all__ = [
    "DispatchKnownNotSent",
    "DurableWorkStore",
    "DurableWorkStoreError",
    "DurableWorkerRuntime",
    "InvocationDispatcher",
    "InvocationKind",
    "InvocationRecord",
    "InvocationState",
    "LeaseClaim",
    "LeaseLostError",
    "OutcomeUnknownError",
    "PostgresDurableWorkStore",
    "RetryableWorkError",
    "TerminalWorkError",
    "WorkContext",
    "WorkDisposition",
    "WorkFinalizationMode",
    "WorkHandler",
    "WorkKind",
    "WorkResult",
    "build_worker_parser",
    "main",
    "run_worker_command",
]
