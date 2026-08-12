from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

import httpx

from sleepagent.observability import log_event, record_error
from sleepagent.integrations.llm import (
    CloudLLMClient,
    CloudLLMConfig,
    CloudLLMError,
    CloudLLMNotConfiguredError,
    ModelInvocationMetadata,
)


DEFAULT_PRODUCT_LLM_MODEL = "deepseek-v4-flash"
DEFAULT_PRODUCT_LLM_BASE_URL = "https://api.deepseek.com"
PRODUCT_LLM_API_KEY_ENV = "DEEPSEEK_API_KEY"
PRODUCT_LLM_MODEL_ENV = "SLEEPAGENT_PRODUCT_LLM_MODEL"
PRODUCT_LLM_BASE_URL_ENV = "SLEEPAGENT_PRODUCT_LLM_BASE_URL"
PRODUCT_LLM_TIMEOUT_SECONDS_ENV = "SLEEPAGENT_PRODUCT_LLM_TIMEOUT_SECONDS"
PRODUCT_LLM_MAX_TOKENS_ENV = "SLEEPAGENT_PRODUCT_LLM_MAX_TOKENS"
LLM_NOT_CONFIGURED_MESSAGE = "LLM is not configured"


class ProductLLMProviderError(RuntimeError):
    """Raised when an OpenAI-compatible provider cannot return usable content."""


class ProductLLMNotConfiguredError(ProductLLMProviderError):
    """Raised when no API key is configured for the product dialogue provider."""


class ProductLLMConfigurationError(ValueError):
    """Raised when explicit Product LLM environment configuration is invalid."""


@dataclass(frozen=True)
class OpenAICompatibleProviderConfig:
    model: str = DEFAULT_PRODUCT_LLM_MODEL
    api_key_env: str = PRODUCT_LLM_API_KEY_ENV
    base_url: str = DEFAULT_PRODUCT_LLM_BASE_URL
    temperature: float = 0.2
    timeout_seconds: float = 30.0
    retry: int = 1
    max_output_tokens: int = 1200
    thinking_type: str | None = "disabled"


def openai_compatible_provider_config_from_env(
    environment: Mapping[str, str] | None = None,
) -> OpenAICompatibleProviderConfig:
    """Resolve the documented Product LLM settings without reading the secret."""

    env = os.environ if environment is None else environment
    model = _optional_setting(
        env,
        PRODUCT_LLM_MODEL_ENV,
        DEFAULT_PRODUCT_LLM_MODEL,
    )
    base_url = _optional_setting(
        env,
        PRODUCT_LLM_BASE_URL_ENV,
        DEFAULT_PRODUCT_LLM_BASE_URL,
    ).rstrip("/")
    _validate_base_url(base_url)
    timeout_seconds = _positive_float_setting(
        env,
        PRODUCT_LLM_TIMEOUT_SECONDS_ENV,
        30.0,
    )
    max_output_tokens = _positive_int_setting(
        env,
        PRODUCT_LLM_MAX_TOKENS_ENV,
        1200,
    )
    return OpenAICompatibleProviderConfig(
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        max_output_tokens=max_output_tokens,
    )


class ProductChatProvider(Protocol):
    @property
    def is_configured(self) -> bool:
        ...

    def create_json_completion(
        self,
        *,
        messages: list[dict[str, str]],
        config: OpenAICompatibleProviderConfig,
    ) -> str:
        ...


class OpenAICompatibleChatProvider:
    """Minimal DeepSeek/OpenAI-compatible JSON chat provider."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str = PRODUCT_LLM_API_KEY_ENV,
        base_url: str = DEFAULT_PRODUCT_LLM_BASE_URL,
        timeout_seconds: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        candidate = os.environ.get(api_key_env) if api_key is None else api_key
        self._api_key = _usable_secret(candidate)
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    @property
    def is_configured(self) -> bool:
        return self._api_key is not None

    def create_json_completion(
        self,
        *,
        messages: list[dict[str, str]],
        config: OpenAICompatibleProviderConfig,
    ) -> str:
        if self._api_key is None:
            error = ProductLLMNotConfiguredError(
                f"LLM API key is missing. Set {self.api_key_env}."
            )
            log_event(
                "llm_call_skipped",
                source="product_dialogue",
                reason="not_configured",
                model=config.model,
                provider_host=_provider_host(config.base_url),
            )
            raise error

        response = self.create_chat_completion(
            model=config.model,
            messages=messages,
            temperature=config.temperature,
            max_output_tokens=config.max_output_tokens,
            thinking_type=config.thinking_type,
            retry=config.retry,
        )
        return _extract_message_content(response)

    def create_chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_output_tokens: int = 1200,
        thinking_type: str | None = "disabled",
        retry: int = 1,
    ) -> dict[str, Any]:
        if self._api_key is None:
            error = ProductLLMNotConfiguredError(
                f"LLM API key is missing. Set {self.api_key_env}."
            )
            log_event(
                "llm_call_skipped",
                source="product_dialogue",
                reason="not_configured",
                model=model,
                provider_host=_provider_host(self.base_url),
            )
            raise error

        extra_body = (
            {"thinking": {"type": thinking_type}}
            if thinking_type is not None
            else {}
        )
        cloud = CloudLLMClient(
            CloudLLMConfig(
                base_url=self.base_url,
                api_key=self._api_key,
                model_id=model,
                timeout=self.timeout_seconds,
                retry=retry,
                temperature=temperature,
                max_output_tokens=max(1, max_output_tokens),
                extra_body=extra_body,
            ),
            http_client=self._http_client,
        )
        try:
            log_event(
                "llm_call_start",
                source="product_dialogue",
                model=model,
                provider_host=_provider_host(self.base_url),
                message_count=len(messages),
                response_format="json_object",
            )
            response_payload = cloud.complete_raw(
                messages=messages,
                metadata=ModelInvocationMetadata(
                    model_provider="openai-compatible",
                    model_id=model,
                    prompt_version="product-device.v1",
                ),
                response_format={"type": "json_object"},
            )
            log_event(
                "llm_call_success",
                source="product_dialogue",
                model=model,
                provider_host=_provider_host(self.base_url),
                status_code=200,
                choice_count=len(response_payload.get("choices", []))
                if isinstance(response_payload.get("choices"), list)
                else None,
            )
            return response_payload
        except CloudLLMNotConfiguredError as exc:
            raise ProductLLMNotConfiguredError(
                f"LLM API key is missing. Set {self.api_key_env}."
            ) from exc
        except CloudLLMError as exc:
            error = ProductLLMProviderError("Product LLM request failed.")
            log_event(
                "llm_call_failure",
                level=logging.WARNING,
                source="product_dialogue",
                model=model,
                provider_host=_provider_host(self.base_url),
                error_type=exc.__class__.__name__,
            )
            record_error(
                event="llm_call_failure",
                error=error,
                source="product_dialogue",
                context={"model": model, "provider_host": _provider_host(self.base_url)},
            )
            raise error from exc


def _extract_message_content(response: dict[str, Any]) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProductLLMProviderError("Product LLM response did not include content.") from exc
    if not isinstance(content, str) or not content.strip():
        raise ProductLLMProviderError("Product LLM response content was empty.")
    return content


def _provider_host(base_url: str) -> str:
    parsed = urlparse(base_url)
    host = parsed.hostname
    if host is None:
        return "invalid"
    try:
        port = parsed.port
    except ValueError:
        return host
    return host if port is None else f"{host}:{port}"


def _optional_setting(
    env: Mapping[str, str],
    name: str,
    default: str,
) -> str:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip()
    if not value or _looks_like_placeholder(value):
        raise ProductLLMConfigurationError(f"{name} must be a concrete value")
    return value


def _positive_float_setting(
    env: Mapping[str, str],
    name: str,
    default: float,
) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip()
    try:
        parsed = float(value)
    except ValueError:
        raise ProductLLMConfigurationError(
            f"{name} must be a positive finite number"
        ) from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise ProductLLMConfigurationError(
            f"{name} must be a positive finite number"
        )
    return parsed


def _positive_int_setting(
    env: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip()
    try:
        parsed = int(value)
    except ValueError:
        raise ProductLLMConfigurationError(
            f"{name} must be a positive integer"
        ) from None
    if parsed <= 0:
        raise ProductLLMConfigurationError(
            f"{name} must be a positive integer"
        )
    return parsed


def _validate_base_url(base_url: str) -> None:
    if any(
        character.isspace()
        or ord(character) < 0x20
        or ord(character) == 0x7F
        for character in base_url
    ) or re.search(r"%(?![0-9A-Fa-f]{2})", base_url):
        raise ProductLLMConfigurationError(
            f"{PRODUCT_LLM_BASE_URL_ENV} must be a valid absolute HTTP(S) URL"
        )
    try:
        parsed = urlparse(base_url)
        normalized = httpx.URL(base_url)
        hostname = parsed.hostname
        port = parsed.port
    except (ValueError, httpx.InvalidURL):
        raise ProductLLMConfigurationError(
            f"{PRODUCT_LLM_BASE_URL_ENV} must be an absolute HTTP(S) URL"
        ) from None
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ProductLLMConfigurationError(
            f"{PRODUCT_LLM_BASE_URL_ENV} must be an absolute HTTP(S) URL "
            "without credentials, query, or fragment"
        )
    if not normalized.host or not _is_valid_provider_host(hostname):
        raise ProductLLMConfigurationError(
            f"{PRODUCT_LLM_BASE_URL_ENV} must contain a valid host"
        )
    if parsed.scheme == "http" and not _is_loopback_host(hostname):
        raise ProductLLMConfigurationError(
            f"{PRODUCT_LLM_BASE_URL_ENV} must use HTTPS outside loopback"
        )


def _usable_secret(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate or _looks_like_placeholder(candidate):
        return None
    return candidate


def _looks_like_placeholder(value: str) -> bool:
    return value.startswith("<") and value.endswith(">")


def _is_loopback_host(hostname: str) -> bool:
    if hostname.rstrip(".").lower() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_valid_provider_host(hostname: str) -> bool:
    candidate = hostname.rstrip(".")
    if not candidate:
        return False
    try:
        ip_address(candidate)
        return True
    except ValueError:
        pass
    try:
        ascii_hostname = candidate.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_hostname) > 253:
        return False
    label_pattern = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
    return all(
        label_pattern.fullmatch(label) is not None
        for label in ascii_hostname.split(".")
    )
