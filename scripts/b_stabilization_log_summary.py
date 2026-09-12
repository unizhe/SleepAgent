#!/usr/bin/env python3
"""Summarize redacted API/cpolar evidence for one local wake-date window."""

from __future__ import annotations

import argparse
import gzip
import json
import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


UTC = timezone.utc
LOCAL = ZoneInfo("Asia/Shanghai")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
CPOLAR_TIME = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})\]")


def _lines(root: Path, stem: str) -> Iterable[str]:
    for path in sorted(root.glob(stem + "*")):
        if not path.is_file():
            continue
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as stream:
            yield from stream


def _window(wake_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(wake_date - timedelta(days=1), time(12), LOCAL)
    end = datetime.combine(wake_date, time(12), LOCAL)
    return start.astimezone(UTC), end.astimezone(UTC)


def _api_summary(root: Path, start: datetime, end: datetime) -> dict[str, object]:
    events: Counter[str] = Counter()
    dispositions: Counter[str] = Counter()
    status_classes: Counter[str] = Counter()
    first: datetime | None = None
    last: datetime | None = None
    for line in _lines(root, "api.log"):
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
            observed = datetime.fromisoformat(str(item["timestamp"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if not start <= observed < end:
            continue
        event = str(item.get("event", "unknown"))
        events[event] += 1
        if event == "perceptor_webhook_durable_result":
            dispositions[str(item.get("disposition", "unknown"))] += 1
        if event == "backend_http_completed":
            status_classes[str(item.get("status_class", "unknown"))] += 1
        if event in {
            "perceptor_webhook_authenticated",
            "perceptor_webhook_durable_result",
        }:
            first = observed if first is None else min(first, observed)
            last = observed if last is None else max(last, observed)
    return {
        "events": dict(sorted(events.items())),
        "durable_dispositions": dict(sorted(dispositions.items())),
        "http_status_classes": dict(sorted(status_classes.items())),
        "first_webhook_evidence_at": None if first is None else first.isoformat(),
        "last_webhook_evidence_at": None if last is None else last.isoformat(),
    }


def _cpolar_summary(root: Path, start: datetime, end: datetime) -> dict[str, object]:
    patterns = {
        "new_proxy_connection": "New connection to:",
        "joined_proxy_connection": "Joined with connection",
        "control_pong": '"Type":"Pong"',
        "closed_network_connection": "use of closed network connection",
        "eof": "EOF",
        "connection_refused": "connection refused",
        "reconnect": "reconnect",
        "disconnect": "disconnect",
        "proxy_error": "proxy error",
    }
    counts: Counter[str] = Counter()
    first: datetime | None = None
    last: datetime | None = None
    for raw in _lines(root, "cpolar.log"):
        line = ANSI.sub("", raw)
        match = CPOLAR_TIME.search(line)
        if match is None:
            continue
        try:
            observed = datetime.fromisoformat(match.group(1)).astimezone(UTC)
        except ValueError:
            continue
        if not start <= observed < end:
            continue
        first = observed if first is None else min(first, observed)
        last = observed if last is None else max(last, observed)
        lowered = line.lower()
        for name, marker in patterns.items():
            if marker.lower() in lowered:
                counts[name] += 1
    return {
        "signals": dict(sorted(counts.items())),
        "first_log_at": None if first is None else first.isoformat(),
        "last_log_at": None if last is None else last.isoformat(),
        "attribution_limit": (
            "server-side cpolar evidence cannot prove whether the vendor sent"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wake-date", type=date.fromisoformat, required=True)
    parser.add_argument("--log-root", type=Path, required=True)
    args = parser.parse_args()
    start, end = _window(args.wake_date)
    result = {
        "schema_version": "b_stabilization_log_summary.v1",
        "target_wake_date": args.wake_date.isoformat(),
        "timezone": "Asia/Shanghai",
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "api": _api_summary(args.log_root, start, end),
        "cpolar": _cpolar_summary(args.log_root, start, end),
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
