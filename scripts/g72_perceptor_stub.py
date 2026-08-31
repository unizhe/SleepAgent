#!/usr/bin/env python3
"""TLS-only controlled Perceptor read stub for G7.2 process fault proofs."""

from __future__ import annotations

import argparse
import json
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping


TOKEN_PATH = "/v2/token/get"
HISTORY_PATH = "/v2/vitalSigns/getHistoryData"
SLEEP_REPORT_PATH = "/v2/vitalSigns/getSleepReport"


def _envelope(data: object) -> dict[str, object]:
    return {"success": True, "code": "200", "message": "ok", "data": data}


def _fixture(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("stub fixture must be a JSON object")
    return value


class ControlledPerceptorHandler(BaseHTTPRequestHandler):
    server_version = "SleepAgentG72Stub/1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract.
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        configuration = self.server.configuration  # type: ignore[attr-defined]
        mode = configuration["mode_file"].read_text(encoding="utf-8").strip()
        if mode == "timeout" and self.path != TOKEN_PATH:
            time.sleep(configuration["timeout_seconds"])
            return
        if self.path == TOKEN_PATH:
            value = _envelope(
                {"access_token": "g72-loopback-token", "token_type": "Bearer"}
            )
        elif self.path == HISTORY_PATH:
            if mode == "no_data":
                value = _envelope([])
            elif mode == "success":
                value = configuration["history"]
            else:
                value = {"success": False, "code": "500", "data": {}}
        elif self.path == SLEEP_REPORT_PATH:
            if mode == "no_data":
                value = _envelope({})
            elif mode == "success":
                value = configuration["sleep_report"]
            else:
                value = {"success": False, "code": "500", "data": {}}
        else:
            self.send_error(404)
            return
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def main() -> int:
    parser = argparse.ArgumentParser(prog="g72-perceptor-stub")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--mode-file", type=Path, required=True)
    parser.add_argument("--history-fixture", type=Path, required=True)
    parser.add_argument("--sleep-report-fixture", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    arguments = parser.parse_args()
    server = ThreadingHTTPServer(
        (arguments.host, arguments.port), ControlledPerceptorHandler
    )
    server.configuration = {  # type: ignore[attr-defined]
        "mode_file": arguments.mode_file,
        "timeout_seconds": arguments.timeout_seconds,
        "history": _fixture(arguments.history_fixture),
        "sleep_report": _fixture(arguments.sleep_report_fixture),
    }
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(arguments.cert, arguments.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print(json.dumps({"event": "g72_perceptor_stub_ready", "tls": True}))
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
