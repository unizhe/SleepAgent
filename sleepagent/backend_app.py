"""The only composable FastAPI application factory for the backend."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import zlib
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Iterable
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from sleepagent.backend_runtime import SleepBackendRuntime
from sleepagent.backend_settings import ApiSurface, ProcessRole
from sleepagent.demo_api import DemoApiError, create_demo_router
from sleepagent.observability import (
    backend_metrics_snapshot,
    log_event,
    record_backend_http,
    record_backend_signal,
)
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
    async def livez() -> dict[str, str]:
        return {"status": "alive"}

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
    app.add_exception_handler(DemoApiError, _demo_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_middleware(
        BoundedRequestMiddleware,
        max_compressed_bytes=runtime.settings.max_compressed_body_bytes,
        max_decompressed_bytes=runtime.settings.max_decompressed_body_bytes,
        max_json_depth=runtime.settings.max_json_depth,
        max_json_members=runtime.settings.max_json_members,
        request_timeout_seconds=runtime.settings.request_timeout_seconds,
        data_mode=runtime.settings.data_mode.value,
    )
    app.add_middleware(
        ReplayWatermarkMiddleware,
        data_mode=runtime.settings.data_mode.value,
    )
    app.add_middleware(BackendTelemetryMiddleware)
    # Correlation is outermost so even body-limit failures get the same
    # server-generated id in their response header and typed error body.
    app.add_middleware(CorrelationIdMiddleware)
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

    @internal.get("/metrics")
    def metrics(
        x_internal_token: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(x_internal_token)
        result = backend_metrics_snapshot()
        reader = getattr(runtime.services.internal_status, "operational_metrics", None)
        if reader is not None:
            result["durable"] = reader()
        return result

    @internal.get("/reconciliation/{operation_id}")
    def reconciliation_status(
        operation_id: str,
        x_internal_token: str | None = Header(default=None),
    ) -> dict[str, object]:
        authorize(x_internal_token)
        reader = getattr(
            runtime.services.internal_status,
            "reconciliation_status",
            None,
        )
        if reader is None:
            raise HTTPException(
                status_code=503,
                detail="reconciliation status is unavailable",
            )
        result = reader(operation_id)
        if result is None:
            raise HTTPException(status_code=404, detail="reconciliation not found")
        return dict(result)

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


class ReplayWatermarkMiddleware:
    """Inject server-owned provenance headers on every public v1 response."""

    def __init__(self, app: ASGIApp, *, data_mode: str) -> None:
        if data_mode not in {"live", "replay"}:
            raise ValueError("unsupported backend data mode")
        self.app = app
        self.data_mode = data_mode

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(
            "/api/v1"
        ):
            await self.app(scope, receive, send)
            return

        async def send_with_watermark(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower()
                    not in {
                        b"x-sleepagent-data-mode",
                        b"x-sleepagent-synthetic-non-release",
                    }
                ]
                headers.extend(
                    (
                        (b"x-sleepagent-data-mode", self.data_mode.encode("ascii")),
                        (
                            b"x-sleepagent-synthetic-non-release",
                            b"true" if self.data_mode == "replay" else b"false",
                        ),
                    )
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_watermark)


class BackendTelemetryMiddleware:
    """Emit bounded, PHI-free HTTP metrics and completion logs."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status_code = 500

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        finally:
            method = str(scope.get("method", "UNKNOWN")).upper()
            route_group = _safe_route_group(str(scope.get("path", "")))
            elapsed_ms = max(0, int((time.monotonic() - started) * 1_000))
            record_backend_http(
                method=method,
                route_group=route_group,
                status_code=status_code,
                elapsed_ms=elapsed_ms,
            )
            if status_code in {401, 403}:
                record_backend_signal(category="auth", outcome="denied")
            log_event(
                "backend_http_completed",
                method=method,
                route_group=route_group,
                status_class=f"{status_code // 100}xx",
                elapsed_ms=elapsed_ms,
                correlation_id=_scope_correlation_id(scope),
            )


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
        data_mode: str = "live",
    ) -> None:
        self.app = app
        self.max_compressed_bytes = max_compressed_bytes
        self.max_decompressed_bytes = max_decompressed_bytes
        self.max_json_depth = max_json_depth
        self.max_json_members = max_json_members
        self.request_timeout_seconds = request_timeout_seconds
        self.data_mode = data_mode

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)
        declared = headers.get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
                if declared_size < 0:
                    raise ValueError
                if declared_size > self.max_compressed_bytes:
                    await _send_error(
                        send, 413, "request_too_large",
                        path=str(scope.get("path", "")),
                        data_mode=self.data_mode,
                        correlation_id=_scope_correlation_id(scope),
                    )
                    return
            except ValueError:
                await _send_error(
                    send, 400, "invalid_content_length",
                    path=str(scope.get("path", "")),
                    data_mode=self.data_mode,
                    correlation_id=_scope_correlation_id(scope),
                )
                return
        response_started = False

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.request_timeout_seconds

        def ensure_before_deadline() -> None:
            if loop.time() >= deadline:
                raise TimeoutError

        try:
            async with asyncio.timeout(self.request_timeout_seconds):
                if (
                    str(scope.get("method", "")).upper()
                    in {"GET", "HEAD", "OPTIONS"}
                    and declared is None
                    and "transfer-encoding" not in headers
                ):
                    await self.app(scope, receive, tracked_send)
                    return

                raw = await self._read(receive)
                ensure_before_deadline()
                body = self._decode(raw, headers.get("content-encoding"))
                ensure_before_deadline()
                if _is_json(headers.get("content-type")) and body:
                    _validate_json(
                        body,
                        max_depth=self.max_json_depth,
                        max_members=self.max_json_members,
                    )
                    ensure_before_deadline()

                sent = False

                async def replay_receive() -> dict[str, Any]:
                    nonlocal sent
                    if sent:
                        return {"type": "http.disconnect"}
                    sent = True
                    return {
                        "type": "http.request",
                        "body": body,
                        "more_body": False,
                    }

                bounded_scope = _replace_body_headers(scope, len(body))
                state = dict(bounded_scope.get("state") or {})
                state["sleepagent_validated_request_body"] = body
                bounded_scope = {**bounded_scope, "state": state}
                await self.app(bounded_scope, replay_receive, tracked_send)
        except RequestBoundaryError as exc:
            if not response_started:
                await _send_error(
                    send, exc.status_code, exc.code,
                    path=str(scope.get("path", "")),
                    data_mode=self.data_mode,
                    correlation_id=_scope_correlation_id(scope),
                )
        except TimeoutError:
            if not response_started:
                await _send_error(
                    send, 504, "request_deadline_exceeded",
                    path=str(scope.get("path", "")),
                    data_mode=self.data_mode,
                    correlation_id=_scope_correlation_id(scope),
                )

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
            if not decompressor.eof or decompressor.unused_data:
                raise RequestBoundaryError(400, "invalid_gzip_body")
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


async def _send_error(
    send,
    status_code: int,
    code: str,
    *,
    path: str,
    data_mode: str,
    correlation_id: str,
) -> None:
    product = path.startswith("/product/")
    demo = path.startswith("/demo/")
    body = json.dumps(
        {
            "schema_version": (
                "product_error.v1" if product else
                "demo_error.v1" if demo else
                "backend_error.v1"
            ),
            **(
                {
                    "data_mode": data_mode,
                    "synthetic_non_release": data_mode == "replay",
                }
                if product or demo
                else {}
            ),
            "code": code,
            "message": code.replace("_", " "),
            "retryable": status_code >= 500,
            "correlation_id": correlation_id,
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


def _scope_correlation_id(scope: dict[str, Any]) -> str:
    state = scope.setdefault("state", {})
    value = str(state.get("correlation_id") or uuid4())
    state["correlation_id"] = value
    return value


async def _product_error_handler(
    request: Request,
    exc: ProductApiError,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "schema_version": "product_error.v1",
            "data_mode": request.app.state.runtime.settings.data_mode.value,
            "synthetic_non_release": (
                request.app.state.runtime.settings.data_mode.value == "replay"
            ),
            "code": exc.code,
            "message": str(exc),
            "retryable": exc.retryable,
            "correlation_id": getattr(request.state, "correlation_id", None),
        },
    )


async def _demo_error_handler(
    request: Request,
    exc: DemoApiError,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "schema_version": "demo_error.v1",
            "data_mode": "replay",
            "synthetic_non_release": True,
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "correlation_id": getattr(request.state, "correlation_id", None),
        },
    )


async def _validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    if request.url.path.startswith("/api/v1/"):
        from sleepagent.sleep_api.router import sleep_api_validation_handler

        return await sleep_api_validation_handler(request, exc)
    del exc
    is_demo = request.url.path.startswith("/demo/")
    is_product = request.url.path.startswith("/product/")
    data_mode = request.app.state.runtime.settings.data_mode.value
    return JSONResponse(
        status_code=422,
        content={
            "schema_version": (
                "demo_error.v1" if is_demo else
                "product_error.v1" if is_product else
                "backend_error.v1"
            ),
            **(
                {
                    "data_mode": data_mode,
                    "synthetic_non_release": data_mode == "replay",
                }
                if is_demo or is_product
                else {}
            ),
            "code": "invalid_request",
            "message": "Request validation failed.",
            "retryable": False,
            "correlation_id": getattr(request.state, "correlation_id", None),
        },
    )


def _safe_route_group(path: str) -> str:
    if path == "/livez":
        return "livez"
    for prefix, name in (
        ("/api/v1/", "public_v1"),
        ("/product/sleep/", "product_sleep"),
        ("/demo/v1/", "demo_v1"),
        ("/internal/", "internal"),
    ):
        if path.startswith(prefix):
            return name
    return "unknown"


__all__ = [
    "BoundedRequestMiddleware",
    "BackendTelemetryMiddleware",
    "CorrelationIdMiddleware",
    "ReplayWatermarkMiddleware",
    "RequestBoundaryError",
    "create_sleep_backend_app",
]
