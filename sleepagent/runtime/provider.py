# 本文件是四 Agent 唯一的模型供应商边界。
# 它负责解析无密钥配置、调用 OpenAI-compatible JSON 接口并严格校验输出；
# 不负责选择业务 Agent、持久化调用结果或提供旧 Radar task runtime。

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, Mapping, Protocol, TypeVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError

from sleepagent.observability import log_event, record_error


DEFAULT_PRODUCT_LLM_MODEL = "deepseek-v4-flash"
DEFAULT_PRODUCT_LLM_BASE_URL = "https://api.deepseek.com"
PRODUCT_LLM_API_KEY_ENV = "DEEPSEEK_API_KEY"
PRODUCT_LLM_MODEL_ENV = "SLEEPAGENT_PRODUCT_LLM_MODEL"
PRODUCT_LLM_BASE_URL_ENV = "SLEEPAGENT_PRODUCT_LLM_BASE_URL"
PRODUCT_LLM_TIMEOUT_SECONDS_ENV = "SLEEPAGENT_PRODUCT_LLM_TIMEOUT_SECONDS"
PRODUCT_LLM_MAX_TOKENS_ENV = "SLEEPAGENT_PRODUCT_LLM_MAX_TOKENS"
PRODUCT_STRUCTURED_PROVIDER_VERSION = "sleepagent-product-structured-provider.v1"
LLM_NOT_CONFIGURED_MESSAGE = "LLM is not configured"


class ProductLLMProviderError(RuntimeError):
    """The provider did not return a usable, schema-valid response."""


class ProductLLMNotConfiguredError(ProductLLMProviderError):
    """No concrete API key is available to the live model path."""


class ProductLLMConfigurationError(ValueError):
    """A public Product LLM setting is malformed."""


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
    """读取公开配置；API key 只在 provider 构造时读取且不会进入配置对象。"""

    env = os.environ if environment is None else environment
    model = _optional_setting(env, PRODUCT_LLM_MODEL_ENV, DEFAULT_PRODUCT_LLM_MODEL)
    base_url = _optional_setting(
        env, PRODUCT_LLM_BASE_URL_ENV, DEFAULT_PRODUCT_LLM_BASE_URL
    ).rstrip("/")
    _validate_base_url(base_url)
    return OpenAICompatibleProviderConfig(
        model=model,
        base_url=base_url,
        timeout_seconds=_positive_float_setting(
            env, PRODUCT_LLM_TIMEOUT_SECONDS_ENV, 30.0
        ),
        max_output_tokens=_positive_int_setting(
            env, PRODUCT_LLM_MAX_TOKENS_ENV, 1200
        ),
    )


class ProductChatProvider(Protocol):
    @property
    def is_configured(self) -> bool: ...

    def create_chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_output_tokens: int = 1200,
        thinking_type: str | None = "disabled",
        retry: int = 1,
    ) -> dict[str, Any]: ...


class OpenAICompatibleChatProvider:
    """最小化 HTTP transport；密钥只保存在私有字段，不进入日志或 repr。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str = PRODUCT_LLM_API_KEY_ENV,
        base_url: str = DEFAULT_PRODUCT_LLM_BASE_URL,
        timeout_seconds: float = 30.0,
        http_client: Any | None = None,
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
        return _message_content(
            self.create_chat_completion(
                model=config.model,
                messages=messages,
                temperature=config.temperature,
                max_output_tokens=config.max_output_tokens,
                thinking_type=config.thinking_type,
                retry=config.retry,
            )
        )

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
            log_event(
                "llm_call_skipped",
                source="product_dialogue",
                reason="not_configured",
                model=model,
                provider_host=_provider_host(self.base_url),
            )
            raise ProductLLMNotConfiguredError(
                f"LLM API key is missing. Set {self.api_key_env}."
            )

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max(1, max_output_tokens),
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        if thinking_type is not None:
            body["thinking"] = {"type": thinking_type}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        client = self._http_client or httpx.Client(timeout=self.timeout_seconds)
        owns_client = self._http_client is None
        last_error: BaseException | None = None
        try:
            for attempt in range(max(0, retry) + 1):
                try:
                    log_event(
                        "llm_call_start",
                        source="product_dialogue",
                        model=model,
                        provider_host=_provider_host(self.base_url),
                        message_count=len(messages),
                        attempt=attempt + 1,
                        response_format="json_object",
                    )
                    response = client.post(
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=body,
                    )
                    status = int(response.status_code)
                    if status == 429 or status >= 500:
                        raise ProductLLMProviderError(
                            f"provider returned retryable HTTP {status}"
                        )
                    if status < 200 or status >= 300:
                        raise ProductLLMProviderError(
                            f"provider returned HTTP {status}"
                        )
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ProductLLMProviderError(
                            "Product LLM response envelope must be an object."
                        )
                    log_event(
                        "llm_call_success",
                        source="product_dialogue",
                        model=model,
                        provider_host=_provider_host(self.base_url),
                        status_code=status,
                        choice_count=len(payload.get("choices", []))
                        if isinstance(payload.get("choices"), list)
                        else None,
                    )
                    return payload
                except (
                    httpx.RequestError,
                    json.JSONDecodeError,
                    ProductLLMProviderError,
                    ValueError,
                ) as exc:
                    last_error = exc
                    if attempt >= max(0, retry):
                        break
            error = ProductLLMProviderError("Product LLM request failed.")
            log_event(
                "llm_call_failure",
                level=logging.WARNING,
                source="product_dialogue",
                model=model,
                provider_host=_provider_host(self.base_url),
                error_type=type(last_error).__name__ if last_error else "unknown",
            )
            record_error(
                event="llm_call_failure",
                error=error,
                source="product_dialogue",
                context={"model": model, "provider_host": _provider_host(self.base_url)},
            )
            raise error from last_error
        finally:
            if owns_client:
                client.close()


SchemaT = TypeVar("SchemaT", bound=BaseModel)


class OpenAICompatibleStructuredAgentModel:
    """把四 Agent 的 Pydantic schema 绑定到同一个严格 JSON 调用路径。"""

    def __init__(
        self,
        *,
        config: OpenAICompatibleProviderConfig | None = None,
        provider: ProductChatProvider | None = None,
    ) -> None:
        self.config = config or OpenAICompatibleProviderConfig()
        self._provider = provider or OpenAICompatibleChatProvider(
            api_key_env=self.config.api_key_env,
            base_url=self.config.base_url,
            timeout_seconds=self.config.timeout_seconds,
        )
        self.last_provider_request_id: str | None = None

    @property
    def provider(self) -> str:
        return "openai-compatible"

    @property
    def model_id(self) -> str:
        return self.config.model

    @property
    def is_configured(self) -> bool:
        configured = getattr(self._provider, "is_configured", None)
        return bool(configured) if configured is not None else True

    def generate(
        self,
        *,
        messages: list[dict[str, str]],
        schema: type[SchemaT],
        prompt_version: str,
        context_packet_id: str,
    ) -> SchemaT:
        self.last_provider_request_id = None
        schema_instruction = {
            "role": "system",
            "content": (
                "Return one JSON object only. It must validate against this "
                f"exact schema for {schema.__name__}. Do not add markdown or "
                "unknown fields. JSON Schema: "
                + json.dumps(
                    schema.model_json_schema(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + f" Prompt version: {prompt_version}. "
                f"Context packet: {context_packet_id}."
            ),
        }
        response = self._provider.create_chat_completion(
            model=self.config.model,
            messages=[schema_instruction, *messages],
            temperature=self.config.temperature,
            max_output_tokens=self.config.max_output_tokens,
            thinking_type=self.config.thinking_type,
            retry=self.config.retry,
        )
        request_id = response.get("id") if isinstance(response, dict) else None
        if isinstance(request_id, str) and request_id.strip():
            self.last_provider_request_id = request_id.strip()
        content = _message_content(response)
        try:
            payload: Any = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ProductLLMProviderError(
                "Structured Agent provider returned invalid JSON."
            ) from exc
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            raise ProductLLMProviderError(
                f"Structured Agent response failed {schema.__name__} validation."
            ) from exc


def _message_content(response: Any) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProductLLMProviderError(
            "Structured Agent response did not include message content."
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise ProductLLMProviderError(
            "Structured Agent response content was empty."
        )
    return content


def _optional_setting(
    env: Mapping[str, str], name: str, default: str
) -> str:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip()
    if not value or _looks_like_placeholder(value):
        raise ProductLLMConfigurationError(f"{name} must be a concrete value")
    return value


def _positive_float_setting(
    env: Mapping[str, str], name: str, default: float
) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        raise ProductLLMConfigurationError(
            f"{name} must be a positive finite number"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise ProductLLMConfigurationError(
            f"{name} must be a positive finite number"
        )
    return value


def _positive_int_setting(
    env: Mapping[str, str], name: str, default: int
) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ProductLLMConfigurationError(
            f"{name} must be a positive integer"
        ) from None
    if value <= 0:
        raise ProductLLMConfigurationError(f"{name} must be a positive integer")
    return value


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
    label = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
    return all(label.fullmatch(part) is not None for part in ascii_hostname.split("."))


__all__ = [
    "DEFAULT_PRODUCT_LLM_BASE_URL",
    "DEFAULT_PRODUCT_LLM_MODEL",
    "LLM_NOT_CONFIGURED_MESSAGE",
    "OpenAICompatibleChatProvider",
    "OpenAICompatibleProviderConfig",
    "OpenAICompatibleStructuredAgentModel",
    "PRODUCT_LLM_API_KEY_ENV",
    "PRODUCT_LLM_BASE_URL_ENV",
    "PRODUCT_LLM_MAX_TOKENS_ENV",
    "PRODUCT_LLM_MODEL_ENV",
    "PRODUCT_LLM_TIMEOUT_SECONDS_ENV",
    "PRODUCT_STRUCTURED_PROVIDER_VERSION",
    "ProductChatProvider",
    "ProductLLMConfigurationError",
    "ProductLLMNotConfiguredError",
    "ProductLLMProviderError",
    "openai_compatible_provider_config_from_env",
]
