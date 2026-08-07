from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse


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

    # Legacy and dynamic runtimes are test-only compatibility surfaces. The
    # application default is the four-role product runtime.
    os.environ.setdefault("SLEEPAGENT_RADAR_AGENT_RUNTIME_MODE", "hybrid")
    os.environ.setdefault("SLEEPAGENT_RADAR_AGENT_DEV_MODE", "true")
    os.environ.setdefault("SLEEPAGENT_DEPLOYMENT_MODE", "test")
    os.environ.setdefault("SLEEPAGENT_PRODUCT_RADAR_PROVIDER_MODE", "fake")
    os.environ.setdefault(
        "SLEEPAGENT_PRODUCT_RADAR_NAMESPACE",
        "replay:pytest-product-radar",
    )
    fastapi.testclient.TestClient = LightweightASGITestClient
