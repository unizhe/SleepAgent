from __future__ import annotations

import pytest

from sleepagent.backend_app import create_sleep_backend_app
from sleepagent.backend_settings import ApiSurface
from tests.integration.test_backend_app import _runtime
from tests.support.runtime_fixtures import (
    reset_backend_runtime_state as reset_active_runtime_for_tests,
)


pytestmark = pytest.mark.asgi_lifespan


def test_real_lifespan_client_runs_the_canonical_backend_graph(
    real_lifespan_client,
) -> None:
    reset_active_runtime_for_tests()
    runtime, pool, _ = _runtime(
        surfaces=frozenset({ApiSurface.PUBLIC_V1, ApiSurface.PRODUCT})
    )
    app = create_sleep_backend_app(
        runtime,
        enabled_surfaces={ApiSurface.PRODUCT},
    )

    try:
        with real_lifespan_client(app) as client:
            assert pool.calls == ["open"]
            response = client.get("/livez")
            assert response.status_code == 200
            assert response.json() == {"status": "alive"}
            assert runtime.worker_handlers == {}
    finally:
        reset_active_runtime_for_tests()

    assert pool.calls == ["open", "close"]
