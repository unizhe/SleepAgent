import asyncio
import json

from tests.support.diagnostic_app import app
from sleepagent.observability import STATE


def test_health_check_returns_ok() -> None:
    STATE.reset_for_tests()

    response = _request("GET", "/health")

    assert response["status"] == 200
    payload = response["json"]
    assert payload["status"] == "ok"
    assert payload["project"] == "SleepAgent"
    assert payload["stage"] == "v1_observability"
    assert payload["provider_mode"] in {"unconfigured", "fake", "live"}
    assert "capabilities" in payload
    assert "recent_pull_at" in payload
    assert "recent_push_at" in payload
    assert "data_freshness" in payload
    assert "errors" in payload


def test_status_reports_capabilities_without_env_secret_values(monkeypatch) -> None:
    STATE.reset_for_tests()
    monkeypatch.setenv("PERCEPTOR_PROVIDER_MODE", "live")
    monkeypatch.setenv("PERCEPTOR_BASE_URL", "https://vendor.example.test")
    monkeypatch.setenv("PERCEPTOR_CLIENT_ID", "client-id-placeholder")
    monkeypatch.setenv("PERCEPTOR_CLIENT_SECRET", "super-secret-client-value")
    monkeypatch.setenv(
        "SLEEPAGENT_PERCEPTOR_PUSH_SIGNING_SECRET",
        "super-secret-webhook-value",
    )
    monkeypatch.setenv("SLEEPAGENT_PERCEPTOR_PUSH_ENABLED", "true")
    monkeypatch.setenv("SLEEPAGENT_PRODUCT_RADAR_API_KEY", "super-secret-product-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "super-secret-llm-key")

    response = _request("GET", "/status")

    assert response["status"] == 200
    payload = response["json"]
    assert payload["provider_mode"] == "live"
    assert payload["capabilities"]["perceptor_live_configured"] is True
    assert payload["capabilities"]["perceptor_webhook_signature_configured"] is True
    assert payload["capabilities"]["perceptor_push_enabled"] is True
    assert payload["capabilities"]["perceptor_push_capability_status"] == "pending"
    assert payload["capabilities"]["product_api_auth_configured"] is True
    assert payload["capabilities"]["product_llm_configured"] is True
    serialized = response["text"]
    assert "super-secret" not in serialized
    assert "client-id-placeholder" not in serialized


def _request(method: str, path: str) -> dict[str, object]:
    return asyncio.run(_request_async(method, path))


async def _request_async(method: str, path: str) -> dict[str, object]:
    messages: list[dict[str, object]] = []
    request_sent = False

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 1),
            "server": ("testserver", 80),
            "scheme": "http",
            "root_path": "",
        },
        receive,
        send,
    )
    status = next(
        int(message["status"])
        for message in messages
        if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    decoded_body = response_body.decode("utf-8")
    return {
        "status": status,
        "text": decoded_body,
        "json": json.loads(decoded_body) if decoded_body else None,
    }
