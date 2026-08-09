"""The only composable FastAPI application factory for the backend."""

from __future__ import annotations

import asyncio
import json
import secrets
import zlib
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Iterable
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from sleepagent.backend_runtime import SleepBackendRuntime
from sleepagent.backend_settings import ApiSurface, ProcessRole
from sleepagent.demo_api import create_demo_router
from sleepagent.product_api.router import create_product_router
from sleepagent.product_api.service import ProductApiError, ProductApiService


ASGIApp = Callable[
    [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]],
    Awaitable[None],
]


def create_sleep_backend_app(
    runtime: SleepBackendRuntime,
    enabled_surfaces: Iterable[ApiSurface] | None = None,
) -> FastAPI:
    """Compose public, product, demo and isolated internal surfaces.

    Construction validates that every enabled surface has an explicit service;
    it never silently falls back to an in-memory authority.
    """

    if runtime.settings.process_role != ProcessRole.API:
        raise ValueError("FastAPI app requires an API capability profile")
    surfaces = frozenset(
        runtime.settings.enabled_surfaces
        if enabled_surfaces is None
        else enabled_surfaces
    )
    unauthorized = surfaces - runtime.settings.enabled_surfaces
    if unauthorized:
        raise ValueError(
            "requested surfaces were not enabled by settings: "
            + ", ".join(sorted(item.value for item in unauthorized))
        )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        del application
        await runtime.start()
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(
        title="SleepAgent Backend",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.runtime = runtime

    @app.get("/livez", include_in_schema=False)
    def livez() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, object]:
        """Preserve the non-Agent health surface across the runtime cutover."""

        return runtime.readiness()

    @app.get("/status", include_in_schema=False)
    def status() -> dict[str, object]:
        """Preserve non-Agent status without mounting a legacy API surface."""

        return {
            "readiness": runtime.readiness(),
            "dependencies": runtime.dependency_manifest().model_dump(mode="json"),
        }

    if ApiSurface.PUBLIC_V1 in surfaces:
        provider = runtime.services.public_v1_runtime_provider
        if provider is None:
            raise ValueError("public_v1 surface requires a runtime provider")
        from sleepagent.sleep_api.router import install_sleep_api

        install_sleep_api(app, provider)  # type: ignore[arg-type]

    if ApiSurface.PRODUCT in surfaces:
        product = runtime.services.product
        if not isinstance(product, ProductApiService):
            raise ValueError("product surface requires ProductApiService")
        app.include_router(create_product_router(lambda: product))

    if ApiSurface.DEMO in surfaces:
        controller = runtime.services.demo
        if controller is None:
            raise ValueError("demo surface requires a durable DemoController")
        token = runtime.settings.demo_controller_token
        if token is None:  # settings validation should make this unreachable
            raise ValueError("demo surface requires a controller token")
        app.include_router(
            create_demo_router(
                controller,  # type: ignore[arg-type]
                token=token.get_secret_value(),
            )
        )

    if ApiSurface.INTERNAL in surfaces:
        app.mount("/internal", _internal_app(runtime))

    app.add_exception_handler(ProductApiError, _product_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(
        BoundedRequestMiddleware,
        max_compressed_bytes=runtime.settings.max_compressed_body_bytes,
        max_decompressed_bytes=runtime.settings.max_decompressed_body_bytes,
        max_json_depth=runtime.settings.max_json_depth,
        max_json_members=runtime.settings.max_json_members,
        request_timeout_seconds=runtime.settings.request_timeout_seconds,
    )
    return app


def _internal_app(runtime: SleepBackendRuntime) -> FastAPI:
    internal = FastAPI(
        title="SleepAgent internal operations",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    configured = runtime.settings.internal_auth_token
    if configured is None:
        raise ValueError("internal surface requires a token")
    expected = configured.get_secret_value()

    def authorize(x_internal_token: str | None = Header(default=None)) -> None:
        if x_internal_token is None or not secrets.compare_digest(
            x_internal_token,
            expected,
        ):
            raise HTTPException(status_code=401, detail="internal credential required")

    @internal.get("/readyz")
    def readyz(
        x_internal_token: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(x_internal_token)
        return runtime.readiness()

    @internal.get("/status")
    def status(
        x_internal_token: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(x_internal_token)
        return {
            "readiness": runtime.readiness(),
            "dependencies": runtime.dependency_manifest().model_dump(mode="json"),
        }

    return internal


class CorrelationIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        correlation_id = str(uuid4())
        state = scope.setdefault("state", {})
        state["correlation_id"] = correlation_id

        async def send_with_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append(
                    (b"x-correlation-id", correlation_id.encode("ascii"))
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_id)


class BoundedRequestMiddleware:
    """Bound and validate request bytes before FastAPI parses a body."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_compressed_bytes: int,
        max_decompressed_bytes: int,
        max_json_depth: int,
        max_json_members: int,
        request_timeout_seconds: float,
    ) -> None:
        self.app = app
        self.max_compressed_bytes = max_compressed_bytes
        self.max_decompressed_bytes = max_decompressed_bytes
        self.max_json_depth = max_json_depth
        self.max_json_members = max_json_members
        self.request_timeout_seconds = request_timeout_seconds

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_compressed_bytes:
                    await _send_error(send, 413, "request_too_large")
                    return
            except ValueError:
                await _send_error(send, 400, "invalid_content_length")
                return
        try:
            raw = await self._read(receive)
            body = self._decode(raw, headers.get("content-encoding"))
            if _is_json(headers.get("content-type")) and body:
                _validate_json(
                    body,
                    max_depth=self.max_json_depth,
                    max_members=self.max_json_members,
                )
        except RequestBoundaryError as exc:
            await _send_error(send, exc.status_code, exc.code)
            return

        sent = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        scope = _replace_body_headers(scope, len(body))
        response_started = False

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            async with asyncio.timeout(self.request_timeout_seconds):
                await self.app(scope, replay_receive, tracked_send)
        except TimeoutError:
            if not response_started:
                await _send_error(send, 504, "request_deadline_exceeded")

    async def _read(self, receive) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise RequestBoundaryError(400, "request_disconnected")
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_compressed_bytes:
                raise RequestBoundaryError(413, "request_too_large")
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)

    def _decode(self, raw: bytes, encoding: str | None) -> bytes:
        normalized = (encoding or "identity").strip().lower()
        if normalized in {"", "identity"}:
            if len(raw) > self.max_decompressed_bytes:
                raise RequestBoundaryError(413, "request_too_large")
            return raw
        if normalized != "gzip":
            raise RequestBoundaryError(415, "unsupported_content_encoding")
        try:
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decoded = decompressor.decompress(
                raw,
                self.max_decompressed_bytes + 1,
            )
            if decompressor.unconsumed_tail:
                raise RequestBoundaryError(413, "request_too_large")
            decoded += decompressor.flush()
        except (OSError, zlib.error) as exc:
            raise RequestBoundaryError(400, "invalid_gzip_body") from exc
        if len(decoded) > self.max_decompressed_bytes:
            raise RequestBoundaryError(413, "request_too_large")
        return decoded


class RequestBoundaryError(ValueError):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def _headers(scope: dict[str, Any]) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _replace_body_headers(scope: dict[str, Any], length: int) -> dict[str, Any]:
    headers = [
        (key, value)
        for key, value in scope.get("headers", [])
        if key.lower() not in {b"content-length", b"content-encoding"}
    ]
    headers.append((b"content-length", str(length).encode("ascii")))
    return {**scope, "headers": headers}


def _is_json(content_type: str | None) -> bool:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    return media_type == "application/json" or media_type.endswith("+json")


def _validate_json(body: bytes, *, max_depth: int, max_members: int) -> None:
    try:
        value = json.loads(
            body,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RequestBoundaryError(400, "invalid_json") from exc
    members = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > max_depth:
            raise RequestBoundaryError(400, "json_too_deep")
        if isinstance(item, dict):
            members += len(item)
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            members += len(item)
            stack.extend((child, depth + 1) for child in item)
        if members > max_members:
            raise RequestBoundaryError(400, "json_too_many_members")


async def _send_error(send, status_code: int, code: str) -> None:
    body = json.dumps(
        {
            "schema_version": "backend_error.v1",
            "code": code,
            "message": code.replace("_", " "),
            "retryable": status_code >= 500,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _product_error_handler(
    request: Request,
    exc: ProductApiError,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "schema_version": "product_error.v1",
            "code": exc.code,
            "message": str(exc),
            "retryable": exc.retryable,
            "correlation_id": getattr(request.state, "correlation_id", None),
        },
    )


async def _validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    del exc
    return JSONResponse(
        status_code=422,
        content={
            "schema_version": "backend_error.v1",
            "code": "request_validation_failed",
            "message": "Request validation failed.",
            "retryable": False,
            "correlation_id": getattr(request.state, "correlation_id", None),
        },
    )


__all__ = [
    "BoundedRequestMiddleware",
    "CorrelationIdMiddleware",
    "RequestBoundaryError",
    "create_sleep_backend_app",
]
