"""HTTP-only demo controller and external backend verifier CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


MAX_RESPONSE_BYTES = 1_048_576


class DemoCliError(RuntimeError):
    pass


@dataclass
class DemoHttpClient:
    base_url: str
    demo_token: str
    timeout_seconds: float = 10.0
    opener: Callable[..., Any] = urlopen

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        query: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        if query:
            url += "?" + urlencode(query)
        body = None
        headers = {
            "Accept": "application/json",
            "X-Demo-Controller-Token": self.demo_token,
        }
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = int(response.status)
        except HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            raise DemoCliError(
                f"backend returned HTTP {exc.code}: {_safe_error_code(raw)}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DemoCliError(
                f"backend request failed: {type(exc).__name__}"
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise DemoCliError("backend response exceeded verifier limit")
        if status < 200 or status >= 300:
            raise DemoCliError(f"backend returned HTTP {status}")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DemoCliError("backend returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise DemoCliError("backend response must be a JSON object")
        return value


def verify_backend(
    client: DemoHttpClient,
    *,
    scenario_id: str,
    model: str,
    wait_seconds: float,
) -> dict[str, Any]:
    if model != "deterministic":
        raise DemoCliError("only deterministic verification is supported")
    live = client.request("GET", "/livez")
    if live.get("status") != "alive":
        raise DemoCliError("backend liveness check failed")
    clock = client.request("GET", "/demo/v1/clock")
    _require_replay_watermark(clock)
    accepted = client.request(
        "POST",
        "/demo/v1/seed",
        payload={
            "artifact_family": "canonical-replay-fixtures",
            "scenario_id": scenario_id,
            "batch_size": 250,
        },
        idempotency_key=f"verify-{scenario_id}-{uuid4()}",
    )
    _require_replay_watermark(accepted)
    operation_id = str(accepted.get("operation_id") or "")
    if not operation_id:
        raise DemoCliError("seed response omitted operation_id")
    deadline = time.monotonic() + wait_seconds
    terminal_entry: Mapping[str, Any] | None = None
    while True:
        trace = client.request(
            "GET",
            "/demo/v1/trace",
            query={"operation_id": operation_id, "limit": "200"},
        )
        _require_replay_watermark(trace)
        for entry in trace.get("entries", []):
            if not isinstance(entry, dict):
                continue
            if entry.get("operation_id") != operation_id:
                continue
            if entry.get("state") in {
                "succeeded",
                "failed",
                "blocked",
                "reconciliation_required",
            }:
                terminal_entry = entry
        if terminal_entry is not None:
            break
        if time.monotonic() >= deadline:
            raise DemoCliError("seed operation did not reach a terminal state")
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    if terminal_entry.get("state") != "succeeded":
        raise DemoCliError(
            f"seed operation ended as {terminal_entry.get('state', 'unknown')}"
        )
    return {
        "schema_version": "backend_verification.v1",
        "verified": True,
        "data_mode": "replay",
        "synthetic_non_release": True,
        "model": model,
        "scenario_id": scenario_id,
        "operation_id": operation_id,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sleepagent-demo")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SLEEPAGENT_DEMO_BASE_URL", "http://127.0.0.1:18000"),
    )
    parser.add_argument(
        "--demo-token",
        default=os.environ.get("SLEEPAGENT_DEMO_CONTROLLER_TOKEN", ""),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    verify = subcommands.add_parser("verify")
    verify_subcommands = verify.add_subparsers(dest="target", required=True)
    backend = verify_subcommands.add_parser("backend")
    backend.add_argument("--model", choices=("deterministic",), required=True)
    backend.add_argument("--scenario", default="golden-15-night")
    backend.add_argument("--wait-seconds", type=float, default=30.0)

    seed = subcommands.add_parser("seed")
    seed.add_argument("scenario")
    seed.add_argument("--artifact-family", default="canonical-replay-fixtures")
    seed.add_argument("--batch-size", type=int, default=250)
    advance = subcommands.add_parser("advance")
    advance.add_argument("seconds", type=int)
    subcommands.add_parser("clock")
    subcommands.add_parser("trace")
    subcommands.add_parser("reset")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.demo_token:
        print("sleepagent-demo: demo controller token is required", file=sys.stderr)
        return 2
    client = DemoHttpClient(args.base_url, args.demo_token)
    try:
        if args.command == "verify":
            result = verify_backend(
                client,
                scenario_id=args.scenario,
                model=args.model,
                wait_seconds=args.wait_seconds,
            )
        elif args.command == "seed":
            result = client.request(
                "POST",
                "/demo/v1/seed",
                payload={
                    "artifact_family": args.artifact_family,
                    "scenario_id": args.scenario,
                    "batch_size": args.batch_size,
                },
                idempotency_key=f"cli-seed-{uuid4()}",
            )
        elif args.command == "advance":
            result = client.request(
                "POST",
                "/demo/v1/advance",
                payload={"seconds": args.seconds},
                idempotency_key=f"cli-advance-{uuid4()}",
            )
        elif args.command == "clock":
            result = client.request("GET", "/demo/v1/clock")
        elif args.command == "trace":
            result = client.request("GET", "/demo/v1/trace")
        else:
            result = client.request(
                "POST",
                "/demo/v1/reset",
                payload={"confirmation": "reset-replay-generation"},
                idempotency_key=f"cli-reset-{uuid4()}",
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except DemoCliError as exc:
        print(f"sleepagent-demo: {exc}", file=sys.stderr)
        return 1


def _require_replay_watermark(value: Mapping[str, Any]) -> None:
    if value.get("data_mode") != "replay" or value.get("synthetic_non_release") is not True:
        raise DemoCliError("backend response lacks replay non-release watermark")


def _safe_error_code(raw: bytes) -> str:
    if len(raw) > MAX_RESPONSE_BYTES:
        return "response_too_large"
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid_error_response"
    if isinstance(payload, dict):
        value = payload.get("code") or payload.get("detail")
        if isinstance(value, str) and len(value) <= 100:
            return value
    return "backend_error"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DemoCliError",
    "DemoHttpClient",
    "build_parser",
    "main",
    "verify_backend",
]
