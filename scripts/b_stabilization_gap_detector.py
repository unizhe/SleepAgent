#!/usr/bin/env python3
"""Read-only, restart-safe Push receipt-gap evidence for the live binding."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg


NAMESPACE = "live:p4e1-r2-20260824"
SUBJECT = "p4d-real-acceptance-subject"
BINDING = "p4e1-r2-binding-v2-dddead70730a475da80460e5cc84a65a"
EVENT_TYPE = "VitalSignsDataEvent"
UTC = timezone.utc


def _read_gaps(dsn: str, warning_seconds: int, repair_seconds: int) -> dict[str, Any]:
    observed_at = datetime.now(tz=UTC)
    with psycopg.connect(dsn, autocommit=False) as connection:
        connection.execute("BEGIN READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '5s'")
        settings = {
            "sleepagent.namespace_id": NAMESPACE,
            "sleepagent.data_mode": "live",
            "sleepagent.namespace_generation": "1",
            "sleepagent.run_id": "",
            "sleepagent.arm_id": "",
            "sleepagent.subject_id": SUBJECT,
            "sleepagent.actor_id": "p4e1-r2-product-cli-elder",
            "sleepagent.actor_role": "elder",
            "sleepagent.service_principal_id": "sleepagent-p4e1-r2-api",
            "sleepagent.process_role": "api",
            "sleepagent.purpose": "sleep_care",
            "sleepagent.authorization_epoch": "1",
            "sleepagent.privacy_epoch": "1",
            "sleepagent.retrieval_policy_epoch": "1",
            "sleepagent.worker_instance": "",
        }
        for name, value in settings.items():
            connection.execute("SELECT set_config(%s, %s, true)", (name, value))
        rows = connection.execute(
            """
            WITH received AS (
              SELECT received_at,
                     lag(received_at) OVER (ORDER BY received_at) AS prior
              FROM public.sleep_domain_raw_inbox
              WHERE namespace_id = %s AND data_mode = 'live' AND subject_id = %s
                AND provider_id = 'perceptor' AND event_type = %s
                AND received_at >= timestamptz '2026-09-05 18:00:00+08'
            )
            SELECT prior, received_at, extract(epoch FROM received_at - prior)
            FROM received
            WHERE received_at - prior >= make_interval(secs => %s)
            ORDER BY prior
            """,
            (NAMESPACE, SUBJECT, EVENT_TYPE, warning_seconds),
        ).fetchall()
        latest = connection.execute(
            """
            SELECT max(received_at)
            FROM public.sleep_domain_raw_inbox
            WHERE namespace_id = %s AND data_mode = 'live' AND subject_id = %s
              AND provider_id = 'perceptor' AND event_type = %s
            """,
            (NAMESPACE, SUBJECT, EVENT_TYPE),
        ).fetchone()[0]
        connection.commit()

    gaps: list[dict[str, Any]] = []
    for start, end, duration in rows:
        gaps.append(
            {
                "gap_key": f"closed:{start.isoformat()}:{end.isoformat()}",
                "gap_start": start.isoformat(),
                "gap_end": end.isoformat(),
                "duration_seconds": int(duration),
                "receipt_status": "PUSH_RECOVERED",
                "threshold": (
                    "repair" if duration >= repair_seconds else "warning"
                ),
                "repair_target_interval": None,
                "repair_status": "INSUFFICIENT_SAMPLE_TIME_EVIDENCE",
            }
        )
    if latest is not None:
        active_seconds = int((observed_at - latest).total_seconds())
        if active_seconds >= warning_seconds:
            gaps.append(
                {
                    "gap_key": f"active:{latest.isoformat()}",
                    "gap_start": latest.isoformat(),
                    "gap_end": None,
                    "duration_seconds": active_seconds,
                    "receipt_status": "PUSH_GAP_ACTIVE",
                    "threshold": (
                        "repair" if active_seconds >= repair_seconds else "warning"
                    ),
                    "repair_target_interval": None,
                    "repair_status": "INSUFFICIENT_SAMPLE_TIME_EVIDENCE",
                }
            )
    return {
        "schema_version": "sleepagent_push_gap_evidence.v1",
        "observed_at": observed_at.isoformat(),
        "namespace_id": NAMESPACE,
        "subject_id": SUBJECT,
        "device_binding_id": BINDING,
        "event_type": EVENT_TYPE,
        "warning_seconds": warning_seconds,
        "repair_seconds": repair_seconds,
        "last_push_received_at": None if latest is None else latest.isoformat(),
        "gaps": gaps,
        "attribution": "RECEIPT_EVIDENCE_ONLY",
    }


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--warning-seconds", type=int, default=180)
    parser.add_argument("--repair-seconds", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not 30 <= args.interval_seconds <= 300:
        raise ValueError("interval must be between 30 and 300 seconds")
    if not 60 <= args.warning_seconds < args.repair_seconds <= 3600:
        raise ValueError("gap thresholds are invalid")
    dsn = os.environ.get("SLEEPAGENT_ONE_NIGHT_READ_DSN", "")
    if not dsn:
        raise RuntimeError("read-only DSN is required")
    args.state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    prior_digest: str | None = None
    while True:
        state = _read_gaps(dsn, args.warning_seconds, args.repair_seconds)
        stable = dict(state)
        stable.pop("observed_at", None)
        digest = hashlib.sha256(_canonical(stable)).hexdigest()
        temporary = args.state.with_suffix(args.state.suffix + ".tmp")
        temporary.write_bytes(_canonical(state) + b"\n")
        temporary.chmod(0o600)
        os.replace(temporary, args.state)
        if digest != prior_digest:
            with args.evidence.open("ab") as stream:
                stream.write(_canonical(state) + b"\n")
            args.evidence.chmod(0o600)
            prior_digest = digest
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
