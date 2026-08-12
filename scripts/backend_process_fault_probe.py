#!/usr/bin/env python3
"""Database-side synchronization for Docker process fault proofs.

The DSN is read only from ``SLEEPAGENT_FAULT_PROBE_POSTGRES_DSN`` so database
credentials never appear in argv or the process-proof transcript.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


DSN_ENV = "SLEEPAGENT_FAULT_PROBE_POSTGRES_DSN"
DELIVERY_DESTINATION = "replay_care_notification"


class TerminalProbeError(RuntimeError):
    """Observed state proves the requested fault window can no longer occur."""


def delivery_lock_name(semantic_effect_key: str) -> str:
    if not semantic_effect_key:
        raise ValueError("semantic effect key is required")
    return "sleepagent:replay-delivery-effect:" + semantic_effect_key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backend-process-fault-probe")
    subparsers = parser.add_subparsers(dest="action", required=True)

    hold = subparsers.add_parser("hold-delivery-effect-lock")
    hold.add_argument("--ready-file", type=Path, required=True)
    hold.add_argument("--release-file", type=Path, required=True)
    hold.add_argument("--timeout-seconds", type=float, default=180.0)

    send_started = subparsers.add_parser("wait-delivery-send-started")
    send_started.add_argument("--state-file", type=Path, required=True)
    send_started.add_argument("--timeout-seconds", type=float, default=60.0)

    root = subparsers.add_parser("wait-root-active")
    root.add_argument("--root-file", type=Path, required=True)
    root.add_argument("--timeout-seconds", type=float, default=120.0)
    root.add_argument("--require-progress", action="store_true")
    return parser


def _dsn() -> str:
    value = os.environ.get(DSN_ENV, "").strip()
    if not value:
        raise RuntimeError(f"{DSN_ENV} is required")
    return value


def _wait_until(
    probe: Callable[[], Any | None],
    *,
    timeout_seconds: float,
    description: str,
) -> Any:
    if timeout_seconds <= 0:
        raise ValueError("timeout must be positive")
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = probe()
            if value is not None:
                return value
            last_error = None
        except TerminalProbeError:
            raise
        except Exception as exc:  # PostgreSQL may be intentionally restarting.
            last_error = exc
        time.sleep(0.1)
    suffix = "" if last_error is None else f": {last_error.__class__.__name__}"
    raise TimeoutError(f"timed out waiting for {description}{suffix}")


def _pending_delivery(connection: Any) -> dict[str, str] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT delivery_intent_id, semantic_effect_key "
            "FROM public.backend_delivery_intents "
            "WHERE destination = %s AND status IN ('pending', 'retry') "
            "ORDER BY created_at, delivery_intent_id LIMIT 1",
            (DELIVERY_DESTINATION,),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return {
        "delivery_intent_id": str(row[0]),
        "semantic_effect_key": str(row[1]),
    }


def hold_delivery_effect_lock(
    *,
    ready_file: Path,
    release_file: Path,
    timeout_seconds: float,
) -> None:
    import psycopg

    ready_file.parent.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(_dsn(), autocommit=True) as connection:
        delivery = _wait_until(
            lambda: _pending_delivery(connection),
            timeout_seconds=timeout_seconds,
            description="a pending deterministic delivery",
        )
        lock_name = delivery_lock_name(delivery["semantic_effect_key"])
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                (lock_name,),
            )
        ready_file.write_text(
            json.dumps(
                {**delivery, "lock_name": lock_name},
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        _wait_until(
            lambda: True if release_file.exists() else None,
            timeout_seconds=timeout_seconds,
            description="delivery fault-lock release",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                (lock_name,),
            )
            row = cursor.fetchone()
        if row != (True,):
            raise RuntimeError("delivery effect advisory lock was not released")


def wait_delivery_send_started(
    *,
    state_file: Path,
    timeout_seconds: float,
) -> None:
    import psycopg

    state = json.loads(state_file.read_text(encoding="utf-8"))
    delivery_intent_id = str(state["delivery_intent_id"])

    def probe() -> dict[str, str] | None:
        with psycopg.connect(_dsn()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT invocation.invocation_id, invocation.current_state "
                    "FROM public.backend_delivery_intents AS intent "
                    "JOIN public.sleep_domain_domain_outbox AS source "
                    "ON source.event_id = intent.source_event_id "
                    "JOIN public.backend_invocations AS invocation "
                    "ON invocation.operation_id = source.operation_id "
                    "AND invocation.invocation_kind = 'external_sink' "
                    "AND invocation.invocation_key = "
                    "'replay-delivery:' || intent.semantic_effect_key || ':v1' "
                    "WHERE intent.delivery_intent_id = %s",
                    (delivery_intent_id,),
                )
                row = cursor.fetchone()
        if row is None or str(row[1]) != "send_started":
            return None
        return {
            "invocation_id": str(row[0]),
            "current_state": str(row[1]),
        }

    observed = _wait_until(
        probe,
        timeout_seconds=timeout_seconds,
        description="committed delivery send_started journal state",
    )
    print(json.dumps(observed, sort_keys=True, separators=(",", ":")))


def wait_root_active(
    *,
    root_file: Path,
    timeout_seconds: float,
    require_progress: bool = False,
) -> None:
    import psycopg

    root_operation_id = root_file.read_text(encoding="utf-8").strip()
    if not root_operation_id:
        raise ValueError("root operation file is empty")

    def probe() -> dict[str, str] | None:
        with psycopg.connect(_dsn()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT phase FROM public.backend_demo_journeys "
                    "WHERE root_operation_id = %s",
                    (root_operation_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        phase = str(row[0])
        if phase in {"succeeded", "blocked", "reconciliation_required", "failed"}:
            raise TerminalProbeError(
                "root became terminal before PostgreSQL fault"
            )
        if require_progress and phase == "accepted":
            return None
        return {"root_operation_id": root_operation_id, "phase": phase}

    observed = _wait_until(
        probe,
        timeout_seconds=timeout_seconds,
        description="an active Demo root",
    )
    print(json.dumps(observed, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "hold-delivery-effect-lock":
        hold_delivery_effect_lock(
            ready_file=args.ready_file,
            release_file=args.release_file,
            timeout_seconds=args.timeout_seconds,
        )
    elif args.action == "wait-delivery-send-started":
        wait_delivery_send_started(
            state_file=args.state_file,
            timeout_seconds=args.timeout_seconds,
        )
    elif args.action == "wait-root-active":
        wait_root_active(
            root_file=args.root_file,
            timeout_seconds=args.timeout_seconds,
            require_progress=args.require_progress,
        )
    else:  # pragma: no cover - argparse owns the closed set.
        raise ValueError(f"unknown action: {args.action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
