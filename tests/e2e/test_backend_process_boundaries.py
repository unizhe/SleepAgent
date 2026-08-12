from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest


pytestmark = pytest.mark.e2e


def _get(base_url: str, path: str) -> tuple[int, dict[str, Any] | None]:
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=5.0) as response:
            status = int(response.status)
            raw = response.read(1_048_577)
    except HTTPError as exc:
        status = int(exc.code)
        raw = exc.read(1_048_577)
    except (URLError, TimeoutError, OSError) as exc:
        pytest.fail(
            f"independent backend process at {base_url} is unavailable: "
            f"{type(exc).__name__}"
        )
    assert len(raw) <= 1_048_576
    if not raw:
        return status, None
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None
    return status, value if isinstance(value, dict) else None


def test_real_network_processes_have_disjoint_public_surfaces() -> None:
    """Exercise separately bound compose processes, never an in-process ASGI app."""

    if os.environ.get("SLEEPAGENT_SETTINGS_PROFILE") != "test-postgres":
        pytest.skip("the compose-backed test-postgres process profile is required")
    product_url = os.environ.get(
        "SLEEPAGENT_E2E_PRODUCT_BASE_URL", "http://127.0.0.1:18000"
    )
    demo_url = os.environ.get(
        "SLEEPAGENT_E2E_DEMO_BASE_URL", "http://127.0.0.1:18001"
    )

    for base_url in (product_url, demo_url):
        status, body = _get(base_url, "/livez")
        assert status == 200
        assert body == {"status": "alive"}

    product_demo_status, _ = _get(product_url, "/demo/v1/clock")
    demo_product_status, _ = _get(demo_url, "/product/sleep/today")
    product_route_status, _ = _get(product_url, "/product/sleep/today")
    demo_route_status, _ = _get(demo_url, "/demo/v1/clock")

    assert product_demo_status == 404
    assert demo_product_status == 404
    assert product_route_status in {401, 403}
    assert demo_route_status in {401, 403}
