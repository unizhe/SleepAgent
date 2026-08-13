# 本模块负责 PostgreSQL 持久化边界与完整性校验，不提供内存或 SQLite 旁路。
"""PostgreSQL connection-pool and explicit Unit-of-Work primitives.

This module is intentionally independent from psycopg at import time so unit
tests and replay-only tooling do not need the PostgreSQL extra installed.  A
production runtime creates :class:`PsycopgPoolProvider` and hands the resulting
``UnitOfWorkFactory`` to application services.  Repositories receive the
transaction-bound connection facade and therefore cannot commit or roll back
their caller's transaction.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable, Generic, Literal, Mapping, Protocol, TypeVar


DataMode = Literal["live", "replay"]
ProcessRole = Literal["api", "worker"]
ActorRole = Literal["elder", "family", "doctor"]
T = TypeVar("T")


class UnitOfWorkError(RuntimeError):
    """Base error for the PostgreSQL Unit-of-Work boundary."""


class UnitOfWorkStateError(UnitOfWorkError):
    """Raised when a Unit of Work is used outside its single transaction."""


class RepositoryTransactionControlError(UnitOfWorkError):
    """Raised when repository code tries to own the caller's transaction."""


class PoolContextLeakError(UnitOfWorkError):
    """Raised when request scope survives a pool checkout/reset boundary."""


class CursorLike(Protocol):
    rowcount: int

    def execute(self, query: str, params: Any = None) -> Any: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def close(self) -> None: ...


class ConnectionLike(Protocol):
    def cursor(self) -> CursorLike: ...

    def execute(self, query: str, params: Any = None) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class PoolLike(Protocol):
    def connection(self) -> AbstractContextManager[ConnectionLike]: ...

    def open(self, *, wait: bool = True, timeout: float = 30.0) -> None: ...

    def close(self) -> None: ...


_SCOPE_GUCS = (
    "sleepagent.namespace_id",
    "sleepagent.namespace_generation",
    "sleepagent.data_mode",
    "sleepagent.run_id",
    "sleepagent.arm_id",
    "sleepagent.subject_id",
    "sleepagent.actor_id",
    "sleepagent.actor_role",
    "sleepagent.service_principal_id",
    "sleepagent.process_role",
    "sleepagent.purpose",
    "sleepagent.authorization_epoch",
    "sleepagent.privacy_epoch",
    "sleepagent.retrieval_policy_epoch",
    "sleepagent.worker_instance",
)


@dataclass(frozen=True, slots=True)
class UowScope:
    """Exact tenant, identity, purpose and governance scope for one transaction."""

    namespace_id: str
    data_mode: DataMode
    process_role: ProcessRole
    purpose: str
    service_principal_id: str
    namespace_generation: int
    subject_id: str | None = None
    actor_id: str | None = None
    actor_role: ActorRole | None = None
    run_id: str | None = None
    arm_id: str | None = None
    authorization_epoch: int | None = None
    privacy_epoch: int | None = None
    retrieval_policy_epoch: int | None = None
    worker_instance: str | None = None

    def __post_init__(self) -> None:
        expected_prefix = f"{self.data_mode}:"
        if not self.namespace_id.startswith(expected_prefix) or (
            self.namespace_id == expected_prefix
        ):
            raise ValueError(
                f"{self.data_mode} namespace must have prefix {expected_prefix!r}"
            )
        for name in ("purpose", "service_principal_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if self.namespace_generation < 1:
            raise ValueError("namespace_generation must be positive")
        if self.data_mode == "live" and (self.run_id or self.arm_id):
            raise ValueError("live scope cannot carry replay run/arm identifiers")
        if self.data_mode == "replay" and not (self.run_id and self.arm_id):
            raise ValueError("replay scope requires run_id and arm_id")
        if self.process_role == "api" and self.worker_instance is not None:
            raise ValueError("API scope cannot claim a worker instance")
        if self.process_role == "api" and not self.actor_id:
            raise ValueError("API scope requires an authoritative actor_id")
        if self.process_role == "api" and self.actor_role is None:
            raise ValueError("API scope requires an authoritative actor_role")
        if self.process_role == "worker" and self.actor_id is not None:
            raise ValueError("worker scope cannot self-assert an actor_id")
        if self.process_role == "worker" and self.actor_role is not None:
            raise ValueError("worker scope cannot self-assert an actor_role")
        if self.subject_id is not None and any(
            value is None
            for value in (
                self.authorization_epoch,
                self.privacy_epoch,
                self.retrieval_policy_epoch,
            )
        ):
            raise ValueError("subject scope requires all governance epochs")
        for name in (
            "authorization_epoch",
            "privacy_epoch",
            "retrieval_policy_epoch",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")

    def guc_values(self) -> Mapping[str, str]:
        return {
            "sleepagent.namespace_id": self.namespace_id,
            "sleepagent.namespace_generation": str(self.namespace_generation),
            "sleepagent.data_mode": self.data_mode,
            "sleepagent.run_id": self.run_id or "",
            "sleepagent.arm_id": self.arm_id or "",
            "sleepagent.subject_id": self.subject_id or "",
            "sleepagent.actor_id": self.actor_id or "",
            "sleepagent.actor_role": self.actor_role or "",
            "sleepagent.service_principal_id": self.service_principal_id,
            "sleepagent.process_role": self.process_role,
            "sleepagent.purpose": self.purpose,
            "sleepagent.authorization_epoch": _optional_int_text(
                self.authorization_epoch
            ),
            "sleepagent.privacy_epoch": _optional_int_text(self.privacy_epoch),
            "sleepagent.retrieval_policy_epoch": _optional_int_text(
                self.retrieval_policy_epoch
            ),
            "sleepagent.worker_instance": self.worker_instance or "",
        }


@dataclass(frozen=True, slots=True)
class AuthorityResolutionScope:
    """Minimal context that can call only the authority resolver function."""

    data_mode: DataMode
    purpose: str
    service_principal_id: str
    actor_id: str

    def __post_init__(self) -> None:
        for name in ("purpose", "service_principal_id", "actor_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")

    def guc_values(self) -> Mapping[str, str]:
        return _empty_scope_gucs(
            data_mode=self.data_mode,
            process_role="api",
            purpose=self.purpose,
            service_principal_id=self.service_principal_id,
            actor_id=self.actor_id,
        )


@dataclass(frozen=True, slots=True)
class DemoControlScope:
    """Pre-authority context limited to SECURITY DEFINER demo functions."""

    service_principal_id: str
    purpose: Literal["demo_control"] = "demo_control"
    data_mode: Literal["replay"] = "replay"

    def __post_init__(self) -> None:
        if not self.service_principal_id.strip():
            raise ValueError("service_principal_id is required")

    def guc_values(self) -> Mapping[str, str]:
        return _empty_scope_gucs(
            data_mode="replay",
            process_role="api",
            purpose=self.purpose,
            service_principal_id=self.service_principal_id,
        )


@dataclass(frozen=True, slots=True)
class InternalControlScope:
    """Minimal API context limited to protected internal status functions."""

    data_mode: DataMode
    service_principal_id: str
    purpose: Literal["internal_status"] = "internal_status"

    def __post_init__(self) -> None:
        if not self.service_principal_id.strip():
            raise ValueError("service_principal_id is required")

    def guc_values(self) -> Mapping[str, str]:
        return _empty_scope_gucs(
            data_mode=self.data_mode,
            process_role="api",
            purpose=self.purpose,
            service_principal_id=self.service_principal_id,
        )


@dataclass(frozen=True, slots=True)
class WorkerClaimScope:
    """Minimal cross-namespace context for audited SECURITY DEFINER claims."""

    data_mode: DataMode
    purpose: str
    service_principal_id: str
    worker_instance: str

    def __post_init__(self) -> None:
        for name in ("purpose", "service_principal_id", "worker_instance"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")

    def guc_values(self) -> Mapping[str, str]:
        return _empty_scope_gucs(
            data_mode=self.data_mode,
            process_role="worker",
            purpose=self.purpose,
            service_principal_id=self.service_principal_id,
            worker_instance=self.worker_instance,
        )


class TransactionScope(Protocol):
    def guc_values(self) -> Mapping[str, str]: ...


def _empty_scope_gucs(
    *,
    data_mode: DataMode,
    process_role: ProcessRole,
    purpose: str,
    service_principal_id: str,
    actor_id: str = "",
    worker_instance: str = "",
) -> Mapping[str, str]:
    values = {name: "" for name in _SCOPE_GUCS}
    values.update(
        {
            "sleepagent.data_mode": data_mode,
            "sleepagent.actor_id": actor_id,
            "sleepagent.service_principal_id": service_principal_id,
            "sleepagent.process_role": process_role,
            "sleepagent.purpose": purpose,
            "sleepagent.worker_instance": worker_instance,
        }
    )
    return values


@dataclass(frozen=True, slots=True)
class PoolConfiguration:
    min_size: int = 1
    max_size: int = 10
    open_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.min_size < 0:
            raise ValueError("pool min_size must be non-negative")
        if self.max_size < 1 or self.max_size < self.min_size:
            raise ValueError("pool max_size must be >= max(1, min_size)")
        if self.open_timeout_seconds <= 0:
            raise ValueError("pool open timeout must be positive")


class PsycopgPoolProvider:
    """Small lifecycle wrapper around ``psycopg_pool.ConnectionPool``."""

    def __init__(self, pool: PoolLike, *, configuration: PoolConfiguration) -> None:
        self._pool = pool
        self.configuration = configuration
        self._opened = False

    @classmethod
    def from_dsn(
        cls,
        dsn: str,
        *,
        configuration: PoolConfiguration | None = None,
        application_name: str = "sleepagent",
    ) -> "PsycopgPoolProvider":
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        configuration = configuration or PoolConfiguration()
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - depends on optional extra.
            raise RuntimeError(
                "psycopg_pool is required for PostgreSQL runtime persistence"
            ) from exc
        pool: Any = ConnectionPool(
            conninfo=dsn,
            min_size=configuration.min_size,
            max_size=configuration.max_size,
            open=False,
            kwargs={
                "autocommit": False,
                "application_name": application_name,
            },
            reset=reset_pooled_connection,
        )
        return cls(pool, configuration=configuration)

    def open(self) -> None:
        if self._opened:
            return
        self._pool.open(
            wait=True,
            timeout=self.configuration.open_timeout_seconds,
        )
        self._opened = True

    def close(self) -> None:
        if not self._opened:
            return
        self._pool.close()
        self._opened = False

    def connection(self) -> AbstractContextManager[ConnectionLike]:
        if not self._opened:
            raise UnitOfWorkStateError("PostgreSQL pool is not open")
        return self._pool.connection()


class TransactionBoundConnection:
    """Connection facade that deliberately withholds transaction ownership."""

    __slots__ = ("_connection", "_active")

    def __init__(
        self,
        connection: ConnectionLike,
        active: Callable[[], bool],
    ) -> None:
        self._connection = connection
        self._active = active

    def cursor(self) -> CursorLike:
        self._require_active()
        return self._connection.cursor()

    def execute(self, query: str, params: Any = None) -> Any:
        self._require_active()
        return self._connection.execute(query, params)

    def commit(self) -> None:
        raise RepositoryTransactionControlError(
            "repository code cannot commit a Unit-of-Work transaction"
        )

    def rollback(self) -> None:
        raise RepositoryTransactionControlError(
            "repository code cannot roll back a Unit-of-Work transaction"
        )

    def _require_active(self) -> None:
        if not self._active():
            raise UnitOfWorkStateError("transaction-bound connection is not active")


class PostgresUnitOfWork(AbstractContextManager["PostgresUnitOfWork"]):
    """One pool checkout and one explicit PostgreSQL transaction."""

    def __init__(
        self,
        provider: PsycopgPoolProvider | Any,
        scope: TransactionScope,
        *,
        statement_timeout_ms: int = 15_000,
        lock_timeout_ms: int = 2_000,
        idle_in_transaction_timeout_ms: int = 15_000,
    ) -> None:
        for name, value in (
            ("statement_timeout_ms", statement_timeout_ms),
            ("lock_timeout_ms", lock_timeout_ms),
            ("idle_in_transaction_timeout_ms", idle_in_transaction_timeout_ms),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        self.provider = provider
        self.scope = scope
        self.statement_timeout_ms = statement_timeout_ms
        self.lock_timeout_ms = lock_timeout_ms
        self.idle_in_transaction_timeout_ms = idle_in_transaction_timeout_ms
        self._checkout: AbstractContextManager[ConnectionLike] | None = None
        self._raw_connection: ConnectionLike | None = None
        self._connection: TransactionBoundConnection | None = None
        self._active = False
        self._completed = False
        self._committed = False

    @property
    def connection(self) -> TransactionBoundConnection:
        if self._connection is None or not self._active:
            raise UnitOfWorkStateError("Unit of Work has not entered its transaction")
        return self._connection

    @property
    def committed(self) -> bool:
        return self._committed

    def __enter__(self) -> "PostgresUnitOfWork":
        if self._active or self._checkout is not None or self._completed:
            raise UnitOfWorkStateError("Unit of Work is single-use")
        checkout = self.provider.connection()
        raw = checkout.__enter__()
        self._checkout = checkout
        self._raw_connection = raw
        try:
            # A rollback is cheap and guarantees no caller inherits a prior
            # transaction or transaction-local RLS setting.
            raw.rollback()
            cursor = raw.cursor()
            try:
                cursor.execute("BEGIN")
                cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                for name, value in self.scope.guc_values().items():
                    cursor.execute("SELECT set_config(%s, %s, true)", (name, value))
                for name, value in (
                    ("statement_timeout", f"{self.statement_timeout_ms}ms"),
                    ("lock_timeout", f"{self.lock_timeout_ms}ms"),
                    (
                        "idle_in_transaction_session_timeout",
                        f"{self.idle_in_transaction_timeout_ms}ms",
                    ),
                ):
                    cursor.execute("SELECT set_config(%s, %s, true)", (name, value))
            finally:
                cursor.close()
        except Exception:
            raw.rollback()
            checkout.__exit__(None, None, None)
            self._checkout = None
            self._raw_connection = None
            raise
        self._active = True
        self._connection = TransactionBoundConnection(raw, lambda: self._active)
        return self

    def repository(
        self,
        factory: Callable[[TransactionBoundConnection], T],
    ) -> T:
        """Build a repository bound to this transaction-only connection."""

        return factory(self.connection)

    def commit(self) -> None:
        self._require_uncompleted()
        assert self._raw_connection is not None
        self._raw_connection.commit()
        self._completed = True
        self._committed = True
        self._active = False

    def rollback(self) -> None:
        self._require_uncompleted()
        assert self._raw_connection is not None
        self._raw_connection.rollback()
        self._completed = True
        self._active = False

    def __exit__(
        self,
        exc_type: Any,
        exc: Any,
        traceback: Any,
    ) -> Literal[False]:
        raw = self._raw_connection
        checkout = self._checkout
        try:
            if raw is not None and not self._completed:
                raw.rollback()
                self._completed = True
                self._active = False
            if raw is not None:
                reset_pooled_connection(raw)
        finally:
            self._active = False
            self._connection = None
            self._raw_connection = None
            self._checkout = None
            if checkout is not None:
                checkout.__exit__(exc_type, exc, traceback)
        return False

    def _require_uncompleted(self) -> None:
        if not self._active or self._completed:
            raise UnitOfWorkStateError("Unit of Work transaction is not active")


class UnitOfWorkFactory(Generic[T]):
    """Factory carrying pool ownership and bounded transaction timeouts."""

    def __init__(
        self,
        provider: PsycopgPoolProvider | Any,
        *,
        statement_timeout_ms: int = 15_000,
        lock_timeout_ms: int = 2_000,
        idle_in_transaction_timeout_ms: int = 15_000,
    ) -> None:
        self.provider = provider
        self.statement_timeout_ms = statement_timeout_ms
        self.lock_timeout_ms = lock_timeout_ms
        self.idle_in_transaction_timeout_ms = idle_in_transaction_timeout_ms

    def begin(self, scope: TransactionScope) -> PostgresUnitOfWork:
        return PostgresUnitOfWork(
            self.provider,
            scope,
            statement_timeout_ms=self.statement_timeout_ms,
            lock_timeout_ms=self.lock_timeout_ms,
            idle_in_transaction_timeout_ms=self.idle_in_transaction_timeout_ms,
        )


def reset_pooled_connection(connection: ConnectionLike) -> None:
    """Clear transaction and custom RLS context before returning a connection."""

    connection.rollback()
    cursor = connection.cursor()
    try:
        cursor.execute("RESET ALL")
    finally:
        cursor.close()
    connection.commit()

    cursor = connection.cursor()
    try:
        placeholders = ", ".join(
            "current_setting(%s, true)" for _ in _SCOPE_GUCS
        )
        cursor.execute(f"SELECT {placeholders}", _SCOPE_GUCS)
        row = cursor.fetchone()
    finally:
        cursor.close()
        connection.rollback()
    if row is not None and any(value not in (None, "") for value in row):
        raise PoolContextLeakError("PostgreSQL pool connection retained RLS context")


def _optional_int_text(value: int | None) -> str:
    return "" if value is None else str(value)


__all__ = [
    "ActorRole",
    "AuthorityResolutionScope",
    "DemoControlScope",
    "InternalControlScope",
    "PoolConfiguration",
    "PoolContextLeakError",
    "PostgresUnitOfWork",
    "PsycopgPoolProvider",
    "RepositoryTransactionControlError",
    "TransactionBoundConnection",
    "TransactionScope",
    "UnitOfWorkError",
    "UnitOfWorkFactory",
    "UnitOfWorkStateError",
    "UowScope",
    "WorkerClaimScope",
    "reset_pooled_connection",
]
