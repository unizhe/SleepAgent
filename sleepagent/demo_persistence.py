"""Durable PostgreSQL-backed Demo control plane.

HTTP reservation is intentionally short: validate a packaged allowlist entry,
reserve one root/journey transaction, and return.  Fixture generation, ingress,
normalization, Product work, and verification remain Worker capabilities.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from sleepagent.backend_settings import SleepBackendSettings
from sleepagent.demo_api import (
    DemoAcceptedResponse,
    DemoAdvanceRequest,
    DemoApiError,
    DemoOperationResponse,
    DemoResetRequest,
    DemoSeedRequest,
    DemoTraceEntry,
    DemoTraceResponse,
    ScenarioClockResponse,
)
from sleepagent.persistence.uow import DemoControlScope, UnitOfWorkFactory
from sleepagent.simulation.seed_registry import (
    ReplaySeedDefinition,
    ReplaySeedRegistry,
    ReplaySeedRegistryError,
    load_replay_seed_registry,
    verify_packaged_seed,
)
from sleepagent.sleep_domain.episode_v2 import UUID7Generator


UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class DemoReservation:
    operation_id: str
    journey_id: str
    namespace_id: str
    generation: int
    run_id: str
    arm_id: str
    subject_id: str
    phase: str
    reused: bool


@dataclass(frozen=True, slots=True)
class DemoAdvanceReservation:
    operation_id: str
    generation: int
    reused: bool


@dataclass(frozen=True, slots=True)
class DemoResetReservation:
    operation_id: str
    generation: int
    reused: bool


@dataclass(frozen=True, slots=True)
class StoredDemoOperation:
    operation_id: str
    generation: int
    phase: str
    result: dict[str, Any] | None
    error_code: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredDemoClock:
    scenario_time: datetime
    generation: int


class DemoStore(Protocol):
    def reserve_seed(
        self,
        *,
        seed: ReplaySeedDefinition,
        caller_idempotency_key: str,
        request_sha256: str,
        batch_size: int,
        root_operation_id: str,
        journey_id: str,
        command_receipt_id: str,
        event_id: str,
    ) -> DemoReservation: ...

    def reserve_advance(
        self,
        *,
        caller_idempotency_key: str,
        request_sha256: str,
        seconds: int,
        operation_id: str,
        command_receipt_id: str,
        event_id: str,
        audit_id: str,
    ) -> DemoAdvanceReservation: ...

    def reserve_reset(
        self,
        *,
        caller_idempotency_key: str,
        request_sha256: str,
        operation_id: str,
        reset_id: str,
        command_receipt_id: str,
        event_id: str,
        audit_id: str,
    ) -> DemoResetReservation: ...

    def get_operation(self, operation_id: str) -> StoredDemoOperation: ...

    def get_clock(self) -> StoredDemoClock: ...

    def read_trace(
        self,
        *,
        operation_id: str,
        after_sequence: int,
        limit: int,
    ) -> tuple[DemoTraceEntry, ...]: ...


class PostgresDemoStore:
    def __init__(
        self,
        settings: SleepBackendSettings,
        uow_factory: UnitOfWorkFactory[Any],
    ) -> None:
        self.settings = settings
        self.uow_factory = uow_factory

    def _scope(self) -> DemoControlScope:
        return DemoControlScope(
            service_principal_id=self.settings.service_principal_id,
        )

    def reserve_seed(
        self,
        *,
        seed: ReplaySeedDefinition,
        caller_idempotency_key: str,
        request_sha256: str,
        batch_size: int,
        root_operation_id: str,
        journey_id: str,
        command_receipt_id: str,
        event_id: str,
    ) -> DemoReservation:
        with self.uow_factory.begin(self._scope()) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT * FROM public.sleepagent_reserve_demo_journey("
                    "%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        seed.seed_id,
                        caller_idempotency_key,
                        request_sha256,
                        batch_size,
                        root_operation_id,
                        journey_id,
                        command_receipt_id,
                        event_id,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None:
                raise RuntimeError("demo reservation returned no durable root")
            uow.commit()
        return DemoReservation(
            operation_id=str(row[0]),
            journey_id=str(row[1]),
            namespace_id=str(row[2]),
            generation=int(row[3]),
            run_id=str(row[4]),
            arm_id=str(row[5]),
            subject_id=str(row[6]),
            phase=str(row[7]),
            reused=bool(row[8]),
        )

    def get_operation(self, operation_id: str) -> StoredDemoOperation:
        with self.uow_factory.begin(self._scope()) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT * FROM public.sleepagent_get_demo_operation(%s)",
                    (operation_id,),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None:
                raise LookupError("operation_not_found")
            uow.commit()
        return StoredDemoOperation(
            operation_id=str(row[0]),
            generation=int(row[1]),
            phase=str(row[2]),
            result=_json_object(row[3]),
            error_code=None if row[4] is None else str(row[4]),
            updated_at=row[5],
        )

    def reserve_advance(
        self,
        *,
        caller_idempotency_key: str,
        request_sha256: str,
        seconds: int,
        operation_id: str,
        command_receipt_id: str,
        event_id: str,
        audit_id: str,
    ) -> DemoAdvanceReservation:
        with self.uow_factory.begin(self._scope()) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT * FROM public.sleepagent_reserve_demo_advance("
                    "%s, %s, %s, %s, %s, %s, %s)",
                    (
                        caller_idempotency_key,
                        request_sha256,
                        seconds,
                        operation_id,
                        command_receipt_id,
                        event_id,
                        audit_id,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None:
                raise RuntimeError("demo advance reservation returned no Operation")
            uow.commit()
        return DemoAdvanceReservation(
            operation_id=str(row[0]),
            generation=int(row[1]),
            reused=bool(row[2]),
        )

    def get_clock(self) -> StoredDemoClock:
        with self.uow_factory.begin(self._scope()) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute("SELECT * FROM public.sleepagent_read_demo_clock()")
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None:
                raise LookupError("operation_not_found")
            uow.commit()
        return StoredDemoClock(scenario_time=row[0], generation=int(row[1]))

    def reserve_reset(
        self,
        *,
        caller_idempotency_key: str,
        request_sha256: str,
        operation_id: str,
        reset_id: str,
        command_receipt_id: str,
        event_id: str,
        audit_id: str,
    ) -> DemoResetReservation:
        with self.uow_factory.begin(self._scope()) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT * FROM public.sleepagent_reserve_demo_reset("
                    "%s, %s, %s, %s, %s, %s, %s)",
                    (
                        caller_idempotency_key,
                        request_sha256,
                        operation_id,
                        reset_id,
                        command_receipt_id,
                        event_id,
                        audit_id,
                    ),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None:
                raise RuntimeError("demo reset reservation returned no Operation")
            uow.commit()
        return DemoResetReservation(
            operation_id=str(row[0]),
            generation=int(row[1]),
            reused=bool(row[2]),
        )

    def read_trace(
        self,
        *,
        operation_id: str,
        after_sequence: int,
        limit: int,
    ) -> tuple[DemoTraceEntry, ...]:
        with self.uow_factory.begin(self._scope()) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT * FROM public.sleepagent_read_demo_trace(%s, %s, %s)",
                    (operation_id, after_sequence, limit),
                )
                rows = cursor.fetchall()
            finally:
                cursor.close()
            uow.commit()
        return tuple(
            DemoTraceEntry(
                sequence=int(row[0]),
                event_type=str(row[1]),
                operation_id=None if row[2] is None else str(row[2]),
                state=str(row[3]),
                correlation_id=None if row[4] is None else str(row[4]),
                occurred_at=row[5],
                root_operation_id=str(row[6]),
                night_episode_revision_id=(
                    None if row[7] is None else str(row[7])
                ),
                fast_path_operation_id=None if row[8] is None else str(row[8]),
                product_operation_id=None if row[9] is None else str(row[9]),
                analysis_revision_id=None if row[10] is None else str(row[10]),
            )
            for row in rows
        )


class DurableDemoController:
    def __init__(
        self,
        store: DemoStore,
        *,
        registry: ReplaySeedRegistry | None = None,
        id_generator: Callable[[datetime | None], str] | None = None,
        now_factory: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self.store = store
        self.registry = registry or load_replay_seed_registry()
        self.id_generator = id_generator or UUID7Generator()
        self.now_factory = now_factory

    def seed(
        self,
        *,
        request: DemoSeedRequest,
        idempotency_key: str,
    ) -> DemoAcceptedResponse:
        try:
            seed = self.registry.lookup(
                request.artifact_family,
                request.scenario_id,
            )
            verify_packaged_seed(seed)
        except ReplaySeedRegistryError as exc:
            status = 404 if "unknown" in str(exc) else 422
            code = "scenario_not_found" if status == 404 else "scenario_contract_invalid"
            raise _demo_error(status, code, str(exc)) from exc
        request_sha256 = hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        now = self.now_factory()
        try:
            reservation = self.store.reserve_seed(
                seed=seed,
                caller_idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                batch_size=request.batch_size,
                root_operation_id=self.id_generator(now),
                journey_id=self.id_generator(now),
                command_receipt_id=self.id_generator(now),
                event_id=self.id_generator(now),
            )
        except Exception as exc:
            raise _translate_store_error(exc) from exc
        return DemoAcceptedResponse(
            operation_id=reservation.operation_id,
            generation=reservation.generation,
            status_url=f"/demo/v1/operations/{reservation.operation_id}",
        )

    def operation(self, *, operation_id: str) -> DemoOperationResponse:
        try:
            value = self.store.get_operation(operation_id)
        except Exception as exc:
            raise _translate_store_error(exc) from exc
        return DemoOperationResponse(
            operation_id=value.operation_id,
            generation=value.generation,
            state=value.phase,  # type: ignore[arg-type]
            result=value.result,
            error_code=value.error_code,
            updated_at=value.updated_at,
        )

    def advance(
        self,
        *,
        request: DemoAdvanceRequest,
        idempotency_key: str,
    ) -> DemoAcceptedResponse:
        request_sha256 = hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        now = self.now_factory()
        try:
            reservation = self.store.reserve_advance(
                caller_idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                seconds=request.seconds,
                operation_id=self.id_generator(now),
                command_receipt_id=self.id_generator(now),
                event_id=self.id_generator(now),
                audit_id=self.id_generator(now),
            )
        except Exception as exc:
            raise _translate_store_error(exc) from exc
        return DemoAcceptedResponse(
            operation_id=reservation.operation_id,
            generation=reservation.generation,
            status_url=f"/demo/v1/operations/{reservation.operation_id}",
        )

    def reset(
        self,
        *,
        request: DemoResetRequest,
        idempotency_key: str,
    ) -> DemoAcceptedResponse:
        request_sha256 = hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        now = self.now_factory()
        try:
            reservation = self.store.reserve_reset(
                caller_idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                operation_id=self.id_generator(now),
                reset_id=self.id_generator(now),
                command_receipt_id=self.id_generator(now),
                event_id=self.id_generator(now),
                audit_id=self.id_generator(now),
            )
        except Exception as exc:
            raise _translate_store_error(exc) from exc
        return DemoAcceptedResponse(
            operation_id=reservation.operation_id,
            generation=reservation.generation,
            status_url=f"/demo/v1/operations/{reservation.operation_id}",
        )

    def clock(self) -> ScenarioClockResponse:
        try:
            value = self.store.get_clock()
        except Exception as exc:
            raise _translate_store_error(exc) from exc
        return ScenarioClockResponse(
            scenario_time=value.scenario_time,
            generation=value.generation,
        )

    def trace(
        self,
        *,
        operation_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> DemoTraceResponse:
        if operation_id is None or not operation_id.strip():
            raise _demo_error(400, "invalid_request", "operation_id is required")
        try:
            after_sequence = 0 if cursor is None else int(cursor)
        except ValueError as exc:
            raise _demo_error(400, "invalid_request", "invalid trace cursor") from exc
        if after_sequence < 0:
            raise _demo_error(400, "invalid_request", "invalid trace cursor")
        try:
            operation = self.store.get_operation(operation_id)
            entries = self.store.read_trace(
                operation_id=operation_id,
                after_sequence=after_sequence,
                limit=limit + 1,
            )
        except Exception as exc:
            raise _translate_store_error(exc) from exc
        page = entries[:limit]
        next_cursor = str(page[-1].sequence) if len(entries) > limit else None
        return DemoTraceResponse(
            generation=operation.generation,
            entries=page,
            next_cursor=next_cursor,
        )


def _json_object(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        raise RuntimeError("durable demo JSON is not an object")
    return {str(key): item for key, item in parsed.items()}


def _demo_error(status: int, code: str, message: str) -> DemoApiError:
    return DemoApiError(
        status,
        code,
        message,
        retryable=status == 503,
    )


def _translate_store_error(exc: Exception) -> DemoApiError:
    detail = str(exc)
    mapping = {
        "scenario_not_found": (404, "scenario_not_found"),
        "operation_not_found": (404, "operation_not_found"),
        "idempotency_conflict": (409, "idempotency_conflict"),
        "generation_fenced": (409, "generation_fenced"),
        "scenario_contract_invalid": (422, "scenario_contract_invalid"),
        "authorization_denied": (403, "authorization_denied"),
    }
    for marker, (status, code) in mapping.items():
        if marker in detail:
            return _demo_error(status, code, marker.replace("_", " "))
    return _demo_error(
        503,
        "dependency_unavailable",
        "the durable demo store is unavailable",
    )


__all__ = [
    "DemoAdvanceReservation",
    "DemoReservation",
    "DemoResetReservation",
    "DemoStore",
    "DurableDemoController",
    "PostgresDemoStore",
    "StoredDemoClock",
    "StoredDemoOperation",
]
