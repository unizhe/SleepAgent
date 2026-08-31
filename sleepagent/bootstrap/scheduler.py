"""Feature-gated composition root for the durable acquisition scheduler."""

from __future__ import annotations

import argparse
import json
import signal
import threading
from collections.abc import Sequence

from sleepagent.application.acquisition import PostgresAcquisitionScheduler
from sleepagent.config import ProcessRole, SleepBackendSettings
from sleepagent.persistence.uow import (
    PoolConfiguration,
    PsycopgPoolProvider,
    UnitOfWorkFactory,
)


def build_scheduler(
    settings: SleepBackendSettings,
    *,
    worker_instance: str,
) -> tuple[PsycopgPoolProvider, PostgresAcquisitionScheduler]:
    if settings.process_role is not ProcessRole.WORKER:
        raise ValueError("scheduler requires a worker capability profile")
    pool = PsycopgPoolProvider.from_dsn(
        settings.database_dsn.get_secret_value(),
        configuration=PoolConfiguration(
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            open_timeout_seconds=settings.pool_timeout_seconds,
        ),
        application_name="sleepagent-acquisition-scheduler",
    )
    uow_factory = UnitOfWorkFactory(
        pool,
        lock_timeout_ms=settings.lock_timeout_ms,
        statement_timeout_ms=settings.statement_timeout_ms,
        idle_in_transaction_timeout_ms=settings.idle_transaction_timeout_ms,
    )
    return pool, PostgresAcquisitionScheduler(
        uow_factory,
        data_mode=settings.data_mode.value,
        service_principal_id=settings.service_principal_id,
        worker_instance=worker_instance,
        enabled=settings.acquisition_scheduler_enabled,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sleepagent-acquisition-scheduler",
        description="Create idempotent durable acquisition work from due schedules",
    )
    parser.add_argument("action", choices=("once", "run"))
    parser.add_argument("--worker-instance", default="acquisition-scheduler-1")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    arguments = parser.parse_args(argv)
    settings = SleepBackendSettings.from_environment()
    if not settings.acquisition_scheduler_enabled:
        print(json.dumps({"enabled": False, "fires": 0}))
        return 0 if arguments.action == "once" else 2
    pool, scheduler = build_scheduler(
        settings, worker_instance=arguments.worker_instance
    )
    pool.open(wait=True, timeout=settings.pool_timeout_seconds)
    try:
        if arguments.action == "once":
            fires = scheduler.fire_due(limit=arguments.limit)
            print(json.dumps({"enabled": True, "fires": len(fires)}))
            return 0
        stopped = threading.Event()

        def stop(signum: int, frame: object) -> None:
            del signum, frame
            stopped.set()

        prior = {
            signum: signal.signal(signum, stop)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            while not stopped.is_set():
                scheduler.fire_due(limit=arguments.limit)
                stopped.wait(arguments.poll_seconds)
        finally:
            for signum, handler in prior.items():
                signal.signal(signum, handler)
        return 0
    finally:
        pool.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_scheduler", "main"]
