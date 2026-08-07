from __future__ import annotations

import inspect

from fastapi import Request

from backend.main import app, perceptor_webhook


WEBHOOK_PATH = "/integrations/perceptor/webhook"


def test_perceptor_webhook_route_is_registered() -> None:
    assert any(
        route.path == WEBHOOK_PATH and "POST" in getattr(route, "methods", set())
        for route in app.routes
    )


def test_perceptor_webhook_boundary_accepts_only_the_raw_request() -> None:
    parameters = inspect.signature(perceptor_webhook).parameters
    assert tuple(parameters) == ("request",)
    assert parameters["request"].annotation is Request
    assert inspect.iscoroutinefunction(perceptor_webhook)


def test_production_route_does_not_use_legacy_webhook_sqlite_authority() -> None:
    source = inspect.getsource(perceptor_webhook)
    assert "build_perceptor_webhook_service_from_env" not in source
    assert "PerceptorWebhookRepository" not in source
    assert "read_bounded_starlette_request" in source
    assert "runtime.ingestion.ingest" in source
