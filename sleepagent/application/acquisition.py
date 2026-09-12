"""Durable acquisition schedule application services."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sleepagent.application.device_bindings import ManagedDeviceBinding
from sleepagent.observability import log_event
from sleepagent.persistence.uow import UnitOfWorkFactory, UowScope, WorkerClaimScope


UTC = timezone.utc


class AcquisitionJobType(str, Enum):
    HISTORY_OVERLAP_PULL = "perceptor.history_overlap_pull"
    SLEEP_REPORT_PULL = "perceptor.sleep_report_pull"
    NIGHT_FINALIZATION_SCAN = "night.finalization_scan"


class AcquisitionSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "acquisition_schedule.v1"
    schedule_id: str = Field(min_length=1)
    namespace_id: str = Field(min_length=1)
    data_mode: str
    namespace_generation: int = Field(ge=1)
    run_id: str | None = None
    arm_id: str | None = None
    subject_id: str = Field(min_length=1)
    device_binding_id: str = Field(min_length=1)
    binding_version: int = Field(ge=1)
    job_type: AcquisitionJobType
    enabled: bool
    next_run_at: datetime
    last_fire_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = Field(ge=0)
    last_error_code: str | None = None
    cadence_seconds: int = Field(ge=60, le=604_800)
    jitter_seconds: int = Field(ge=0, le=3_600)
    max_attempts: int = Field(ge=1, le=100)
    schedule_policy_version: str = Field(min_length=1)
    schedule_policy_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    cas_version: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def exact_mode_scope(self) -> "AcquisitionSchedule":
        if self.data_mode == "live" and (self.run_id or self.arm_id):
            raise ValueError("live schedule cannot carry replay identifiers")
        if self.data_mode == "replay" and not (self.run_id and self.arm_id):
            raise ValueError("replay schedule requires run and arm identifiers")
        if self.jitter_seconds >= self.cadence_seconds:
            raise ValueError("jitter_seconds must be less than cadence_seconds")
        return self


class AcquisitionScheduleFire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fire_id: str
    schedule_id: str
    operation_id: str
    job_type: AcquisitionJobType
    scheduled_for: datetime


class AcquisitionScheduleConflict(RuntimeError):
    pass


class AcquisitionScheduleService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        now_factory: Any = lambda: datetime.now(tz=UTC),
    ) -> None:
        self.uow_factory = uow_factory
        self.now_factory = now_factory

    def create(
        self,
        scope: UowScope,
        *,
        binding: ManagedDeviceBinding,
        job_type: AcquisitionJobType,
        next_run_at: datetime,
        cadence_seconds: int,
        jitter_seconds: int = 0,
        max_attempts: int = 5,
        policy_version: str = "acquisition-default.v1",
    ) -> AcquisitionSchedule:
        if scope.process_role != "api" or scope.purpose != "device_binding_management":
            raise PermissionError("schedule creation requires binding authority")
        if scope.subject_id != binding.binding.subject_id:
            raise PermissionError("schedule binding is outside subject authority")
        if binding.binding.status.value != "active":
            raise ValueError("schedule requires an active DeviceBinding")
        if next_run_at.tzinfo is None or next_run_at.utcoffset() is None:
            raise ValueError("next_run_at must be timezone-aware")
        if jitter_seconds < 0 or jitter_seconds >= cadence_seconds:
            raise ValueError("jitter_seconds must satisfy 0 <= jitter < cadence")
        policy = {
            "schema_version": "acquisition_schedule_policy.v1",
            "policy_version": policy_version,
            "job_type": job_type.value,
            "cadence_seconds": cadence_seconds,
            "jitter_seconds": jitter_seconds,
            "max_attempts": max_attempts,
        }
        canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"))
        policy_sha = hashlib.sha256(canonical.encode()).hexdigest()
        schedule_id = _identifier(
            "schedule",
            scope.namespace_id,
            binding.binding.device_binding_id,
            str(binding.binding.binding_version),
            job_type.value,
        )
        now = self.now_factory()
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO public.backend_acquisition_schedules (
                      schedule_id, namespace_id, data_mode,
                      namespace_generation, run_id, arm_id, subject_id,
                      device_binding_id, binding_version, job_type, enabled,
                      next_run_at, cadence_seconds, jitter_seconds,
                      max_attempts, schedule_policy_version,
                      schedule_policy_sha256, schedule_json, created_at, updated_at
                    ) VALUES (
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE,
                      %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s
                    ) ON CONFLICT (schedule_id) DO NOTHING
                    """,
                    (
                        schedule_id,
                        scope.namespace_id,
                        scope.data_mode,
                        scope.namespace_generation,
                        scope.run_id,
                        scope.arm_id,
                        binding.binding.subject_id,
                        binding.binding.device_binding_id,
                        binding.binding.binding_version,
                        job_type.value,
                        next_run_at,
                        cadence_seconds,
                        jitter_seconds,
                        max_attempts,
                        policy_version,
                        policy_sha,
                        canonical,
                        now,
                        now,
                    ),
                )
                uow.commit()
            except Exception as exc:
                raise AcquisitionScheduleConflict(
                    "schedule create was rejected"
                ) from exc
            finally:
                cursor.close()
        schedule = self.show(scope, schedule_id=schedule_id)
        expected = (
            binding.binding.device_binding_id,
            binding.binding.binding_version,
            job_type,
            cadence_seconds,
            jitter_seconds,
            policy_sha,
        )
        actual = (
            schedule.device_binding_id,
            schedule.binding_version,
            schedule.job_type,
            schedule.cadence_seconds,
            schedule.jitter_seconds,
            schedule.schedule_policy_sha256,
        )
        if actual != expected:
            raise AcquisitionScheduleConflict("schedule id conflicts with prior policy")
        return schedule

    def show(self, scope: UowScope, *, schedule_id: str) -> AcquisitionSchedule:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    _schedule_select() + " WHERE schedule_id = %s",
                    (schedule_id,),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        if row is None:
            raise KeyError(schedule_id)
        return _schedule(row)

    def list(self, scope: UowScope) -> tuple[AcquisitionSchedule, ...]:
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    _schedule_select()
                    + " WHERE namespace_id = %s AND data_mode = %s"
                    + " AND subject_id = %s ORDER BY next_run_at, schedule_id",
                    (scope.namespace_id, scope.data_mode, scope.subject_id),
                )
                rows = tuple(cursor.fetchall())
            finally:
                cursor.close()
        return tuple(_schedule(row) for row in rows)

    def pause(
        self, scope: UowScope, *, schedule_id: str, expected_cas: int
    ) -> AcquisitionSchedule:
        return self._set_enabled(
            scope, schedule_id=schedule_id, expected_cas=expected_cas, enabled=False
        )

    def resume(
        self,
        scope: UowScope,
        *,
        schedule_id: str,
        expected_cas: int,
        next_run_at: datetime | None = None,
    ) -> AcquisitionSchedule:
        return self._set_enabled(
            scope,
            schedule_id=schedule_id,
            expected_cas=expected_cas,
            enabled=True,
            next_run_at=next_run_at,
        )

    def _set_enabled(
        self,
        scope: UowScope,
        *,
        schedule_id: str,
        expected_cas: int,
        enabled: bool,
        next_run_at: datetime | None = None,
    ) -> AcquisitionSchedule:
        now = self.now_factory()
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    UPDATE public.backend_acquisition_schedules
                    SET enabled = %s,
                        next_run_at = COALESCE(%s, next_run_at),
                        cas_version = cas_version + 1, updated_at = %s
                    WHERE schedule_id = %s AND cas_version = %s
                    """,
                    (enabled, next_run_at, now, schedule_id, expected_cas),
                )
                if cursor.rowcount != 1:
                    raise AcquisitionScheduleConflict("schedule CAS conflict")
                uow.commit()
            finally:
                cursor.close()
        return self.show(scope, schedule_id=schedule_id)

    def record_result(
        self,
        scope: UowScope,
        *,
        fire_id: str,
        succeeded: bool,
        error_code: str | None = None,
    ) -> None:
        now = self.now_factory()
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    """
                    UPDATE public.backend_acquisition_schedule_fires
                    SET status = %s, attempt_count = attempt_count + 1,
                        last_error_code = %s, completed_at = %s
                    WHERE fire_id = %s AND status IN ('queued', 'failed')
                    RETURNING schedule_id
                    """,
                    (
                        "succeeded" if succeeded else "failed",
                        error_code,
                        now if succeeded else None,
                        fire_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise AcquisitionScheduleConflict("schedule fire result conflict")
                if succeeded:
                    cursor.execute(
                        """
                        UPDATE public.backend_acquisition_schedules
                        SET last_success_at = %s, consecutive_failures = 0,
                            last_error_code = NULL, cas_version = cas_version + 1,
                            updated_at = %s
                        WHERE schedule_id = %s
                        """,
                        (now, now, str(row[0])),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE public.backend_acquisition_schedules
                        SET consecutive_failures = consecutive_failures + 1,
                            last_error_code = %s,
                            next_run_at = GREATEST(
                              next_run_at,
                              %s + make_interval(secs => LEAST(
                                3600, 60 * (1 << LEAST(consecutive_failures, 5))
                              ))
                            ),
                            cas_version = cas_version + 1, updated_at = %s
                        WHERE schedule_id = %s
                        """,
                        (error_code or "scheduled_work_failed", now, now, str(row[0])),
                    )
                uow.commit()
            finally:
                cursor.close()

    def fire_succeeded(self, scope: UowScope, *, fire_id: str) -> bool:
        """Allow a recovered operation to skip an already committed effect."""

        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT status FROM public.backend_acquisition_schedule_fires "
                    "WHERE fire_id = %s",
                    (fire_id,),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
        if row is None:
            raise AcquisitionScheduleConflict("schedule fire is not visible")
        return str(row[0]) == "succeeded"


class PostgresAcquisitionScheduler:
    """Thin cron-like edge: claim due rows and create durable work only."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory[Any],
        *,
        data_mode: str,
        service_principal_id: str,
        worker_instance: str,
        enabled: bool = False,
    ) -> None:
        self.uow_factory = uow_factory
        self.data_mode = data_mode
        self.service_principal_id = service_principal_id
        self.worker_instance = worker_instance
        self.enabled = enabled

    def fire_due(self, *, limit: int = 20) -> tuple[AcquisitionScheduleFire, ...]:
        if not self.enabled:
            return ()
        scope = WorkerClaimScope(
            data_mode=self.data_mode,  # type: ignore[arg-type]
            purpose="acquisition_schedule",
            service_principal_id=self.service_principal_id,
            worker_instance=self.worker_instance,
        )
        with self.uow_factory.begin(scope) as uow:
            cursor = uow.connection.cursor()
            try:
                cursor.execute(
                    "SELECT * FROM "
                    "public.sleepagent_fire_due_acquisition_schedules(%s,%s,%s)",
                    (self.worker_instance, limit, True),
                )
                rows = tuple(cursor.fetchall())
                uow.commit()
            finally:
                cursor.close()
        fires = tuple(
            AcquisitionScheduleFire(
                fire_id=str(row[0]),
                schedule_id=str(row[1]),
                operation_id=str(row[2]),
                job_type=AcquisitionJobType(str(row[3])),
                scheduled_for=row[4],
            )
            for row in rows
        )
        for fire in fires:
            log_event(
                "acquisition_schedule_fire_created",
                schedule_id=fire.schedule_id,
                operation_id=fire.operation_id,
                job_type=fire.job_type.value,
                scheduled_for=fire.scheduled_for,
            )
        return fires


def _schedule_select() -> str:
    return """
      SELECT schedule_id, namespace_id, data_mode, namespace_generation,
        run_id, arm_id, subject_id, device_binding_id, binding_version,
        job_type, enabled, next_run_at, last_fire_at, last_success_at,
        consecutive_failures, last_error_code, cadence_seconds,
        jitter_seconds, max_attempts, schedule_policy_version,
        schedule_policy_sha256, cas_version, created_at, updated_at
      FROM public.backend_acquisition_schedules
    """


def _schedule(row: Any) -> AcquisitionSchedule:
    return AcquisitionSchedule(
        schedule_id=str(row[0]),
        namespace_id=str(row[1]),
        data_mode=str(row[2]),
        namespace_generation=int(row[3]),
        run_id=None if row[4] is None else str(row[4]),
        arm_id=None if row[5] is None else str(row[5]),
        subject_id=str(row[6]),
        device_binding_id=str(row[7]),
        binding_version=int(row[8]),
        job_type=AcquisitionJobType(str(row[9])),
        enabled=bool(row[10]),
        next_run_at=row[11],
        last_fire_at=row[12],
        last_success_at=row[13],
        consecutive_failures=int(row[14]),
        last_error_code=None if row[15] is None else str(row[15]),
        cadence_seconds=int(row[16]),
        jitter_seconds=int(row[17]),
        max_attempts=int(row[18]),
        schedule_policy_version=str(row[19]),
        schedule_policy_sha256=str(row[20]),
        cas_version=int(row[21]),
        created_at=row[22],
        updated_at=row[23],
    )


def _identifier(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
    return f"{prefix}:{digest}"


__all__ = [
    "AcquisitionJobType",
    "AcquisitionSchedule",
    "AcquisitionScheduleConflict",
    "AcquisitionScheduleFire",
    "AcquisitionScheduleService",
    "PostgresAcquisitionScheduler",
]
