"""Concrete durable-worker composition root and CLI entry point."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from sleepagent.config import SleepBackendSettings
from sleepagent.workers.commands import build_command_worker_handlers
from sleepagent.workers.demo import build_demo_worker_handlers
from sleepagent.workers.effects import build_effect_worker_handlers
from sleepagent.workers.ingestion import build_b3_worker_handlers
from sleepagent.workers.product import build_product_agent_worker_handlers
from sleepagent.workers.retention import build_retention_worker_handlers
from sleepagent.workers.kernel import WorkHandler
from sleepagent.workers.runtime import (
    DurableWorkStoreError,
    build_worker_parser,
    run_worker_command,
)


def build_worker_handlers(
    settings: SleepBackendSettings,
) -> dict[str, WorkHandler]:
    registries = (
        build_b3_worker_handlers(settings),
        build_product_agent_worker_handlers(settings),
        build_command_worker_handlers(settings),
        build_demo_worker_handlers(settings),
        build_effect_worker_handlers(settings),
        build_retention_worker_handlers(settings),
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
    handlers: dict[str, WorkHandler] = {}
    for registry in registries:
        handlers.update(registry)
    unsupported = set(settings.worker_queues) - set(handlers)
    if unsupported:
        raise DurableWorkStoreError(
            "worker handlers must be explicitly composed: "
            + ", ".join(sorted(unsupported))
        )
    return {queue: handlers[queue] for queue in settings.worker_queues}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_worker_parser().parse_args(argv)
    try:
        settings = SleepBackendSettings.from_environment()
        handlers = (
            build_worker_handlers(settings)
            if args.action == "run"
            else None
        )
        return run_worker_command(
            args.action,
            settings=settings,
            handlers=handlers,
            lease_seconds=getattr(args, "lease_seconds", 30),
            heartbeat_seconds=getattr(args, "heartbeat_seconds", 10.0),
            idle_poll_seconds=getattr(args, "idle_poll_seconds", 0.25),
            drain_seconds=getattr(args, "drain_seconds", 30.0),
        )
    except (ValueError, RuntimeError) as exc:
        print(f"worker {args.action} failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_worker_handlers", "main"]
