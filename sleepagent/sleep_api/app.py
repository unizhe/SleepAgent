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
    manage_worker: bool = True,
) -> FastAPI:
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


app = create_sleep_api_app()


__all__ = ["app", "create_sleep_api_app"]
