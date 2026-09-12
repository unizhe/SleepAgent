from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import time as wall_time
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from sleepagent.app import (
    BoundedRequestMiddleware,
    CorrelationIdMiddleware,
    ReplayWatermarkMiddleware,
    _demo_error_handler,
    _product_error_handler,
    create_sleep_backend_app,
)
from sleepagent.process import (
    DatabaseAttestation,
    RuntimeServices,
    SleepBackendRuntime,
)
from tests.support.runtime_fixtures import (
    reset_backend_runtime_state as reset_active_runtime_for_tests,
)
from sleepagent.config import (
    ApiSurface,
    DataMode,
    DeploymentMode,
    ProcessRole,
    SleepBackendSettings,
)
from sleepagent.api.demo import (
    DemoAcceptedResponse,
    DemoApiError,
    DemoTechnicalTraceResponse,
    DemoTraceResponse,
    ScenarioClockResponse,
)
from sleepagent.api.product_contracts import InteractionStatusResponse
from sleepagent.api.product_contracts import InteractionStartRequest
from sleepagent.api.product import (
    ProductApiError,
    ProductApiService,
    ProductRequestContext,
)
from sleepagent.persistence.migrations import (
    EXPECTED_MIGRATION_IDENTITIES,
    LATEST_SCHEMA_VERSION,
    MIGRATION_MANIFEST_SHA256,
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
        self.reservations: list[dict[str, Any]] = []

    def list_role_projections(self, context, **kwargs):
        del context, kwargs
        return (), None

    def reserve_command(self, context, **kwargs):
        del context
        self.writes += 1
        self.reservations.append(dict(kwargs))
        return "01987654-3210-7abc-8def-0123456789ab"

    def get_operation(self, context, *, operation_id: str) -> InteractionStatusResponse | None:
        del context, operation_id
        return None


class Demo:
    def seed(self, *, request, idempotency_key):
        del request, idempotency_key
        return DemoAcceptedResponse(
            operation_id="seed-op",
            generation=1,
            status_url="/demo/v1/operations/seed-op",
        )

    def advance(self, *, request, idempotency_key):
        del request, idempotency_key
        return DemoAcceptedResponse(
            operation_id="advance-op",
            generation=1,
            status_url="/demo/v1/operations/advance-op",
        )

    def reset(self, *, request, idempotency_key):
        del request, idempotency_key
        return DemoAcceptedResponse(
            operation_id="reset-op",
            generation=2,
            status_url="/demo/v1/operations/reset-op",
        )

    def clock(self):
        return ScenarioClockResponse(
            scenario_time=datetime(2026, 8, 7, tzinfo=UTC),
            generation=1,
        )

    def trace(self, *, operation_id, cursor, limit):
        del operation_id, cursor, limit
        return DemoTraceResponse(generation=1, entries=())

    def technical_trace(self, *, operation_id):
        return DemoTechnicalTraceResponse(
            root_operation_id=operation_id,
            namespace_generation=1,
            run_id="run-test",
            arm_id="arm-test",
            subject_id="subject-test",
            journey_state="succeeded",
            product_operation_count=1,
            product_attempt_count=1,
            fast_path_succeeded_count=1,
        )


def test_demo_technical_trace_accepts_postgres_json_array_shapes() -> None:
    response = DemoTechnicalTraceResponse.model_validate(
        {
            "root_operation_id": "root-op",
            "namespace_generation": 1,
            "run_id": "run-test",
            "arm_id": "arm-test",
            "subject_id": "subject-test",
            "journey_state": "succeeded",
            "product_operation_count": 1,
            "product_attempt_count": 1,
            "fast_path_succeeded_count": 1,
            "product_attempts": [{"attempt_state": "committed"}],
            "durable_invocations": [],
            "habit_revisions": [],
            "memory_revisions": [],
            "memory_read_receipts": [],
        }
    )

    assert response.product_attempts == [{"attempt_state": "committed"}]


class InternalStatus:
    def operational_metrics(self):
        return {
            "schema_version": "sleepagent_durable_operational_metrics.v2",
            "status": "healthy",
            "queues": [],
            "product_attempts": [],
            "safety": [],
        }

    def reconciliation_status(self, operation_id: str):
        if operation_id == "reconciliation-1":
            return {
                "schema_version": "internal_reconciliation_status.v1",
                "operation_id": operation_id,
                "operation_type": "delivery_reconciliation",
                "status": "succeeded",
                "resolution": "known_delivered",
            }
        return None


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
            schema_version=LATEST_SCHEMA_VERSION,
            migrations_clean=True,
            migration_manifest_sha256=MIGRATION_MANIFEST_SHA256,
            applied_migration_identities=EXPECTED_MIGRATION_IDENTITIES,
        ),
        services=RuntimeServices(
            product=product,
            demo=Demo(),
            internal_status=InternalStatus(),
        ),
    )
    return runtime, pool, backend


def setup_function() -> None:
    reset_active_runtime_for_tests()


def teardown_function() -> None:
    reset_active_runtime_for_tests()


def test_lifespan_opens_pool_without_starting_any_worker() -> None:
    runtime, pool, _ = _runtime(
        surfaces=frozenset({ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT})
    )
    app = create_sleep_backend_app(
        runtime,
        enabled_surfaces={ApiSurface.PRODUCT},
    )

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            assert pool.calls == ["open"]
            assert runtime.worker_handlers == {}

    asyncio.run(exercise())
    assert pool.calls == ["open", "close"]


@pytest.mark.asgi_lifespan
def test_real_asgi_body_replay_and_typed_oversize_error(
    real_lifespan_client,
) -> None:
    def bounded_app(max_body: int) -> FastAPI:
        app = FastAPI()

        @app.post("/demo/v1/echo")
        async def echo(payload: dict[str, Any]) -> dict[str, Any]:
            return payload

        app.add_middleware(
            BoundedRequestMiddleware,
            max_compressed_bytes=max_body,
            max_decompressed_bytes=max_body,
            max_json_depth=8,
            max_json_members=100,
            request_timeout_seconds=2.0,
            data_mode="replay",
        )
        app.add_middleware(CorrelationIdMiddleware)
        return app

    valid_app = bounded_app(4096)
    payload = {
        "artifact_family": "canonical-replay-fixtures",
        "scenario_id": "normal-one-night",
        "batch_size": 100,
    }
    with real_lifespan_client(valid_app) as client:
        accepted = client.post("/demo/v1/echo", json=payload)
    assert accepted.status_code == 200
    assert accepted.json() == payload

    limited_app = bounded_app(32)
    with real_lifespan_client(limited_app) as client:
        rejected = client.post("/demo/v1/echo", json=payload)
    assert rejected.status_code == 413
    assert rejected.json()["code"] == "request_too_large"
    assert rejected.json()["data_mode"] == "replay"
    assert rejected.headers["x-correlation-id"] == rejected.json()[
        "correlation_id"
    ]


def test_product_surface_exposes_async_operation_contract_and_strict_dto() -> None:
    runtime, _, backend = _runtime(
        surfaces=frozenset({ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT})
    )
    app = create_sleep_backend_app(
        runtime,
        enabled_surfaces={ApiSurface.PRODUCT},
    )

    operation = app.openapi()["paths"][
        "/product/sleep/interactions/start"
    ]["post"]
    assert operation["responses"]["202"]
    assert operation["responses"]["501"]
    assert any(
        parameter["name"] == "Idempotency-Key" and parameter["required"] is True
        for parameter in operation["parameters"]
    )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        InteractionStartRequest.model_validate(
            {"intent": "morning_review", "actor_id": "self-reported"}
        )
    assert backend.writes == 0


@pytest.mark.asgi_lifespan
def test_product_command_binds_auth_and_idempotency_to_validated_http_bytes(
    real_lifespan_client,
) -> None:
    runtime, _, backend = _runtime(
        surfaces=frozenset({ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT})
    )
    app = create_sleep_backend_app(runtime, enabled_surfaces={ApiSurface.PRODUCT})
    body = (
        b'{ "episode_revision_id": "rev-1", '
        b'"intent": "morning_review" }'
    )

    with real_lifespan_client(app) as client:
        response = client.post(
            "/product/sleep/interactions/start",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "raw-body-key",
            },
        )

    assert response.status_code == 202
    assert backend.reservations[0]["body_sha256"] == hashlib.sha256(body).hexdigest()


def test_today_openapi_is_state_and_role_discriminated() -> None:
    runtime, _, _ = _runtime(
        surfaces=frozenset({ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT})
    )
    schema = create_sleep_backend_app(
        runtime,
        enabled_surfaces={ApiSurface.PRODUCT},
    ).openapi()

    today = schema["paths"]["/product/sleep/today"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]
    assert today["discriminator"]["propertyName"] == "state"
    assert set(today["discriminator"]["mapping"]) == {
        "no_data",
        "ready",
        "degraded",
        "blocked",
    }


def test_read_routes_publish_distinct_versioned_schemas() -> None:
    runtime, _, _ = _runtime(
        surfaces=frozenset({ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT})
    )
    schema = create_sleep_backend_app(
        runtime,
        enabled_surfaces={ApiSurface.PRODUCT},
    ).openapi()

    expected = {
        "/product/sleep/trends": "ProductTrendsResponse",
        "/product/sleep/care": "ProductCareResponse",
        "/product/sleep/records": "ProductRecordsResponse",
    }
    for path, model in expected.items():
        response = schema["paths"][path]["get"]["responses"]["200"]
        assert response["content"]["application/json"]["schema"]["$ref"].endswith(
            "/" + model
        )
    content = schema["components"]["schemas"][
        "ProductSleepTodayProjection"
    ]["properties"]["content"]
    assert content["discriminator"]["propertyName"] == "audience"
    assert set(content["discriminator"]["mapping"]) == {
        "elder",
        "family",
        "doctor",
    }


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


@pytest.mark.parametrize(
    "body",
    [
        gzip.compress(b'{"intent":"review"}')[:-4],
        gzip.compress(b'{"intent":"review"}') + b"trailing-bytes",
        gzip.compress(b'{"intent":"review"}')
        + gzip.compress(b'{"second":"member"}'),
    ],
    ids=("truncated", "trailing-bytes", "additional-member"),
)
def test_body_limiter_rejects_incomplete_or_ambiguous_gzip(body: bytes) -> None:
    downstream_called = False
    messages: list[dict[str, Any]] = []

    async def downstream(scope, receive, send) -> None:
        nonlocal downstream_called
        del scope, receive, send
        downstream_called = True

    limiter = BoundedRequestMiddleware(
        downstream,
        max_compressed_bytes=4096,
        max_decompressed_bytes=4096,
        max_json_depth=10,
        max_json_members=100,
        request_timeout_seconds=1,
    )

    async def exercise() -> None:
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await limiter(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-encoding", b"gzip"),
                ],
            },
            receive,
            send,
        )

    asyncio.run(exercise())

    start = next(item for item in messages if item["type"] == "http.response.start")
    response_body = next(
        item["body"] for item in messages if item["type"] == "http.response.body"
    )
    assert start["status"] == 400
    assert json.loads(response_body)["code"] == "invalid_gzip_body"
    assert downstream_called is False


def test_request_timeout_includes_body_receive() -> None:
    downstream_called = False
    messages: list[dict[str, Any]] = []

    async def downstream(scope, receive, send) -> None:
        nonlocal downstream_called
        del scope, receive, send
        downstream_called = True

    limiter = BoundedRequestMiddleware(
        downstream,
        max_compressed_bytes=4096,
        max_decompressed_bytes=4096,
        max_json_depth=10,
        max_json_members=100,
        request_timeout_seconds=0.01,
    )

    async def exercise() -> None:
        async def receive() -> dict[str, Any]:
            await asyncio.sleep(1)
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await limiter(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": [(b"content-type", b"application/json")],
            },
            receive,
            send,
        )

    asyncio.run(exercise())

    start = next(item for item in messages if item["type"] == "http.response.start")
    response_body = next(
        item["body"] for item in messages if item["type"] == "http.response.body"
    )
    assert start["status"] == 504
    assert json.loads(response_body)["code"] == "request_deadline_exceeded"
    assert downstream_called is False


@pytest.mark.parametrize("phase", ("gzip_decode", "json_parse"))
def test_request_timeout_includes_decode_and_json_parse(
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sleepagent.app as backend_app_module

    messages: list[dict[str, Any]] = []

    async def downstream(scope, receive, send) -> None:
        del scope, receive, send
        raise AssertionError("timed-out request reached downstream app")

    class SlowDecodeMiddleware(BoundedRequestMiddleware):
        def _decode(self, raw: bytes, encoding: str | None) -> bytes:
            wall_time.sleep(0.05)
            return super()._decode(raw, encoding)

    middleware_type = (
        SlowDecodeMiddleware
        if phase == "gzip_decode"
        else BoundedRequestMiddleware
    )
    if phase == "json_parse":
        original_validate_json = backend_app_module._validate_json

        def slow_validate_json(*args: Any, **kwargs: Any) -> None:
            wall_time.sleep(0.05)
            original_validate_json(*args, **kwargs)

        monkeypatch.setattr(
            backend_app_module,
            "_validate_json",
            slow_validate_json,
        )

    limiter = middleware_type(
        downstream,
        max_compressed_bytes=4096,
        max_decompressed_bytes=4096,
        max_json_depth=10,
        max_json_members=100,
        request_timeout_seconds=0.005,
    )
    body = gzip.compress(b"{}") if phase == "gzip_decode" else b"{}"

    async def exercise() -> None:
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        headers = [(b"content-type", b"application/json")]
        if phase == "gzip_decode":
            headers.append((b"content-encoding", b"gzip"))
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

    asyncio.run(exercise())

    start = next(item for item in messages if item["type"] == "http.response.start")
    assert start["status"] == 504


def test_public_v1_watermarks_are_server_owned_even_on_error_responses() -> None:
    async def downstream(scope, receive, send) -> None:
        del scope, receive
        await send(
            {
                "type": "http.response.start",
                "status": 422,
                "headers": [
                    (b"x-sleepagent-data-mode", b"live"),
                    (b"x-sleepagent-synthetic-non-release", b"false"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = ReplayWatermarkMiddleware(downstream, data_mode="replay")
    messages: list[dict[str, Any]] = []

    async def exercise() -> None:
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await middleware(
            {"type": "http", "path": "/api/v1/subjects/s1"},
            receive,
            send,
        )

    asyncio.run(exercise())
    headers = dict(messages[0]["headers"])
    assert headers[b"x-sleepagent-data-mode"] == b"replay"
    assert headers[b"x-sleepagent-synthetic-non-release"] == b"true"


def test_demo_is_token_protected_and_watermarked() -> None:
    runtime, _, _ = _runtime(
        surfaces=frozenset({ApiSurface.DEMO})
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


def test_demo_technical_trace_is_token_protected_and_read_only() -> None:
    runtime, _, _ = _runtime(surfaces=frozenset({ApiSurface.DEMO}))
    app = create_sleep_backend_app(runtime)
    route = next(
        item
        for item in app.routes
        if getattr(item, "path", None) == "/demo/v1/technical-trace"
    )

    with pytest.raises(HTTPException) as denied:
        route.endpoint(operation_id="root-1", demo_token=None)
    allowed = route.endpoint(operation_id="root-1", demo_token=DEMO_TOKEN)

    assert denied.value.status_code == 401
    assert allowed.root_operation_id == "root-1"
    assert allowed.product_attempt_count == 1


def test_demo_and_product_errors_are_flat_correlated_watermarked_envelopes() -> None:
    demo_runtime, _, _ = _runtime(surfaces=frozenset({ApiSurface.DEMO}))
    demo_app = create_sleep_backend_app(demo_runtime)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/demo/v1/clock",
            "headers": [],
            "app": demo_app,
            "state": {"correlation_id": "correlation-1"},
        }
    )
    denied = asyncio.run(
        _demo_error_handler(
            request,
            DemoApiError(
                401,
                "authentication_failed",
                "Demo controller authentication failed.",
            ),
        )
    )
    assert denied.status_code == 401
    assert json.loads(denied.body) == {
        "schema_version": "demo_error.v1",
        "data_mode": "replay",
        "synthetic_non_release": True,
        "code": "authentication_failed",
        "message": "Demo controller authentication failed.",
        "retryable": False,
        "correlation_id": "correlation-1",
    }
    assert "detail" not in json.loads(denied.body)

    product_runtime, _, _ = _runtime(
        surfaces=frozenset({ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT})
    )
    product_app = create_sleep_backend_app(
        product_runtime,
        enabled_surfaces={ApiSurface.PRODUCT},
    )
    product_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/product/sleep/trends",
            "headers": [],
            "app": product_app,
            "state": {"correlation_id": "correlation-2"},
        }
    )
    unavailable = asyncio.run(
        _product_error_handler(
            product_request,
            ProductApiError(
                "capability_not_implemented",
                "The Product trends read model is not implemented.",
                status_code=501,
            ),
        )
    )
    unavailable_body = json.loads(unavailable.body)
    assert unavailable.status_code == 501
    assert unavailable_body["schema_version"] == "product_error.v1"
    assert unavailable_body["code"] == "capability_not_implemented"
    assert unavailable_body["data_mode"] == "replay"
    assert unavailable_body["synthetic_non_release"] is True
    assert unavailable_body["correlation_id"] == "correlation-2"


def test_internal_surface_is_not_in_public_openapi_and_requires_credential() -> None:
    runtime, _, _ = _runtime(
        surfaces=frozenset({ApiSurface.INTERNAL})
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
    reconciliation = next(
        route
        for route in mounted.routes
        if getattr(route, "path", None) == "/reconciliation/{operation_id}"
    )
    metrics = next(
        route
        for route in mounted.routes
        if getattr(route, "path", None) == "/metrics"
    )
    with pytest.raises(HTTPException) as denied:
        ready.endpoint(x_internal_token=None)
    allowed = ready.endpoint(x_internal_token=INTERNAL_TOKEN)
    reconciliation_value = reconciliation.endpoint(
        operation_id="reconciliation-1",
        x_internal_token=INTERNAL_TOKEN,
    )
    metrics_value = metrics.endpoint(x_internal_token=INTERNAL_TOKEN)
    with pytest.raises(HTTPException) as missing:
        reconciliation.endpoint(
            operation_id="missing",
            x_internal_token=INTERNAL_TOKEN,
        )

    assert not any(path.startswith("/internal") for path in paths)
    assert denied.value.status_code == 401
    assert allowed["ready"] is False
    assert reconciliation_value["resolution"] == "known_delivered"
    assert metrics_value["schema_version"] == "sleepagent_backend_metrics.v1"
    assert metrics_value["durable"]["queues"] == []
    assert missing.value.status_code == 404


@pytest.mark.asgi_lifespan
def test_public_status_is_removed_and_correlation_id_is_server_owned(
    real_lifespan_client,
) -> None:
    runtime, _, _ = _runtime(
        surfaces=frozenset({ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT})
    )
    app = create_sleep_backend_app(
        runtime,
        enabled_surfaces={ApiSurface.PRODUCT},
    )

    with real_lifespan_client(app) as client:
        assert client.get("/health").status_code == 404
        assert client.get("/status").status_code == 404
        rejected = client.post(
            "/product/sleep/interactions/start",
            json={},
            headers={
                "Idempotency-Key": "invalid-body",
                "X-Correlation-ID": "client-controlled-id",
            },
        )

    body = rejected.json()
    assert rejected.status_code == 422
    assert body["correlation_id"] == rejected.headers["x-correlation-id"]
    assert body["correlation_id"] != "client-controlled-id"


def test_factory_refuses_a_surface_without_explicit_capability() -> None:
    runtime, _, _ = _runtime(
        surfaces=frozenset({ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT})
    )

    with pytest.raises(ValueError, match="not enabled"):
        create_sleep_backend_app(
            runtime,
            enabled_surfaces={ApiSurface.PRODUCT, ApiSurface.DEMO},
        )
