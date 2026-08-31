"""Minimal durable-worker contracts with no concrete handler dependencies."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sleepagent.persistence.uow import UowScope


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
        self, *, queue: str, worker_instance: str, lease_seconds: int
    ) -> LeaseClaim | None: ...

    def heartbeat(self, claim: LeaseClaim, *, lease_seconds: int) -> bool: ...

    def checkpoint(
        self,
        claim: LeaseClaim,
        *,
        checkpoint_type: str,
        payload: Mapping[str, Any],
    ) -> bool: ...

    def finalize(self, claim: LeaseClaim, result: WorkResult) -> bool: ...

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
        self, claim: LeaseClaim, record: InvocationRecord
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


class InvocationDispatch(Protocol):
    def dispatch(
        self,
        *,
        invocation_key: str,
        request: Mapping[str, Any],
        sender: Callable[[], tuple[Mapping[str, Any], str | None]],
        invocation_kind: InvocationKind | None = None,
    ) -> Mapping[str, Any]: ...


class WorkHandler(Protocol):
    def __call__(self, context: "WorkContext") -> WorkResult: ...


class WorkerError(RuntimeError):
    pass


class RetryableWorkError(WorkerError):
    def __init__(
        self, code: str, *, retry_after_seconds: float | None = None
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


_DispatcherFactory = Callable[..., InvocationDispatch]
_dispatcher_factory: _DispatcherFactory | None = None


def _install_invocation_dispatcher_factory(factory: _DispatcherFactory) -> None:
    global _dispatcher_factory
    _dispatcher_factory = factory


@dataclass
class WorkContext:
    claim: LeaseClaim
    store: DurableWorkStore
    _lease_lost: threading.Event

    @property
    def lease_is_valid(self) -> bool:
        return not self._lease_lost.is_set()

    def checkpoint(
        self, checkpoint_type: str, payload: Mapping[str, Any]
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

    def invocation_dispatcher(self) -> InvocationDispatch:
        if _dispatcher_factory is None:
            raise RuntimeError("invocation dispatcher has not been installed")
        return _dispatcher_factory(
            store=self.store,
            claim=self.claim,
            lease_lost=self._lease_lost,
        )


__all__ = [
    "DispatchKnownNotSent",
    "DurableWorkStore",
    "InvocationKind",
    "InvocationRecord",
    "InvocationState",
    "LeaseClaim",
    "LeaseLostError",
    "OutcomeUnknownError",
    "RetryableWorkError",
    "TerminalWorkError",
    "WorkContext",
    "WorkDisposition",
    "WorkFinalizationMode",
    "WorkHandler",
    "WorkKind",
    "WorkResult",
    "WorkerError",
]
