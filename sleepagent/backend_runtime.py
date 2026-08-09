"""Single capability-scoped composition root for API and worker processes."""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Awaitable, Callable, Mapping, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from sleepagent.backend_settings import ProcessRole, SleepBackendSettings


class PoolLifecycle(Protocol):
    def open(self) -> object: ...

    def close(self) -> object: ...


class DatabaseAttestor(Protocol):
    def __call__(self) -> "DatabaseAttestation": ...


class DatabaseAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    database_identity: str
    database_role: str
    schema_version: int
    migrations_clean: bool


class RuntimeDependencyManifest(BaseModel):
    """Non-secret, reproducible description of the active dependency graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_version: str = "sleep_backend_dependencies.v1"
    runtime_id: str
    process_role: str
    store: str = "postgresql"
    database_identity: str
    database_role: str
    schema_version: int | None
    supported_schema_min: int
    supported_schema_max: int
    data_mode: str
    enabled_surfaces: tuple[str, ...]
    enabled_queues: tuple[str, ...]
    enabled_handlers: tuple[str, ...]
    model_mode: str
    provider_mode: str
    control_clock: str = "system_utc"
    lease_clock: str = "postgresql_server_time"
    config_sha256: str
    manifest_sha256: str


@dataclass(frozen=True)
class RuntimeServices:
    """Transport-facing services; no connection-bound repositories live here."""

    product: object | None = None
    demo: object | None = None
    public_v1_runtime_provider: Callable[[], object] | None = None
    internal_status: object | None = None


_ACTIVE_LOCK = threading.RLock()
_ACTIVE_RUNTIME_ID: str | None = None


class SleepBackendRuntime:
    """One runtime per process, owning only pools and factories.

    A runtime never starts background work.  The API lifespan invokes only
    :meth:`start`/:meth:`close`; worker loops are owned by ``worker_runtime``.
    """

    def __init__(
        self,
        settings: SleepBackendSettings,
        *,
        pool: PoolLifecycle,
        uow_factory: object,
        attestor: DatabaseAttestor,
        services: RuntimeServices | None = None,
        worker_handlers: Mapping[str, object] | None = None,
    ) -> None:
        handlers = dict(worker_handlers or {})
        if settings.process_role == ProcessRole.API and handlers:
            raise ValueError("API runtime cannot install worker handlers")
        if (
            settings.process_role != ProcessRole.API
            and services is not None
            and services != RuntimeServices()
        ):
            raise ValueError("non-API runtime cannot install transport services")
        if settings.process_role == ProcessRole.WORKER:
            missing = set(settings.worker_queues) - set(handlers)
            if missing:
                raise ValueError(
                    "worker handlers are missing for queues: "
                    + ", ".join(sorted(missing))
                )
        elif handlers:
            raise ValueError("only worker runtime may install handlers")
        self.settings = settings
        self.pool = pool
        self.uow_factory = uow_factory
        self.attestor = attestor
        self.services = services or RuntimeServices()
        self.worker_handlers = handlers
        self.runtime_id = str(uuid4())
        self._started = False
        self._attestation: DatabaseAttestation | None = None

    @property
    def started(self) -> bool:
        return self._started

    @property
    def attestation(self) -> DatabaseAttestation | None:
        return self._attestation

    async def start(self) -> None:
        global _ACTIVE_RUNTIME_ID
        with _ACTIVE_LOCK:
            if self._started:
                return
            if _ACTIVE_RUNTIME_ID is not None:
                raise RuntimeError(
                    "another SleepBackendRuntime is already active in this process"
                )
            _ACTIVE_RUNTIME_ID = self.runtime_id
        try:
            await _maybe_await(self.pool.open())
            attestation = self.attestor()
            if inspect.isawaitable(attestation):
                attestation = await attestation
            self._validate_attestation(attestation)
            self._attestation = attestation
            self._started = True
        except BaseException:
            try:
                await _maybe_await(self.pool.close())
            finally:
                with _ACTIVE_LOCK:
                    if _ACTIVE_RUNTIME_ID == self.runtime_id:
                        _ACTIVE_RUNTIME_ID = None
            raise

    async def close(self) -> None:
        global _ACTIVE_RUNTIME_ID
        if not self._started:
            with _ACTIVE_LOCK:
                if _ACTIVE_RUNTIME_ID == self.runtime_id:
                    _ACTIVE_RUNTIME_ID = None
            return
        try:
            await _maybe_await(self.pool.close())
        finally:
            self._started = False
            self._attestation = None
            with _ACTIVE_LOCK:
                if _ACTIVE_RUNTIME_ID == self.runtime_id:
                    _ACTIVE_RUNTIME_ID = None

    def dependency_manifest(self) -> RuntimeDependencyManifest:
        attestation = self._attestation
        unsigned = {
            "runtime_id": self.runtime_id,
            "process_role": self.settings.process_role.value,
            "database_identity": (
                self.settings.database_identity
                if attestation is None
                else attestation.database_identity
            ),
            "database_role": (
                self.settings.database_role
                if attestation is None
                else attestation.database_role
            ),
            "schema_version": (
                None if attestation is None else attestation.schema_version
            ),
            "supported_schema_min": self.settings.supported_schema_min,
            "supported_schema_max": self.settings.supported_schema_max,
            "data_mode": self.settings.data_mode.value,
            "enabled_surfaces": tuple(
                sorted(value.value for value in self.settings.enabled_surfaces)
            ),
            "enabled_queues": tuple(sorted(self.settings.worker_queues)),
            "enabled_handlers": tuple(sorted(self.worker_handlers)),
            "model_mode": self.settings.model_mode.value,
            "provider_mode": self.settings.provider_mode.value,
            "config_sha256": self.settings.public_fingerprint(),
        }
        digest = sha256(
            repr(sorted(unsigned.items())).encode("utf-8")
        ).hexdigest()
        return RuntimeDependencyManifest(
            **unsigned,
            manifest_sha256=digest,
        )

    def readiness(self) -> dict[str, object]:
        attestation = self._attestation
        ready = self._started and attestation is not None
        return {
            "ready": ready,
            "process_role": self.settings.process_role.value,
            "database": "ready" if ready else "unavailable",
            "schema_version": (
                None if attestation is None else attestation.schema_version
            ),
            "config_sha256": self.settings.public_fingerprint(),
        }

    def _validate_attestation(self, value: DatabaseAttestation) -> None:
        if value.database_identity != self.settings.database_identity:
            raise RuntimeError("database identity attestation mismatch")
        if value.database_role != self.settings.database_role:
            raise RuntimeError("database role attestation mismatch")
        if not value.migrations_clean:
            raise RuntimeError("database migration ledger is not clean")
        if not (
            self.settings.supported_schema_min
            <= value.schema_version
            <= self.settings.supported_schema_max
        ):
            raise RuntimeError("database schema version is unsupported")


def build_backend_runtime(
    settings: SleepBackendSettings,
    *,
    services: RuntimeServices | None = None,
    worker_handlers: Mapping[str, object] | None = None,
) -> SleepBackendRuntime:
    try:
        from sleepagent.radar_agent.persistence.uow import (
            PoolConfiguration,
            PsycopgPoolProvider,
            UnitOfWorkFactory,
        )
    except ImportError as exc:  # pragma: no cover - indicates broken package
        raise RuntimeError("PostgreSQL UnitOfWorkFactory is unavailable") from exc
    pool = PsycopgPoolProvider.from_dsn(
        settings.database_dsn.get_secret_value(),
        configuration=PoolConfiguration(
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            open_timeout_seconds=settings.pool_timeout_seconds,
        ),
        application_name=f"sleepagent-{settings.process_role.value}",
    )
    uow_factory: UnitOfWorkFactory[Any] = UnitOfWorkFactory(
        pool,
        lock_timeout_ms=settings.lock_timeout_ms,
        statement_timeout_ms=settings.statement_timeout_ms,
        idle_in_transaction_timeout_ms=settings.idle_transaction_timeout_ms,
    )
    if settings.process_role == ProcessRole.API and services is None:
        from sleepagent.backend_services import build_api_runtime_services

        services = build_api_runtime_services(settings, uow_factory=uow_factory)
    return SleepBackendRuntime(
        settings,
        pool=pool,
        uow_factory=uow_factory,
        attestor=lambda: _attest_postgres(pool),
        services=services,
        worker_handlers=worker_handlers,
    )


async def _maybe_await(value: object) -> None:
    if inspect.isawaitable(value):
        await value


def _attest_postgres(provider: object) -> DatabaseAttestation:
    with provider.connection() as connection:  # type: ignore[attr-defined]
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            database_identity, database_role = cursor.fetchone()
            cursor.execute(
                """
                SELECT COALESCE(MAX(version), 0),
                       COALESCE(bool_and(
                         status IN ('applied', 'legacy_attested')
                         AND finished_at IS NOT NULL
                       ), false)
                FROM radar_agent_schema_migrations_v2
                """
            )
            schema_version, migrations_clean = cursor.fetchone()
    return DatabaseAttestation(
        database_identity=str(database_identity),
        database_role=str(database_role),
        schema_version=int(schema_version),
        migrations_clean=bool(migrations_clean),
    )


def reset_active_runtime_for_tests() -> None:
    global _ACTIVE_RUNTIME_ID
    with _ACTIVE_LOCK:
        _ACTIVE_RUNTIME_ID = None


__all__ = [
    "DatabaseAttestation",
    "RuntimeDependencyManifest",
    "RuntimeServices",
    "SleepBackendRuntime",
    "build_backend_runtime",
    "reset_active_runtime_for_tests",
]
