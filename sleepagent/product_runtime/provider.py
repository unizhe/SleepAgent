from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from sleepagent.product_device.llm import (
    OpenAICompatibleChatProvider,
    OpenAICompatibleProviderConfig,
    ProductChatProvider,
    ProductLLMProviderError,
)


SchemaT = TypeVar("SchemaT", bound=BaseModel)
PRODUCT_STRUCTURED_PROVIDER_VERSION = (
    "sleepagent-product-structured-provider.v1"
)


class OpenAICompatibleStructuredAgentModel:
    """Strict JSON-Schema adapter for all four Product Agent identities."""

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
        provider = self._provider
        if not hasattr(provider, "create_chat_completion"):
            raise ProductLLMProviderError(
                "Structured Agent provider must expose raw completion metadata."
            )
        response = provider.create_chat_completion(
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


__all__ = [
    "PRODUCT_STRUCTURED_PROVIDER_VERSION",
    "OpenAICompatibleStructuredAgentModel",
]
