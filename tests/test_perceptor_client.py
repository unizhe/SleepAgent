from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from sleepagent.integrations.perceptor import FakePerceptorClient
from sleepagent.integrations.perceptor.client import (
    PERCEPTOR_BASE_URL_ENV,
    PERCEPTOR_CLIENT_ID_ENV,
    PERCEPTOR_CLIENT_SECRET_ENV,
    PerceptorClient,
    PerceptorConfig,
    PerceptorConfigurationError,
    PerceptorRealtimeSessionError,
    build_perceptor_client_from_env,
)
from sleepagent.integrations.perceptor.signing import sign_parameters


def test_perceptor_config_requires_live_env_values() -> None:
    with pytest.raises(PerceptorConfigurationError, match=PERCEPTOR_BASE_URL_ENV):
        PerceptorConfig.from_env({})

    config = PerceptorConfig.from_env(
        {
            PERCEPTOR_BASE_URL_ENV: "https://api.example.test/v2/",
            PERCEPTOR_CLIENT_ID_ENV: "client-id",
            PERCEPTOR_CLIENT_SECRET_ENV: "client-secret",
            "PERCEPTOR_DEFAULT_DEVICE_NAME": "imei-001",
            "PERCEPTOR_DEFAULT_HOME_ID": "123",
            "PERCEPTOR_TIMEOUT_SECONDS": "2.5",
        }
    )

    assert config.base_url == "https://api.example.test/v2"
    assert config.client_id == "client-id"
    assert config.default_device_name == "imei-001"
    assert config.default_home_id == "123"
    assert config.timeout_seconds == 2.5


def test_perceptor_factory_requires_explicit_fake_replay_namespace() -> None:
    with pytest.raises(PerceptorConfigurationError, match="explicitly configured"):
        build_perceptor_client_from_env({})
    client = build_perceptor_client_from_env(
        {
            "PERCEPTOR_PROVIDER_MODE": "fake",
            "SLEEPAGENT_DEPLOYMENT_MODE": "test",
            "SLEEPAGENT_PERCEPTOR_DATA_NAMESPACE": "replay:perceptor-client-test",
        }
    )

    assert isinstance(client, FakePerceptorClient)
    client.start_realtime()
    response = client.get_realtime()
    assert response["success"] is True
    assert client.requests[-1]["endpoint"] == "/vitalSigns/getRealTimes"


def test_sleep_report_requires_canonical_explicit_local_date() -> None:
    client = FakePerceptorClient()
    with pytest.raises(TypeError):
        client.get_sleep_report()  # type: ignore[call-arg]
    with pytest.raises(PerceptorConfigurationError):
        client.get_sleep_report(report_date="2026-7-8")
    response = client.get_sleep_report(report_date="2026-07-08")
    assert response["success"] is True
    assert client.requests[-1]["biz_params"]["date"] == "2026-07-08"


def test_history_client_rejects_oversized_batches_windows_and_naive_time() -> None:
    client = FakePerceptorClient()
    start = datetime(2026, 7, 30, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="1..20"):
        client.get_history_data(
            device_names=[f"d-{index}" for index in range(21)],
            start_at=start,
            end_at=start + timedelta(minutes=5),
        )
    with pytest.raises(ValueError, match="<= 1 hour"):
        client.get_history_data(
            device_names=["d-1"],
            start_at=start,
            end_at=start + timedelta(hours=1, seconds=1),
        )
    with pytest.raises(ValueError, match="aware"):
        client.get_history_data(
            device_names=["d-1"],
            start_at=start.replace(tzinfo=None),
            end_at=(start + timedelta(minutes=5)).replace(tzinfo=None),
        )


def test_live_client_builds_signed_request_and_caches_token() -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read()
        json_payload = httpx.Request(
            "POST",
            "https://decode.local",
            content=payload,
        )
        parsed = httpx.Response(200, request=json_payload, content=payload).json()
        calls.append(
            {
                "path": request.url.path,
                "headers": request.headers,
                "json": parsed,
            }
        )
        if request.url.path == "/v2/token/get":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "success": True,
                    "data": {"access_token": "token-1", "expires_in": 300},
                },
            )
        return httpx.Response(
            200,
            json={"code": 200, "success": True, "data": {"ok": True}},
        )

    http_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=2.0,
    )
    config = PerceptorConfig(
        base_url="https://api.example.test/v2",
        client_id="client-id",
        client_secret="client-secret",
        default_device_name="imei-001",
        default_home_id=123,
        timeout_seconds=2.0,
        token_refresh_margin_seconds=30.0,
    )
    client = PerceptorClient(
        config,
        http_client=http_client,
        time_provider=lambda: 1_700_000_000.0,
        nonce_factory=lambda: "nonce-fixed",
        sleep_provider=lambda _seconds: None,
    )
    try:
        first = client.start_realtime()
        second = client.get_realtime()
        third = client.get_alarm_list()
    finally:
        http_client.close()

    assert first["success"] is True
    assert second["success"] is True
    assert third["success"] is True
    assert [call["path"] for call in calls].count("/v2/token/get") == 1
    request_calls = [call for call in calls if call["path"] != "/v2/token/get"]
    start_payload = request_calls[0]["json"]
    realtime_payload = request_calls[1]["json"]
    alarm_payload = request_calls[2]["json"]

    assert request_calls[0]["path"] == "/v2/vitalSigns/start"
    assert request_calls[1]["path"] == "/v2/vitalSigns/getRealTimes"
    assert request_calls[0]["headers"]["authorization"] == "Bearer token-1"
    assert realtime_payload["client_id"] == "client-id"
    assert realtime_payload["device_name"] == "imei-001"
    assert realtime_payload["home_id"] == 123
    assert realtime_payload["timestamp"] == "1700000000"
    assert realtime_payload["sign_nonce"] == "nonce-fixed"
    assert "client_secret" not in realtime_payload
    assert start_payload["sign"] == sign_parameters(
        {key: value for key, value in start_payload.items() if key != "sign"},
        client_secret="client-secret",
    )
    assert realtime_payload["sign"] == sign_parameters(
        {key: value for key, value in realtime_payload.items() if key != "sign"},
        client_secret="client-secret",
    )
    assert alarm_payload["sign"] == sign_parameters(
        {key: value for key, value in alarm_payload.items() if key != "sign"},
        client_secret="client-secret",
    )


def test_token_cache_uses_expires_at_and_refresh_margin() -> None:
    now = 1_000.0
    token_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/token/get":
            token_calls.append(request.url.path)
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "success": True,
                    "data": {
                        "access_token": f"token-{len(token_calls)}",
                        "expires_in": 100,
                    },
                },
            )
        return httpx.Response(
            200,
            json={"code": 200, "success": True, "data": {"ok": True}},
        )

    http_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=2.0,
    )
    config = PerceptorConfig(
        base_url="https://api.example.test/v2",
        client_id="client-id",
        client_secret="client-secret",
        token_refresh_margin_seconds=10.0,
        max_retries=0,
    )
    client = PerceptorClient(
        config,
        http_client=http_client,
        time_provider=lambda: now,
    )
    try:
        assert client.get_access_token() == "token-1"
        assert client.get_access_token() == "token-1"
        assert client._cached_token is not None
        assert client._cached_token.expires_at == 1_100.0

        now = 1_091.0
        assert client.get_access_token() == "token-2"
    finally:
        http_client.close()

    assert len(token_calls) == 2


def test_realtime_requires_start_before_polling_without_http_call() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={"code": 200, "success": True, "data": {"ok": True}},
        )

    http_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=2.0,
    )
    config = PerceptorConfig(
        base_url="https://api.example.test/v2",
        client_id="client-id",
        client_secret="client-secret",
        default_device_name="imei-001",
        default_home_id=123,
    )
    client = PerceptorClient(config, http_client=http_client)
    try:
        with pytest.raises(PerceptorRealtimeSessionError, match="start_realtime"):
            client.get_realtime()
    finally:
        http_client.close()

    assert calls == []


def test_realtime_session_expiry_returns_clear_error() -> None:
    now = 1_000.0
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v2/token/get":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "success": True,
                    "data": {"access_token": "token-1", "expires_in": 300},
                },
            )
        return httpx.Response(
            200,
            json={"code": 200, "success": True, "data": {"expires_in": 1}},
        )

    http_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=2.0,
    )
    config = PerceptorConfig(
        base_url="https://api.example.test/v2",
        client_id="client-id",
        client_secret="client-secret",
        default_device_name="imei-001",
        default_home_id=123,
        max_retries=0,
    )
    client = PerceptorClient(
        config,
        http_client=http_client,
        time_provider=lambda: now,
    )
    try:
        assert client.start_realtime()["success"] is True
        now = 1_002.0
        with pytest.raises(PerceptorRealtimeSessionError, match="expired"):
            client.get_realtime()
    finally:
        http_client.close()

    assert "/v2/vitalSigns/getRealTimes" not in calls


def test_live_client_retries_retryable_vendor_response_once() -> None:
    start_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal start_attempts
        if request.url.path == "/v2/token/get":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "success": True,
                    "data": {"access_token": "token-1", "expires_in": 300},
                },
            )
        if request.url.path == "/v2/vitalSigns/start":
            start_attempts += 1
            if start_attempts == 1:
                return httpx.Response(503, request=request, json={"message": "busy"})
        return httpx.Response(
            200,
            json={"code": 200, "success": True, "data": {"ok": True}},
        )

    http_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=2.0,
    )
    config = PerceptorConfig(
        base_url="https://api.example.test/v2",
        client_id="client-id",
        client_secret="client-secret",
        default_device_name="imei-001",
        default_home_id=123,
        max_retries=1,
        retry_backoff_seconds=0.0,
    )
    client = PerceptorClient(config, http_client=http_client)
    try:
        assert client.start_realtime()["success"] is True
    finally:
        http_client.close()

    assert start_attempts == 2


def test_fake_provider_requires_start_then_supports_pull_endpoints() -> None:
    client = FakePerceptorClient()

    with pytest.raises(PerceptorRealtimeSessionError, match="start_realtime"):
        client.get_realtime()

    assert client.start_realtime()["success"] is True
    assert client.get_realtime()["success"] is True
    assert client.get_sleep_report(report_date="2026-07-08")["success"] is True
    assert client.get_alarm_list()["success"] is True
    assert client.get_device_detail()["success"] is True
    assert [request["endpoint"] for request in client.requests] == [
        "/vitalSigns/start",
        "/vitalSigns/getRealTimes",
        "/vitalSigns/getSleepReport",
        "/alarm/getList",
        "/device/detail",
    ]
