from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import Field

from sleepagent.radar_agent.product_agent.contracts import StrictContract


PRODUCT_EXTERNAL_ACTION_VERSION = "sleepagent-product-external-action.v1"
EXTERNAL_NOTIFY_URL_ENV = "SLEEPAGENT_EXTERNAL_NOTIFY_URL"
EXTERNAL_SHARE_URL_ENV = "SLEEPAGENT_EXTERNAL_SHARE_URL"
EXTERNAL_EXPORT_URL_ENV = "SLEEPAGENT_EXTERNAL_EXPORT_URL"
EXTERNAL_ACTION_API_KEY_ENV = "SLEEPAGENT_EXTERNAL_ACTION_API_KEY"
EXTERNAL_ACTION_TIMEOUT_ENV = "SLEEPAGENT_EXTERNAL_ACTION_TIMEOUT_SECONDS"


class ExternalActionConfigurationError(RuntimeError):
    pass


class ExternalActionExecutionRequest(StrictContract):
    tool_name: Literal["external.notify", "external.share", "external.export"]
    target_id: str = Field(..., min_length=1)
    target_version: int = Field(..., ge=1)
    target_hash: str = Field(..., min_length=64, max_length=64)
    actor_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    action_scope: str = Field(..., min_length=1)
    fact_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    idempotency_key: str = Field(..., min_length=1)
    payload: dict[str, Any]


class ExternalActionExecutionResult(StrictContract):
    provider: str = Field(..., min_length=1)
    provider_request_id: str = Field(..., min_length=1)
    delivery_status: Literal["pending", "delivered"]
    executed_at: datetime


class UnconfiguredExternalActionExecutor:
    def __call__(
        self,
        request: ExternalActionExecutionRequest,
    ) -> ExternalActionExecutionResult:
        raise ExternalActionConfigurationError(
            f"{request.tool_name} executor is not configured"
        )


class ConfiguredExternalActionExecutor:
    """POST confirmed targets to explicitly configured external gateways."""

    def __init__(
        self,
        *,
        endpoints: dict[str, str],
        api_key: str | None = None,
        timeout_seconds: float = 15.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.endpoints = {
            tool_name: _validated_endpoint(url)
            for tool_name, url in endpoints.items()
            if url
        }
        self.api_key = api_key
        self.http_client = http_client or httpx.Client(timeout=timeout_seconds)

    @classmethod
    def from_env(cls) -> "ConfiguredExternalActionExecutor":
        endpoints = {
            "external.notify": _configured_value(
                os.getenv(EXTERNAL_NOTIFY_URL_ENV)
            ),
            "external.share": _configured_value(
                os.getenv(EXTERNAL_SHARE_URL_ENV)
            ),
            "external.export": _configured_value(
                os.getenv(EXTERNAL_EXPORT_URL_ENV)
            ),
        }
        return cls(
            endpoints=endpoints,
            api_key=_configured_secret(os.getenv(EXTERNAL_ACTION_API_KEY_ENV)),
            timeout_seconds=_configured_timeout(
                os.getenv(EXTERNAL_ACTION_TIMEOUT_ENV)
            ),
        )

    def __call__(
        self,
        request: ExternalActionExecutionRequest,
    ) -> ExternalActionExecutionResult:
        endpoint = self.endpoints.get(request.tool_name)
        if endpoint is None:
            raise ExternalActionConfigurationError(
                f"{request.tool_name} endpoint is not configured"
            )
        headers = {
            "Idempotency-Key": request.idempotency_key,
            "X-SleepAgent-Target-Hash": request.target_hash,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = self.http_client.post(
            endpoint,
            json=request.model_dump(mode="json"),
            headers=headers,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("external gateway response must be an object")
        request_id = (
            body.get("provider_request_id")
            or body.get("request_id")
            or body.get("id")
            or response.headers.get("x-request-id")
        )
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("external gateway response lacks a request ID")
        raw_status = str(body.get("delivery_status") or body.get("status") or "")
        if raw_status in {"accepted", "queued", "pending"}:
            delivery_status: Literal["pending", "delivered"] = "pending"
        elif raw_status in {"delivered", "completed", "succeeded"}:
            delivery_status = "delivered"
        else:
            raise ValueError("external gateway response has an unknown status")
        provider = str(body.get("provider") or urlparse(endpoint).hostname or "")
        if not provider:
            raise ValueError("external gateway response lacks provider identity")
        return ExternalActionExecutionResult(
            provider=provider,
            provider_request_id=request_id.strip(),
            delivery_status=delivery_status,
            executed_at=datetime.now(timezone.utc),
        )


def _configured_secret(value: str | None) -> str | None:
    return _configured_value(value)


def _configured_value(value: str | None) -> str | None:
    if value is None or not value.strip() or value.strip().startswith("<"):
        return None
    return value.strip()


def _configured_timeout(value: str | None) -> float:
    configured = _configured_value(value)
    timeout = 15.0 if configured is None else float(configured)
    if timeout <= 0:
        raise ExternalActionConfigurationError(
            "external action timeout must be positive"
        )
    return timeout


def _validated_endpoint(value: str) -> str:
    endpoint = value.strip()
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ExternalActionConfigurationError(
            "external action endpoint must be an absolute HTTP(S) URL"
        )
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ExternalActionConfigurationError(
            "non-local external action endpoints must use HTTPS"
        )
    return endpoint


__all__ = [
    "EXTERNAL_ACTION_API_KEY_ENV",
    "EXTERNAL_ACTION_TIMEOUT_ENV",
    "EXTERNAL_EXPORT_URL_ENV",
    "EXTERNAL_NOTIFY_URL_ENV",
    "EXTERNAL_SHARE_URL_ENV",
    "PRODUCT_EXTERNAL_ACTION_VERSION",
    "ConfiguredExternalActionExecutor",
    "ExternalActionConfigurationError",
    "ExternalActionExecutionRequest",
    "ExternalActionExecutionResult",
    "UnconfiguredExternalActionExecutor",
]
