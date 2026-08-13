from __future__ import annotations

# Tool 调度、缓存与串行化协调共置。

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
import inspect
from threading import Lock, RLock
from typing import Any, Callable, Iterator, ParamSpec, Protocol, TypeVar, cast
from weakref import ReferenceType, ref

from sleepagent.runtime.contracts import (
    AgentId,
    InvocationOutcome,
    ToolEffect,
    ToolReceipt,
    TrustLabel,
    TrustedContextItem,
)
from sleepagent.runtime.registry import (
    RUNTIME_INTERACTION_TOOLS,
    TOOL_DEFINITIONS,
)
from sleepagent.runtime.contracts import (
    ProductToolExecutionContext,
    ProductToolExecutorPort,
    ProductToolResult,
)
from sleepagent.runtime.tooling import (
    canonical_runtime_interaction_idempotency_key,
    canonical_tool_input_hash,
    canonical_tool_invocation_id,
    context_item_from_tool_receipt,
)


TOOL_EXECUTION_COORDINATOR_VERSION = (
    "sleepagent-product-tool-execution-coordinator.v1"
)


class _ToolCallRuntime(Protocol):
    def record_tool_call(self) -> None: ...


@dataclass(slots=True)
class _EpisodeExecutionLease:
    lock: RLock
    references: int = 0


@dataclass(slots=True)
class _ExecutorIdentityReference:
    identity: int
    weak: ReferenceType[ProductToolExecutorPort] | None
    strong: ProductToolExecutorPort | None

    @classmethod
    def create(
        cls,
        executor: ProductToolExecutorPort,
    ) -> _ExecutorIdentityReference:
        try:
            return cls(identity=id(executor), weak=ref(executor), strong=None)
        except TypeError:
            return cls(identity=id(executor), weak=None, strong=executor)

    def resolve(self) -> ProductToolExecutorPort | None:
        return self.weak() if self.weak is not None else self.strong


_PROCESS_EPISODE_LEASES: dict[str, _EpisodeExecutionLease] = {}
_PROCESS_EPISODE_LEASES_LOCK = Lock()
_PROCESS_EPISODE_EXECUTORS: dict[
    str,
    dict[int, _ExecutorIdentityReference],
] = {}
_PROCESS_EPISODE_EXECUTORS_LOCK = Lock()


class ToolExecutionCoordinator:
    """Typed Tool boundary and per-Episode execution serialization.

    The coordinator does not select capabilities or interpret policy. It only
    records an already-selected Tool call when requested, delegates execution
    to the injected typed port, adapts trusted context, and owns transient
    execution serialization/cache release boundaries.
    """

    def __init__(self, executor: ProductToolExecutorPort) -> None:
        self.executor = executor
        # A process can host more than one composition bundle (for example an
        # API adapter and a worker).  They must still serialize the same
        # Episode, so the short-lived lease registry is process-scoped.
        self._episode_leases = _PROCESS_EPISODE_LEASES
        self._episode_leases_lock = _PROCESS_EPISODE_LEASES_LOCK

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ProductToolExecutionContext,
        runtime: _ToolCallRuntime | None = None,
        record_call: bool = False,
    ) -> ProductToolResult:
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("tool_name is required")
        if tool_name not in TOOL_DEFINITIONS:
            raise ValueError(f"unknown Product Tool: {tool_name}")
        if not isinstance(arguments, dict):
            raise TypeError("Tool arguments must be a dict")
        if not isinstance(context, ProductToolExecutionContext):
            raise TypeError(
                "Tool execution requires ProductToolExecutionContext"
            )
        if record_call:
            if runtime is None:
                raise ValueError(
                    "record_call=True requires a Product Episode runtime"
                )
            runtime.record_tool_call()
        canonical_arguments = deepcopy(arguments)
        definition = TOOL_DEFINITIONS[tool_name]
        expected_input_hash = canonical_tool_input_hash(
            tool_name,
            canonical_arguments,
        )
        expected_tool_version = f"{tool_name}.{definition.version}"
        expected_invocation_id = canonical_tool_invocation_id(
            tool_name,
            expected_input_hash,
        )
        validate_idempotency_key = False
        expected_idempotency_key: str | None = None
        if tool_name in RUNTIME_INTERACTION_TOOLS:
            if context.episode_id is None:
                raise ValueError(
                    "runtime interaction Tool requires an Episode binding"
                )
            expected_idempotency_key = (
                canonical_runtime_interaction_idempotency_key(
                    tool_name,
                    canonical_arguments,
                    episode_id=context.episode_id,
                    fact_snapshot_hash=context.fact_snapshot.fact_snapshot_hash,
                )
            )
            validate_idempotency_key = True
        elif definition.effect is ToolEffect.READ_ONLY:
            validate_idempotency_key = True

        delegated_arguments = deepcopy(canonical_arguments)
        delegated_context = context.model_copy(deep=True)
        if context.episode_id is not None:
            with _PROCESS_EPISODE_EXECUTORS_LOCK:
                executors = _PROCESS_EPISODE_EXECUTORS.setdefault(
                    context.episode_id,
                    {},
                )
                executor_id = id(self.executor)
                current = executors.get(executor_id)
                if current is None or current.resolve() is not self.executor:
                    executors[executor_id] = _ExecutorIdentityReference.create(
                        self.executor
                    )
        raw_result = self.executor.execute(
            tool_name,
            delegated_arguments,
            context=delegated_context,
        )
        try:
            result = ProductToolResult.model_validate(
                raw_result.model_dump(mode="python")
                if isinstance(raw_result, ProductToolResult)
                else raw_result
            )
        except Exception as exc:
            raise ValueError("Tool executor returned an invalid typed result") from exc
        receipt = result.receipt
        expected_caller = (
            context.caller.value
            if isinstance(context.caller, AgentId)
            else context.caller
        )
        if (
            receipt.tool_name != tool_name
            or receipt.caller != expected_caller
            or receipt.fact_snapshot_id
            != context.fact_snapshot.fact_snapshot_id
            or receipt.fact_snapshot_hash
            != context.fact_snapshot.fact_snapshot_hash
            or receipt.effect != definition.effect
        ):
            raise ValueError("Tool receipt does not match its invocation binding")
        if (
            receipt.tool_version != expected_tool_version
            or receipt.input_hash != expected_input_hash
            or receipt.tool_invocation_id != expected_invocation_id
            or (
                validate_idempotency_key
                and receipt.idempotency_key != expected_idempotency_key
            )
        ):
            raise ValueError(
                "Tool receipt provenance does not match canonical invocation"
            )
        if (
            receipt.outcome != InvocationOutcome.SUCCEEDED
            and result.context_item is not None
        ):
            raise ValueError("failed Tool result cannot expose trusted context")
        return ProductToolResult(
            receipt=receipt,
            context_item=(
                self.context_item_for_receipt(receipt)
                if receipt.outcome == InvocationOutcome.SUCCEEDED
                else None
            ),
        )

    @staticmethod
    def context_item_for_receipt(receipt: ToolReceipt) -> TrustedContextItem:
        if receipt.tool_name == "cold_start.evaluate":
            return TrustedContextItem(
                key="runtime:cold_start_readiness",
                trust_label=TrustLabel.CANONICAL_FACT,
                value=receipt.output,
                source_refs=tuple(
                    [receipt.tool_invocation_id, *receipt.source_refs]
                ),
            )
        if receipt.tool_name.startswith(("profile.", "questionnaire.")):
            return context_item_from_tool_receipt(receipt)
        if receipt.tool_name == "memory.read":
            items = receipt.output.get("items", [])
            label = (
                TrustLabel.EPISODIC_HINT_UNTRUSTED
                if any(
                    item.get("item_kind") == "episode_digest"
                    for item in items
                    if isinstance(item, dict)
                )
                else TrustLabel.USER_MEMORY_UNTRUSTED_DATA
            )
            return TrustedContextItem(
                key="tool:memory.read",
                trust_label=label,
                value=receipt.output,
                source_refs=tuple(receipt.source_refs),
            )
        return TrustedContextItem(
            key=f"tool:{receipt.tool_name}",
            trust_label=TrustLabel.TOOL_OUTPUT_UNTRUSTED,
            value=receipt.output,
            source_refs=tuple(
                [receipt.tool_invocation_id, *receipt.source_refs]
            ),
        )

    def release_episode(self, episode_id: str) -> None:
        if not episode_id:
            raise ValueError("episode_id is required")
        with _PROCESS_EPISODE_EXECUTORS_LOCK:
            references = list(
                _PROCESS_EPISODE_EXECUTORS.pop(episode_id, {}).values()
            )
        registered = [
            executor
            for item in references
            if (executor := item.resolve()) is not None
        ]
        if not any(item is self.executor for item in registered):
            registered.append(self.executor)
        first_error: Exception | None = None
        for executor in registered:
            try:
                executor.release_episode(episode_id)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise RuntimeError(
                "one or more Product Tool caches failed Episode cleanup"
            ) from first_error

    @contextmanager
    def episode_execution_lease(self, episode_id: str) -> Iterator[None]:
        """Serialize one Episode while allowing same-thread re-entry.

        References include both holders and waiters, so the registry entry is
        removed only when no execution can still be using its lock.
        """

        if not episode_id:
            raise ValueError("episode_id is required")
        with self._episode_leases_lock:
            lease = self._episode_leases.get(episode_id)
            if lease is None:
                lease = _EpisodeExecutionLease(lock=RLock())
                self._episode_leases[episode_id] = lease
            lease.references += 1

        acquired = False
        try:
            lease.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                lease.lock.release()
            with self._episode_leases_lock:
                lease.references -= 1
                if (
                    lease.references == 0
                    and self._episode_leases.get(episode_id) is lease
                ):
                    del self._episode_leases[episode_id]


_P = ParamSpec("_P")
_R = TypeVar("_R")


def serialized_episode_execution(
    function: Callable[_P, _R],
) -> Callable[_P, _R]:
    """Serialize a Runner command by its request-bound Episode identity."""

    signature = inspect.signature(function)

    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        bound = signature.bind(*args, **kwargs)
        owner = bound.arguments.get("self")
        if owner is None:
            raise TypeError(
                "serialized_episode_execution requires an instance method"
            )
        coordinator = getattr(owner, "tool_execution_coordinator", None)
        if not isinstance(coordinator, ToolExecutionCoordinator):
            raise TypeError(
                "serialized Episode execution requires "
                "self.tool_execution_coordinator"
            )

        request = bound.arguments.get("request")
        if request is None:
            command = bound.arguments.get("command")
            request = getattr(command, "request", None)
        episode_id = getattr(request, "episode_id", None)
        if not isinstance(episode_id, str) or not episode_id:
            raise ValueError(
                "serialized Episode execution requires a request episode_id"
            )

        with coordinator.episode_execution_lease(episode_id):
            return function(*args, **kwargs)

    return cast(Callable[_P, _R], wrapped)


__all__ = [
    "TOOL_EXECUTION_COORDINATOR_VERSION",
    "ToolExecutionCoordinator",
    "serialized_episode_execution",
]
