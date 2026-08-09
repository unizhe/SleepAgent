from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI


pytestmark = pytest.mark.asgi_lifespan


def test_real_lifespan_client_executes_startup_and_shutdown(
    real_lifespan_client,
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        events.append("startup")
        yield
        events.append("shutdown")

    app = FastAPI(lifespan=lifespan)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    with real_lifespan_client(app) as client:
        assert events == ["startup"]
        assert client.get("/ping").json() == {"ok": True}

    assert events == ["startup", "shutdown"]
