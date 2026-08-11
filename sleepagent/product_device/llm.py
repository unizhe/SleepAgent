from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol
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
LLM_NOT_CONFIGURED_MESSAGE = "LLM is not configured"


class ProductLLMProviderError(RuntimeError):
    """Raised when an OpenAI-compatible provider cannot return usable content."""


class ProductLLMNotConfiguredError(ProductLLMProviderError):
    """Raised when no API key is configured for the product dialogue provider."""


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
        self.api_key = api_key or os.environ.get(api_key_env)
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def create_json_completion(
        self,
        *,
        messages: list[dict[str, str]],
        config: OpenAICompatibleProviderConfig,
    ) -> str:
        if not self.api_key:
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
        if not self.api_key:
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
                api_key=self.api_key,
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
    return parsed.netloc or parsed.path
