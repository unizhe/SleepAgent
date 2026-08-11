from __future__ import annotations

import asyncio
import gzip
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from sleepagent.backend_app import BoundedRequestMiddleware, create_sleep_backend_app
from sleepagent.backend_runtime import (
    DatabaseAttestation,
    RuntimeServices,
    SleepBackendRuntime,
    reset_active_runtime_for_tests,
)
from sleepagent.backend_settings import (
    ApiSurface,
    DataMode,
    DeploymentMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.demo_api import (
    DemoAcceptedResponse,
    DemoTraceResponse,
    ScenarioClockResponse,
)
from sleepagent.product_api.contracts import InteractionStatusResponse
from sleepagent.product_api.contracts import InteractionStartRequest
from sleepagent.product_api.service import (
    ProductApiService,
    ProductRequestContext,
)


pytestmark = pytest.mark.unit
UTC = timezone.utc
INTERNAL_TOKEN = "internal-controller-token-32-bytes"
DEMO_TOKEN = "demo-controller-token-that-is-32-bytes"


class Pool:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def open(self) -> None:
        self.calls.append("open")

    def close(self) -> None:
        self.calls.append("close")


class Identity:
    def resolve(self, request, *, body: bytes, purpose: str):
        del request, body, purpose
        return ProductRequestContext(
            service_principal_id="bff",
            actor_id="actor",
            binding_id="binding",
            subject_id="subject",
            role="elder",
            effective_scopes=frozenset(
                {
                    "product:sleep:today:read",
                    "product:sleep:trends:read",
                    "product:sleep:care:read",
                    "product:sleep:records:read",
                    "product:sleep:interaction:write",
                    "product:sleep:interaction:answer",
                    "product:sleep:care:confirm",
                    "product:sleep:feedback:write",
                    "product:sleep:operation:read",
                }
            ),
            namespace_id="replay:test",
            namespace_generation=1,
            data_mode="replay",
            run_id="run-test",
            arm_id="arm-test",
            purpose="sleep_care",
            authorization_epoch=1,
            privacy_epoch=1,
            retrieval_epoch=1,
            policy_sha256="a" * 64,
        )


class ProductBackend:
    def __init__(self) -> None:
        self.writes = 0

    def list_role_projections(self, context, **kwargs):
        del context, kwargs
        return (), None

    def reserve_command(self, context, **kwargs):
        del context, kwargs
        self.writes += 1
        return "01987654-3210-7abc-8def-0123456789ab"

    def get_operation(self, context, *, operation_id: str) -> InteractionStatusResponse | None:
        del context, operation_id
        return None


class Demo:
    def seed(self, *, request, idempotency_key):
        del request, idempotency_key
        return DemoAcceptedResponse(operation_id="seed-op", generation=1)

    def advance(self, *, request, idempotency_key):
        del request, idempotency_key
        return DemoAcceptedResponse(operation_id="advance-op", generation=1)

    def reset(self, *, request, idempotency_key):
        del request, idempotency_key
        return DemoAcceptedResponse(operation_id="reset-op", generation=2)

    def clock(self):
        return ScenarioClockResponse(
            scenario_time=datetime(2026, 8, 7, tzinfo=UTC),
            generation=1,
        )

    def trace(self, *, operation_id, cursor, limit):
        del operation_id, cursor, limit
        return DemoTraceResponse(generation=1, entries=())


def _runtime(
    *,
    surfaces: frozenset[ApiSurface],
    max_body: int = 4096,
) -> tuple[SleepBackendRuntime, Pool, ProductBackend]:
    settings = SleepBackendSettings(
        profile="test-replay",
        deployment_mode=DeploymentMode.TEST,
        process_role=ProcessRole.API,
        data_mode=DataMode.REPLAY,
        database_dsn="postgresql://api:secret@postgres/replay_db",
        database_identity="replay_db",
        database_role="sleepagent_api_replay",
        service_principal_id="sleepagent-api-test",
        database_scope=DataMode.REPLAY,
        namespace_prefixes=("replay:test",),
        enabled_surfaces=surfaces,
        signing_key_ref="test:signing",
        encryption_key_ref="test:encryption",
        internal_auth_token=INTERNAL_TOKEN,
        demo_controller_token=DEMO_TOKEN,
        max_compressed_body_bytes=max_body,
        max_decompressed_body_bytes=max_body,
    )
    pool = Pool()
    backend = ProductBackend()
    product = ProductApiService(
        identity_resolver=Identity(),
        backend=backend,
    )
    runtime = SleepBackendRuntime(
        settings,
        pool=pool,
        uow_factory=object(),
        attestor=lambda: DatabaseAttestation(
            database_identity="replay_db",
            database_role="sleepagent_api_replay",
            schema_version=1,
            migrations_clean=True,
        ),
        services=RuntimeServices(product=product, demo=Demo()),
    )
    return runtime, pool, backend


def setup_function() -> None:
    reset_active_runtime_for_tests()


def teardown_function() -> None:
    reset_active_runtime_for_tests()


def test_lifespan_opens_pool_without_starting_any_worker() -> None:
    runtime, pool, _ = _runtime(surfaces=frozenset({ApiSurface.PRODUCT}))
    app = create_sleep_backend_app(runtime)

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            assert pool.calls == ["open"]
            assert runtime.worker_handlers == {}

    asyncio.run(exercise())
    assert pool.calls == ["open", "close"]


def test_product_surface_exposes_async_operation_contract_and_strict_dto() -> None:
    runtime, _, backend = _runtime(surfaces=frozenset({ApiSurface.PRODUCT}))
    app = create_sleep_backend_app(runtime)

    operation = app.openapi()["paths"][
        "/product/sleep/interactions/start"
    ]["post"]
    assert operation["responses"]["202"]
    assert any(
        parameter["name"] == "Idempotency-Key"
        for parameter in operation["parameters"]
    )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        InteractionStartRequest.model_validate(
            {"intent": "morning_review", "actor_id": "self-reported"}
        )
    assert backend.writes == 0


def test_body_limiter_rejects_plain_and_gzip_bombs_before_parsing() -> None:
    huge = ('{"intent":"' + "x" * 2_000 + '"}').encode()
    compressed = gzip.compress(huge)

    async def downstream(scope, receive, send) -> None:
        del scope
        await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    limiter = BoundedRequestMiddleware(
        downstream,
        max_compressed_bytes=1024,
        max_decompressed_bytes=1024,
        max_json_depth=10,
        max_json_members=100,
        request_timeout_seconds=1,
    )

    async def status(body: bytes, *, encoding: str | None = None) -> int:
        messages: list[dict[str, Any]] = []
        request_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal request_sent
            if request_sent:
                return {"type": "http.disconnect"}
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        headers = [(b"content-type", b"application/json")]
        if encoding:
            headers.append((b"content-encoding", encoding.encode("ascii")))
        await limiter(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": headers,
            },
            receive,
            send,
        )
        return next(
            message["status"]
            for message in messages
            if message["type"] == "http.response.start"
        )

    assert asyncio.run(status(huge)) == 413
    assert asyncio.run(status(compressed, encoding="gzip")) == 413


def test_demo_is_token_protected_and_watermarked() -> None:
    runtime, _, _ = _runtime(
        surfaces=frozenset({ApiSurface.PRODUCT, ApiSurface.DEMO})
    )
    app = create_sleep_backend_app(runtime)
    route = next(
        item
        for item in app.routes
        if getattr(item, "path", None) == "/demo/v1/clock"
    )
    with pytest.raises(HTTPException) as denied:
        route.endpoint(demo_token=None)
    allowed = route.endpoint(demo_token=DEMO_TOKEN)

    assert denied.value.status_code == 401
    assert allowed.data_mode == "replay"
    assert allowed.synthetic_non_release is True


def test_internal_surface_is_not_in_public_openapi_and_requires_credential() -> None:
    runtime, _, _ = _runtime(
        surfaces=frozenset({ApiSurface.PRODUCT, ApiSurface.INTERNAL})
    )
    app = create_sleep_backend_app(runtime)
    paths = app.openapi()["paths"]
    mounted = next(
        route.app
        for route in app.routes
        if getattr(route, "path", None) == "/internal"
    )
    ready = next(
        route
        for route in mounted.routes
        if getattr(route, "path", None) == "/readyz"
    )
    with pytest.raises(HTTPException) as denied:
        ready.endpoint(x_internal_token=None)
    allowed = ready.endpoint(x_internal_token=INTERNAL_TOKEN)

    assert not any(path.startswith("/internal") for path in paths)
    assert denied.value.status_code == 401
    assert allowed["ready"] is False


def test_factory_refuses_a_surface_without_explicit_capability() -> None:
    runtime, _, _ = _runtime(surfaces=frozenset({ApiSurface.PRODUCT}))

    with pytest.raises(ValueError, match="not enabled"):
        create_sleep_backend_app(
            runtime,
            enabled_surfaces={ApiSurface.PRODUCT, ApiSurface.DEMO},
        )
