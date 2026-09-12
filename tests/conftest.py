from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx
import pytest


_EXECUTION_MARKERS = (
    "postgres",
    "asgi_lifespan",
    "process_harness",
    "e2e",
)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(
    session: pytest.Session,
    exitstatus: int,
) -> None:
    """Make verifier-required lanes fail if pytest reports any skip."""

    del exitstatus
    if os.environ.get("SLEEPAGENT_PYTEST_FAIL_ON_SKIP") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = () if reporter is None else reporter.stats.get("skipped", ())
    if not skipped:
        return
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if reporter is not None:
        reporter.write_sep(
            "=",
            f"required verifier lane had {len(skipped)} unexpected skip(s)",
            red=True,
        )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep the existing deterministic suite in the explicit unit lane.

    New PostgreSQL, real-lifespan, and process E2E tests opt into their own
    marker. They are deliberately not also marked as unit.
    """

    for item in items:
        if any(item.get_closest_marker(name) for name in _EXECUTION_MARKERS):
            continue
        if item.get_closest_marker("unit") is None:
            item.add_marker(pytest.mark.unit)


@pytest.fixture
def real_lifespan_client() -> type[RealLifespanASGITestClient]:
    """Return a client that executes ASGI startup/shutdown on one event loop.

    Callers must use it as a context manager. The legacy lightweight client
    remains installed below for existing route-only tests.
    """

    return RealLifespanASGITestClient


class RealLifespanASGITestClient:
    """Synchronous HTTP facade over a real ASGI lifespan and AsyncClient.

    Starlette's client is globally replaced for the legacy route-only suite
    below. This separate client therefore owns an event-loop thread, sends the
    ASGI lifespan protocol itself, and keeps startup state alive across all
    requests made inside its context manager.
    """

    __test__ = False

    def __init__(
        self,
        app: Any,
        *,
        base_url: str = "http://testserver",
        timeout_seconds: float = 10.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.app = app
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.headers = headers
        self._thread = threading.Thread(
            target=self._run_session,
            name="sleepagent-asgi-lifespan-test",
            daemon=True,
        )
        self._startup_result: queue.Queue[BaseException | None] = queue.Queue(
            maxsize=1
        )
        self._commands: queue.Queue[
            tuple[str, Any, queue.Queue[Any]]
        ] = queue.Queue()
        self._client: httpx.AsyncClient | None = None
        self._lifespan_task: asyncio.Task[None] | None = None
        self._lifespan_receive: asyncio.Queue[dict[str, Any]] | None = None
        self._lifespan_send: asyncio.Queue[dict[str, Any]] | None = None
        self._lifespan_state: dict[str, Any] = {}
        self._entered = False

    def __enter__(self) -> "RealLifespanASGITestClient":
        if self._entered:
            raise RuntimeError("real lifespan client cannot be entered twice")
        self._thread.start()
        outcome = self._startup_result.get(timeout=self.timeout_seconds)
        if outcome is not None:
            self._thread.join(timeout=self.timeout_seconds)
            raise outcome
        self._entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        result: queue.Queue[Any] = queue.Queue(maxsize=1)
        try:
            self._commands.put(("shutdown", None, result))
            outcome = result.get(timeout=self.timeout_seconds)
            if isinstance(outcome, BaseException):
                raise outcome
        finally:
            self._entered = False
            self._thread.join(timeout=self.timeout_seconds)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if not self._entered or self._client is None:
            raise RuntimeError(
                "real lifespan client must be used as a context manager"
            )
        result: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._commands.put(
            ("request", (method, url, kwargs), result)
        )
        outcome = result.get(timeout=self.timeout_seconds)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, httpx.Response)
        return outcome

    def _run_session(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        try:
            await self._startup()
        except BaseException as exc:
            self._startup_result.put(exc)
            return
        self._startup_result.put(None)
        while True:
            try:
                command, payload, result = self._commands.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
                continue
            try:
                if command == "request":
                    assert self._client is not None
                    method, url, kwargs = payload
                    outcome = await self._client.request(
                        method, url, **kwargs
                    )
                elif command == "shutdown":
                    outcome = await self._shutdown()
                else:
                    raise RuntimeError(f"unknown test client command: {command}")
            except BaseException as exc:
                result.put(exc)
            else:
                result.put(outcome)
            if command == "shutdown":
                return

    async def _startup(self) -> None:
        self._lifespan_receive = asyncio.Queue()
        self._lifespan_send = asyncio.Queue()

        async def receive() -> dict[str, Any]:
            assert self._lifespan_receive is not None
            return await self._lifespan_receive.get()

        async def send(message: dict[str, Any]) -> None:
            assert self._lifespan_send is not None
            await self._lifespan_send.put(message)

        self._lifespan_task = asyncio.create_task(
            self.app(
                {
                    "type": "lifespan",
                    "asgi": {"version": "3.0", "spec_version": "2.0"},
                    "state": self._lifespan_state,
                },
                receive,
                send,
            )
        )
        await self._lifespan_receive.put({"type": "lifespan.startup"})
        message = await asyncio.wait_for(
            self._lifespan_send.get(), timeout=self.timeout_seconds
        )
        if message["type"] != "lifespan.startup.complete":
            detail = message.get("message", "ASGI startup failed")
            raise RuntimeError(str(detail))

        async def state_aware_app(
            scope: dict[str, Any],
            receive: Any,
            send: Any,
        ) -> None:
            request_scope = dict(scope)
            request_scope.setdefault("state", dict(self._lifespan_state))
            await self.app(request_scope, receive, send)

        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=state_aware_app),
            base_url=self.base_url,
            headers=self.headers,
            timeout=self.timeout_seconds,
        )

    async def _shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        if self._lifespan_task is None:
            return
        assert self._lifespan_receive is not None
        assert self._lifespan_send is not None
        await self._lifespan_receive.put({"type": "lifespan.shutdown"})
        message = await asyncio.wait_for(
            self._lifespan_send.get(), timeout=self.timeout_seconds
        )
        if message["type"] != "lifespan.shutdown.complete":
            detail = message.get("message", "ASGI shutdown failed")
            raise RuntimeError(str(detail))
        await self._lifespan_task


class LightweightASGIResponse:
    def __init__(
        self,
        *,
        status_code: int,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = body
        self.text = body.decode("utf-8")
        self.headers = headers or {}

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP request failed with status {self.status_code}.")


class LightweightASGITestClient:
    """Small sync ASGI client for route tests in this Python/anyio environment."""

    __test__ = False

    def __init__(self, app: Any, *args: Any, **kwargs: Any) -> None:
        self.app = app

    def __enter__(self) -> "LightweightASGITestClient":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def close(self) -> None:
        return None

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **_: Any,
    ) -> LightweightASGIResponse:
        return self.request("GET", url, params=params, headers=headers)

    def post(
        self,
        url: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **_: Any,
    ) -> LightweightASGIResponse:
        return self.request("POST", url, json=json, params=params, headers=headers)

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = None,
        content: bytes | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **_: Any,
    ) -> LightweightASGIResponse:
        return asyncio.run(
            self._request_async(
                method,
                url,
                json_body=json,
                raw_body=content,
                params=params,
                headers=headers or {},
            )
        )

    async def _request_async(
        self,
        method: str,
        url: str,
        *,
        json_body: Any | None,
        raw_body: bytes | None,
        params: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> LightweightASGIResponse:
        parsed = urlparse(url)
        path = parsed.path or url
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if params:
            query_pairs.extend((key, value) for key, value in params.items())
        query_string = urlencode(query_pairs, doseq=True).encode("utf-8")
        body = (
            raw_body
            if raw_body is not None
            else (
                json.dumps(json_body, ensure_ascii=False).encode("utf-8")
                if json_body is not None
                else b""
            )
        )
        raw_headers = [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in headers.items()
        ]
        if json_body is not None and "content-type" not in {
            key.lower() for key in headers
        }:
            raw_headers.append((b"content-type", b"application/json"))
        messages: list[dict[str, Any]] = []
        request_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await self.app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": query_string,
                "headers": raw_headers,
                "client": ("testclient", 1),
                "server": ("testserver", 80),
                "scheme": parsed.scheme or "http",
                "root_path": "",
            },
            receive,
            send,
        )
        status = next(
            int(message["status"])
            for message in messages
            if message["type"] == "http.response.start"
        )
        response_headers: dict[str, str] = {}
        for message in messages:
            if message["type"] != "http.response.start":
                continue
            for raw_key, raw_value in message.get("headers", []):
                response_headers[raw_key.decode("latin-1")] = raw_value.decode("latin-1")
        response_body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        return LightweightASGIResponse(
            status_code=status,
            body=response_body,
            headers=response_headers,
        )


def pytest_configure() -> None:
    import fastapi.testclient

    # The legacy test app may expose deterministic non-production adapters, but
    # every Agent task route is canonical Product Episode only.
    os.environ.setdefault("SLEEPAGENT_RADAR_AGENT_DEV_MODE", "true")
    os.environ.setdefault("SLEEPAGENT_DEPLOYMENT_MODE", "test")
    os.environ.setdefault("SLEEPAGENT_PRODUCT_RADAR_PROVIDER_MODE", "fake")
    os.environ.setdefault(
        "SLEEPAGENT_PRODUCT_RADAR_NAMESPACE",
        "replay:pytest-product-radar",
    )
    fastapi.testclient.TestClient = LightweightASGITestClient
