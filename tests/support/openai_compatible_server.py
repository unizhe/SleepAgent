from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class OpenAIRequestRecord:
    path: str
    headers: Mapping[str, str] = field(repr=False)
    json_body: Any | None
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class ScriptedOpenAIResponse:
    status_code: int = 200
    body: Any = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    delay_seconds: float = 0


OpenAIResponder = Callable[
    [OpenAIRequestRecord, int], ScriptedOpenAIResponse
]


class LoopbackOpenAICompatibleServer:
    """Small real-socket OpenAI-compatible server for integration tests."""

    def __init__(
        self,
        responder: ScriptedOpenAIResponse | OpenAIResponder,
    ) -> None:
        self._responder = responder
        self._requests: list[OpenAIRequestRecord] = []
        self._errors: list[BaseException] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        server = self._require_started()
        host, port = server.server_address[:2]
        return f"http://{host}:{port}/v1"

    @property
    def requests(self) -> tuple[OpenAIRequestRecord, ...]:
        with self._lock:
            return tuple(self._requests)

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self._requests)

    @property
    def errors(self) -> tuple[BaseException, ...]:
        with self._lock:
            return tuple(self._errors)

    def __enter__(self) -> "LoopbackOpenAICompatibleServer":
        if self._server is not None:
            raise RuntimeError("loopback provider server is already running")

        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                content_length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(content_length)
                try:
                    json_body = json.loads(body) if body else None
                except (json.JSONDecodeError, UnicodeDecodeError):
                    json_body = None
                request = OpenAIRequestRecord(
                    path=self.path,
                    headers={key: value for key, value in self.headers.items()},
                    json_body=json_body,
                    body=body,
                )
                with owner._lock:
                    index = len(owner._requests)
                    owner._requests.append(request)
                try:
                    response = (
                        owner._responder(request, index)
                        if callable(owner._responder)
                        else owner._responder
                    )
                except BaseException as exc:  # pragma: no cover - debug guard
                    with owner._lock:
                        owner._errors.append(exc)
                    response = ScriptedOpenAIResponse(
                        status_code=500,
                        body={"error": {"message": "test responder failed"}},
                    )
                if response.delay_seconds:
                    time.sleep(response.delay_seconds)
                response_body = _encode_response_body(response.body)
                try:
                    self.send_response(response.status_code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response_body)))
                    for key, value in response.headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(response_body)
                except (BrokenPipeError, ConnectionResetError):
                    # Expected when exercising a client-side read timeout.
                    return

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="loopback-openai-compatible-server",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        server = self._server
        thread = self._thread
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        self._server = None
        self._thread = None

    def _require_started(self) -> ThreadingHTTPServer:
        if self._server is None:
            raise RuntimeError("loopback provider server has not been started")
        return self._server


def _encode_response_body(body: Any) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


__all__ = [
    "LoopbackOpenAICompatibleServer",
    "OpenAIRequestRecord",
    "OpenAIResponder",
    "ScriptedOpenAIResponse",
]
