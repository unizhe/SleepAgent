"""Standalone ASGI application for the public sleep-domain API."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from sleepagent.sleep_api.router import RuntimeProvider, install_sleep_api
from sleepagent.sleep_api.runtime import (
    get_sleep_api_runtime,
    start_sleep_api_worker,
    stop_sleep_api_worker,
)


def create_sleep_api_app(
    runtime_provider: RuntimeProvider = get_sleep_api_runtime,
    *,
    manage_worker: bool = False,
) -> FastAPI:
    """Create the legacy standalone transport adapter.

    Worker ownership is deliberately disabled by default.  Production mounts
    these routes through :func:`sleepagent.backend_app.create_sleep_backend_app`;
    ``manage_worker=True`` remains only for an explicitly requested historical
    development harness.
    """
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if manage_worker:
            start_sleep_api_worker()
        try:
            yield
        finally:
            if manage_worker:
                stop_sleep_api_worker()

    application = FastAPI(
        title="SleepAgent Sleep Domain API",
        version="1.0.0",
        description=(
            "Independent HTTPS boundary for committed sleep projections and "
            "durable asynchronous commands."
        ),
        lifespan=lifespan,
    )
    install_sleep_api(application, runtime_provider)
    return application


app = create_sleep_api_app(manage_worker=False)


__all__ = ["app", "create_sleep_api_app"]
