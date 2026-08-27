# 本文件是四 Agent 唯一的模型供应商边界。
# 它负责解析无密钥配置、调用 OpenAI-compatible JSON 接口并严格校验输出；
# 不负责选择业务 Agent、持久化调用结果或提供旧 Radar task runtime。

from __future__ import annotations

import ast
import copy
import hashlib
import json
import logging
import math
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, Iterator, Literal, Mapping, Protocol, TypeVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sleepagent.observability import log_event, record_error


DEFAULT_PRODUCT_LLM_MODEL = "deepseek-v4-flash"
DEFAULT_PRODUCT_LLM_BASE_URL = "https://api.deepseek.com"
PRODUCT_LLM_API_KEY_ENV = "DEEPSEEK_API_KEY"
PRODUCT_LLM_MODEL_ENV = "SLEEPAGENT_PRODUCT_LLM_MODEL"
PRODUCT_LLM_BASE_URL_ENV = "SLEEPAGENT_PRODUCT_LLM_BASE_URL"
PRODUCT_LLM_TIMEOUT_SECONDS_ENV = "SLEEPAGENT_PRODUCT_LLM_TIMEOUT_SECONDS"
PRODUCT_LLM_MAX_TOKENS_ENV = "SLEEPAGENT_PRODUCT_LLM_MAX_TOKENS"
LEGACY_PRODUCT_LLM_API_KEY_ENV = "SLEEPAGENT_RADAR_AGENT_LLM_API_KEY"
LEGACY_PRODUCT_LLM_MODEL_ENV = "SLEEPAGENT_RADAR_AGENT_LLM_MODEL_ID"
LEGACY_PRODUCT_LLM_BASE_URL_ENV = "SLEEPAGENT_RADAR_AGENT_LLM_BASE_URL"
LEGACY_PRODUCT_LLM_TIMEOUT_SECONDS_ENV = (
    "SLEEPAGENT_RADAR_AGENT_LLM_TIMEOUT_SECONDS"
)
PRODUCT_STRUCTURED_PROVIDER_VERSION = "sleepagent-product-structured-provider.v1"
LLM_NOT_CONFIGURED_MESSAGE = "LLM is not configured"
_EXACT_SUBSTRING_VALIDATION_ERROR = (
    "every semantic binding rendered_text must be an exact substring "
    "of Communication text"
)
_UNBOUND_NUMERIC_TOKEN_MARKER = "remove these unbound tokens:"
_NUMERIC_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?%?"
)
_NUMERIC_REPAIR_REPLACEMENTS = ("当晚", "夜间", "近期")
_NUMERIC_REPAIR_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?<![A-Za-z0-9_.])\d{4}[-/.]\d{1,2}[-/.]\d{1,2}",
        r"(?<![A-Za-z0-9_.])\d{4}年\d{1,2}月\d{1,2}日",
        r"(?<![A-Za-z0-9_.])\d{1,2}月\d{1,2}日",
    )
)


class ProductLLMProviderError(RuntimeError):
    """The provider did not return a usable, schema-valid response."""


class ProductLLMNotConfiguredError(ProductLLMProviderError):
    """No concrete API key is available to the live model path."""


class ProductLLMConfigurationError(ValueError):
    """A public Product LLM setting is malformed."""


class _SemanticBindingRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    binding_index: int = Field(..., ge=0)
    binding_id: str = Field(..., min_length=1)
    replacement_rendered_text: str = Field(..., min_length=1, max_length=1600)


class _SemanticBindingRepairPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    repairs: list[_SemanticBindingRepair] = Field(default_factory=list, max_length=60)


class _NumericTextRepairChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_id: str = Field(..., min_length=1)
    replacement: Literal["当晚", "夜间", "近期"]


class _NumericTextRepairPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    repairs: list[_NumericTextRepairChoice] = Field(
        default_factory=list,
        max_length=20,
    )


@dataclass(frozen=True)
class _NumericTextRepairCandidate:
    candidate_id: str
    start: int
    end: int
    original_text: str
    offending_spans: tuple[tuple[int, int, str], ...]


@dataclass(frozen=True)
class OpenAICompatibleProviderConfig:
    model: str = DEFAULT_PRODUCT_LLM_MODEL
    api_key_env: str = PRODUCT_LLM_API_KEY_ENV
    base_url: str = DEFAULT_PRODUCT_LLM_BASE_URL
    temperature: float = 0.2
    timeout_seconds: float = 30.0
    retry: int = 1
    max_output_tokens: int = 4000
    thinking_type: str | None = "disabled"


def openai_compatible_provider_config_from_env(
    environment: Mapping[str, str] | None = None,
) -> OpenAICompatibleProviderConfig:
    """读取公开配置；API key 只在 provider 构造时读取且不会进入配置对象。"""

    env = os.environ if environment is None else environment
    model = _first_optional_setting(
        env,
        (PRODUCT_LLM_MODEL_ENV, LEGACY_PRODUCT_LLM_MODEL_ENV),
        DEFAULT_PRODUCT_LLM_MODEL,
    )
    base_url = _first_optional_setting(
        env,
        (PRODUCT_LLM_BASE_URL_ENV, LEGACY_PRODUCT_LLM_BASE_URL_ENV),
        DEFAULT_PRODUCT_LLM_BASE_URL,
    ).rstrip("/")
    _validate_base_url(base_url)
    return OpenAICompatibleProviderConfig(
        model=model,
        api_key_env=(
            PRODUCT_LLM_API_KEY_ENV
            if _usable_secret(env.get(PRODUCT_LLM_API_KEY_ENV)) is not None
            else LEGACY_PRODUCT_LLM_API_KEY_ENV
            if _usable_secret(env.get(LEGACY_PRODUCT_LLM_API_KEY_ENV)) is not None
            else PRODUCT_LLM_API_KEY_ENV
        ),
        base_url=base_url,
        timeout_seconds=_first_positive_float_setting(
            env,
            (
                PRODUCT_LLM_TIMEOUT_SECONDS_ENV,
                LEGACY_PRODUCT_LLM_TIMEOUT_SECONDS_ENV,
            ),
            30.0,
        ),
        max_output_tokens=_positive_int_setting(
            env, PRODUCT_LLM_MAX_TOKENS_ENV, 4000
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
        max_output_tokens: int = 4000,
        thinking_type: str | None = "disabled",
        retry: int = 1,
    ) -> dict[str, Any]: ...


class ProviderTransportAuditObserver(Protocol):
    """Process-local audit hook that must retain only safe request metadata."""

    def before_request(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        attempt: int,
    ) -> None: ...

    def after_response(
        self,
        *,
        model: str,
        attempt: int,
        status_code: int,
        request_id_present: bool,
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int,
    ) -> None: ...

    def after_failure(
        self,
        *,
        model: str,
        attempt: int,
        error_type: str,
        latency_ms: int,
    ) -> None: ...


_PROVIDER_TRANSPORT_AUDIT_OBSERVER: ContextVar[
    ProviderTransportAuditObserver | None
] = ContextVar("sleepagent_provider_transport_audit_observer", default=None)


@contextmanager
def provider_transport_audit_scope(
    observer: ProviderTransportAuditObserver,
) -> Iterator[None]:
    """Observe the exact transport boundary without retaining prompt content."""

    token: Token[ProviderTransportAuditObserver | None] = (
        _PROVIDER_TRANSPORT_AUDIT_OBSERVER.set(observer)
    )
    try:
        yield
    finally:
        _PROVIDER_TRANSPORT_AUDIT_OBSERVER.reset(token)


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
        max_output_tokens: int = 4000,
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
                started_ns = time.monotonic_ns()
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
                    observer = _PROVIDER_TRANSPORT_AUDIT_OBSERVER.get()
                    if observer is not None:
                        # The observer sees the exact messages synchronously and
                        # must fail closed here, before bytes leave the process.
                        # The provider boundary itself never persists them.
                        observer.before_request(
                            model=model,
                            messages=messages,
                            attempt=attempt + 1,
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
                    usage = payload.get("usage")
                    prompt_tokens = None
                    completion_tokens = None
                    if isinstance(usage, Mapping):
                        raw_prompt_tokens = usage.get("prompt_tokens")
                        raw_completion_tokens = usage.get("completion_tokens")
                        if (
                            isinstance(raw_prompt_tokens, int)
                            and raw_prompt_tokens >= 0
                        ):
                            prompt_tokens = raw_prompt_tokens
                        if (
                            isinstance(raw_completion_tokens, int)
                            and raw_completion_tokens >= 0
                        ):
                            completion_tokens = raw_completion_tokens
                    request_id = payload.get("id")
                    if observer is not None:
                        observer.after_response(
                            model=model,
                            attempt=attempt + 1,
                            status_code=status,
                            request_id_present=bool(
                                isinstance(request_id, str)
                                and request_id.strip()
                            ),
                            input_tokens=prompt_tokens,
                            output_tokens=completion_tokens,
                            latency_ms=max(
                                0,
                                int((time.monotonic_ns() - started_ns) / 1_000_000),
                            ),
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
                    observer = _PROVIDER_TRANSPORT_AUDIT_OBSERVER.get()
                    if observer is not None:
                        observer.after_failure(
                            model=model,
                            attempt=attempt + 1,
                            error_type=type(exc).__name__,
                            latency_ms=max(
                                0,
                                int((time.monotonic_ns() - started_ns) / 1_000_000),
                            ),
                        )
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
        self.last_provider_input_tokens: int | None = None

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

    def _request_content(self, messages: list[dict[str, str]]) -> str:
        response = self._provider.create_chat_completion(
            model=self.config.model,
            messages=messages,
            temperature=self.config.temperature,
            max_output_tokens=self.config.max_output_tokens,
            thinking_type=self.config.thinking_type,
            retry=self.config.retry,
        )
        request_id = response.get("id") if isinstance(response, dict) else None
        if isinstance(request_id, str) and request_id.strip():
            self.last_provider_request_id = request_id.strip()
        usage = response.get("usage") if isinstance(response, dict) else None
        if isinstance(usage, Mapping):
            prompt_tokens = usage.get("prompt_tokens")
            if isinstance(prompt_tokens, int) and prompt_tokens >= 0:
                self.last_provider_input_tokens = prompt_tokens
        return _message_content(response)

    def _repair_semantic_binding_substrings(
        self,
        *,
        payload: Any,
        invalid_content: str,
        messages: list[dict[str, str]],
        schema: type[SchemaT],
        validation_error: ValidationError,
    ) -> SchemaT:
        repair_context = _semantic_binding_substring_context(payload)
        if repair_context is None:
            raise ProductLLMProviderError(
                "Structured Agent exact-substring repair could not identify "
                "mismatched semantic bindings."
            ) from validation_error
        repair_instruction = {
            "role": "system",
            "content": (
                "Return one semantic-binding repair patch JSON object only. This "
                "is an error-specific patch, not a regenerated Agent output. It "
                "must validate against this exact patch schema: "
                + json.dumps(
                    _SemanticBindingRepairPatch.model_json_schema(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + " Communication.text is immutable and is the sole source of "
                "replacement text. Return exactly one repair for every mismatch "
                "listed in the context, using the same binding_index and binding_id. "
                "replacement_rendered_text must be copied character-for-character "
                "from one exact contiguous substring of communication_text and must "
                "remain supported by the unchanged source_kind and source_ref shown "
                "for that binding. Do not return Communication.text, a complete "
                "Agent output, source_kind, source_ref, or independently paraphrased "
                "wording. If no source-supported exact substring exists, do not "
                "invent one; an empty or invalid patch will be rejected fail closed. "
                "Repair context: "
                + json.dumps(
                    repair_context,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        }
        repair_content = self._request_content(
            [
                repair_instruction,
                *messages,
                {"role": "assistant", "content": invalid_content},
                {
                    "role": "user",
                    "content": (
                        "Return only the narrow semantic-binding repair patch. "
                        "Select each replacement verbatim from the immutable "
                        "communication_text."
                    ),
                },
            ]
        )
        try:
            raw_patch: Any = json.loads(repair_content)
        except json.JSONDecodeError as exc:
            raise ProductLLMProviderError(
                "Structured Agent exact-substring repair returned invalid JSON."
            ) from exc
        try:
            patch = _SemanticBindingRepairPatch.model_validate(raw_patch)
        except ValidationError as exc:
            raise ProductLLMProviderError(
                "Structured Agent exact-substring repair returned an invalid patch."
            ) from exc
        repaired_payload = _apply_semantic_binding_repair_patch(
            payload=payload,
            repair_context=repair_context,
            patch=patch,
        )
        try:
            return schema.model_validate(repaired_payload)
        except ValidationError as exc:
            errors = exc.errors(include_url=False, include_input=False)
            if _has_only_unbound_numeric_errors(errors):
                return self._repair_unbound_numeric_tokens_once(
                    payload=repaired_payload,
                    errors=errors,
                    messages=messages,
                    schema=schema,
                )
            raise ProductLLMProviderError(
                f"Structured Agent response failed {schema.__name__} validation "
                "after constrained exact-substring repair."
            ) from exc

    def _repair_unbound_numeric_tokens_once(
        self,
        *,
        payload: Any,
        errors: list[dict[str, Any]],
        messages: list[dict[str, str]],
        schema: type[SchemaT],
    ) -> SchemaT:
        candidates, required_spans = _numeric_repair_candidates(
            payload=payload,
            errors=errors,
        )
        if not candidates or not _candidate_set_covers_required_spans(
            candidates,
            required_spans,
        ):
            raise ProductLLMProviderError(
                "Structured Agent numeric repair found no safe incidental "
                "calendar-date candidate."
            )
        candidate_context = [
            {
                "candidate_id": candidate.candidate_id,
                "original_text": candidate.original_text,
                "covered_offending_tokens": sorted(
                    {span[2] for span in candidate.offending_spans}
                ),
                "allowed_replacements": list(_NUMERIC_REPAIR_REPLACEMENTS),
            }
            for candidate in candidates
        ]
        repair_instruction = {
            "role": "system",
            "content": (
                "Return one numeric text repair patch JSON object only. This is an "
                "error-specific candidate selection, not a regenerated Agent output. "
                "It must validate against this exact patch schema: "
                + json.dumps(
                    _NumericTextRepairPatch.model_json_schema(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + " Select only candidate_id values supplied below and one of each "
                "candidate's allowed replacement enum values. Return no complete "
                "Agent output, Communication text, offsets, source_ref, binding, or "
                "free-form replacement text. Every offending numeric occurrence must "
                "be covered exactly once. If no candidate is safe, do not invent one; "
                "an incomplete or invalid patch will be rejected fail closed. "
                "Candidates: "
                + json.dumps(
                    candidate_context,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        }
        repair_content = self._request_content(
            [
                repair_instruction,
                *messages,
                {
                    "role": "user",
                    "content": (
                        "Select the complete safe incidental calendar-date candidates "
                        "needed to cover every offending numeric occurrence. Return "
                        "only candidate_id and replacement enum values."
                    ),
                },
            ]
        )
        try:
            raw_patch: Any = json.loads(repair_content)
        except json.JSONDecodeError as exc:
            raise ProductLLMProviderError(
                "Structured Agent numeric repair returned invalid patch JSON."
            ) from exc
        try:
            patch = _NumericTextRepairPatch.model_validate(raw_patch)
        except ValidationError as exc:
            raise ProductLLMProviderError(
                "Structured Agent numeric repair returned an invalid patch."
            ) from exc
        repaired_payload = _apply_numeric_text_repair_patch(
            payload=payload,
            candidates=candidates,
            required_spans=required_spans,
            patch=patch,
        )
        try:
            return schema.model_validate(repaired_payload)
        except ValidationError as exc:
            raise ProductLLMProviderError(
                f"Structured Agent response failed {schema.__name__} validation "
                "after one constrained numeric repair attempt."
            ) from exc

    def generate(
        self,
        *,
        messages: list[dict[str, str]],
        schema: type[SchemaT],
        prompt_version: str,
        context_packet_id: str,
    ) -> SchemaT:
        self.last_provider_request_id = None
        self.last_provider_input_tokens = None
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
        correction = ""
        previous_invalid_content: str | None = None
        last_error: Exception | None = None
        for semantic_attempt in range(2):
            current_schema_instruction = dict(schema_instruction)
            if correction:
                current_schema_instruction["content"] += (
                    " The previous live response failed strict validation. "
                    "Regenerate the complete JSON object and correct every listed "
                    "error by changing the offending fields, not by explaining "
                    "the error. Preserve fields that already validate. "
                    f"Errors: {correction}"
                )
            request_messages = [current_schema_instruction, *messages]
            if correction and previous_invalid_content is not None:
                request_messages.extend(
                    [
                        {
                            "role": "assistant",
                            "content": previous_invalid_content,
                        },
                        {
                            "role": "user",
                            "content": (
                                "Correct the preceding invalid JSON object in place "
                                "using the validation errors and any exact offending "
                                "tokens in the system instruction. Return the complete "
                                "corrected JSON object only."
                            ),
                        },
                    ]
                )
            content = self._request_content(request_messages)
            try:
                payload: Any = json.loads(content)
            except json.JSONDecodeError as exc:
                last_error = exc
                previous_invalid_content = content
                correction = (
                    "invalid JSON; return one syntactically complete object with "
                    "double-quoted property names and no trailing content."
                )
                if semantic_attempt == 0:
                    continue
                raise ProductLLMProviderError(
                    "Structured Agent provider returned invalid JSON after one "
                    "live correction attempt."
                ) from exc
            try:
                return schema.model_validate(payload)
            except ValidationError as exc:
                last_error = exc
                errors = exc.errors(include_url=False, include_input=False)
                if semantic_attempt == 0 and _has_exact_substring_error(errors):
                    return self._repair_semantic_binding_substrings(
                        payload=payload,
                        invalid_content=content,
                        messages=messages,
                        schema=schema,
                        validation_error=exc,
                    )
                if _has_only_unbound_numeric_errors(errors):
                    if semantic_attempt == 0:
                        return self._repair_unbound_numeric_tokens_once(
                            payload=payload,
                            errors=errors,
                            messages=messages,
                            schema=schema,
                        )
                    raise ProductLLMProviderError(
                        f"Structured Agent response failed {schema.__name__} "
                        "validation with unbound numeric tokens after one live "
                        "correction attempt."
                    ) from exc
                if _has_unbound_numeric_error(errors):
                    raise ProductLLMProviderError(
                        f"Structured Agent response failed {schema.__name__} "
                        "validation with mixed numeric and non-numeric errors."
                    ) from exc
                previous_invalid_content = content
                correction = _validation_correction(
                    errors,
                )
                if semantic_attempt == 0:
                    continue
                raise ProductLLMProviderError(
                    f"Structured Agent response failed {schema.__name__} "
                    "validation after one live correction attempt."
                ) from exc
        raise ProductLLMProviderError(
            "Structured Agent live correction ended without a validated response."
        ) from last_error


def _validation_correction(
    errors: list[dict[str, Any]],
) -> str:
    offending_numeric_tokens = _offending_numeric_tokens(errors)
    correction_context: dict[str, Any] = {
        "validation_errors": errors,
        "offending_numeric_tokens": sorted(offending_numeric_tokens),
    }
    return json.dumps(
        correction_context,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _offending_numeric_tokens(errors: list[dict[str, Any]]) -> set[str]:
    offending_numeric_tokens: set[str] = set()
    for error in errors:
        message = str(error.get("msg") or "")
        if _UNBOUND_NUMERIC_TOKEN_MARKER not in message:
            continue
        serialized_tokens = message.split(
            _UNBOUND_NUMERIC_TOKEN_MARKER, 1
        )[1].strip()
        try:
            parsed_tokens = ast.literal_eval(serialized_tokens)
        except (SyntaxError, ValueError):
            continue
        if isinstance(parsed_tokens, list):
            offending_numeric_tokens.update(
                token for token in parsed_tokens if isinstance(token, str)
            )
    return offending_numeric_tokens


def _has_exact_substring_error(errors: list[dict[str, Any]]) -> bool:
    return any(
        _EXACT_SUBSTRING_VALIDATION_ERROR in str(error.get("msg") or "")
        for error in errors
    )


def _has_unbound_numeric_error(errors: list[dict[str, Any]]) -> bool:
    return any(
        _UNBOUND_NUMERIC_TOKEN_MARKER in str(error.get("msg") or "")
        for error in errors
    )


def _has_only_unbound_numeric_errors(errors: list[dict[str, Any]]) -> bool:
    return bool(errors) and all(
        _UNBOUND_NUMERIC_TOKEN_MARKER in str(error.get("msg") or "")
        for error in errors
    )


def _numeric_repair_candidates(
    *,
    payload: Any,
    errors: list[dict[str, Any]],
) -> tuple[
    tuple[_NumericTextRepairCandidate, ...],
    tuple[tuple[int, int, str], ...],
]:
    if not isinstance(payload, Mapping):
        return (), ()
    communication = payload.get("output_payload")
    if not isinstance(communication, Mapping):
        return (), ()
    text = communication.get("text")
    bindings = communication.get("semantic_bindings")
    if not isinstance(text, str) or not isinstance(bindings, list):
        return (), ()
    offending_tokens = _offending_numeric_tokens(errors)
    required_spans = tuple(
        (match.start(), match.end(), match.group(0))
        for match in _NUMERIC_TOKEN_PATTERN.finditer(text)
        if match.group(0) in offending_tokens
    )
    if not offending_tokens or not required_spans:
        return (), required_spans
    binding_spans = _semantic_binding_spans(text=text, bindings=bindings)
    raw_matches: dict[tuple[int, int], tuple[int, int, str]] = {}
    for pattern in _NUMERIC_REPAIR_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            original_text = match.group(0)
            numeric_tokens = {
                item.group(0) for item in _NUMERIC_TOKEN_PATTERN.finditer(original_text)
            }
            if not numeric_tokens or not numeric_tokens.issubset(offending_tokens):
                continue
            if not any(
                start <= required_start and required_end <= end
                for required_start, required_end, _ in required_spans
            ):
                continue
            if any(
                _spans_overlap((start, end), binding_span)
                for binding_span in binding_spans
            ):
                continue
            raw_matches[(start, end)] = (start, end, original_text)

    selected_matches: list[tuple[int, int, str]] = []
    for candidate_match in sorted(
        raw_matches.values(),
        key=lambda item: (item[0], -(item[1] - item[0]), item[2]),
    ):
        candidate_span = candidate_match[:2]
        if any(
            _spans_overlap(candidate_span, selected[:2])
            for selected in selected_matches
        ):
            continue
        selected_matches.append(candidate_match)

    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    candidates: list[_NumericTextRepairCandidate] = []
    for start, end, original_text in selected_matches:
        covered_spans = tuple(
            span
            for span in required_spans
            if start <= span[0] and span[1] <= end
        )
        if not covered_spans:
            continue
        candidate_material = json.dumps(
            {
                "text_hash": text_hash,
                "start": start,
                "end": end,
                "original_text": original_text,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        candidates.append(
            _NumericTextRepairCandidate(
                candidate_id=(
                    "numeric-candidate:"
                    + hashlib.sha256(candidate_material.encode("utf-8")).hexdigest()[:24]
                ),
                start=start,
                end=end,
                original_text=original_text,
                offending_spans=covered_spans,
            )
        )
    return tuple(candidates), required_spans


def _semantic_binding_spans(
    *,
    text: str,
    bindings: list[Any],
) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    for binding in bindings:
        if not isinstance(binding, Mapping):
            continue
        rendered_text = binding.get("rendered_text")
        if not isinstance(rendered_text, str) or not rendered_text:
            continue
        search_start = 0
        while True:
            start = text.find(rendered_text, search_start)
            if start < 0:
                break
            end = start + len(rendered_text)
            spans.append((start, end))
            search_start = start + 1
    return tuple(spans)


def _candidate_set_covers_required_spans(
    candidates: tuple[_NumericTextRepairCandidate, ...],
    required_spans: tuple[tuple[int, int, str], ...],
) -> bool:
    return bool(required_spans) and all(
        sum(
            candidate.start <= required_start and required_end <= candidate.end
            for candidate in candidates
        )
        == 1
        for required_start, required_end, _ in required_spans
    )


def _apply_numeric_text_repair_patch(
    *,
    payload: Any,
    candidates: tuple[_NumericTextRepairCandidate, ...],
    required_spans: tuple[tuple[int, int, str], ...],
    patch: _NumericTextRepairPatch,
) -> Any:
    candidate_by_id = {
        candidate.candidate_id: candidate for candidate in candidates
    }
    selected: list[tuple[_NumericTextRepairCandidate, str]] = []
    seen_ids: set[str] = set()
    for repair in patch.repairs:
        if repair.candidate_id in seen_ids:
            raise ProductLLMProviderError(
                "Structured Agent numeric repair duplicated a candidate."
            )
        seen_ids.add(repair.candidate_id)
        candidate = candidate_by_id.get(repair.candidate_id)
        if candidate is None:
            raise ProductLLMProviderError(
                "Structured Agent numeric repair selected an unknown candidate."
            )
        if repair.replacement not in _NUMERIC_REPAIR_REPLACEMENTS or (
            _NUMERIC_TOKEN_PATTERN.search(repair.replacement) is not None
        ):
            raise ProductLLMProviderError(
                "Structured Agent numeric repair selected an invalid replacement."
            )
        selected.append((candidate, repair.replacement))

    selected_candidates = tuple(item[0] for item in selected)
    ordered_candidates = sorted(
        selected_candidates,
        key=lambda candidate: (candidate.start, candidate.end),
    )
    if any(
        _spans_overlap(
            (left.start, left.end),
            (right.start, right.end),
        )
        for left, right in zip(ordered_candidates, ordered_candidates[1:])
    ):
        raise ProductLLMProviderError(
            "Structured Agent numeric repair selected overlapping candidates."
        )
    if not _candidate_set_covers_required_spans(
        selected_candidates,
        required_spans,
    ):
        raise ProductLLMProviderError(
            "Structured Agent numeric repair did not cover every offending numeric "
            "occurrence exactly once."
        )

    repaired_payload = copy.deepcopy(payload)
    if not isinstance(repaired_payload, dict):
        raise ProductLLMProviderError(
            "Structured Agent numeric repair payload is malformed."
        )
    communication = repaired_payload.get("output_payload")
    if not isinstance(communication, dict):
        raise ProductLLMProviderError(
            "Structured Agent numeric repair payload is malformed."
        )
    text = communication.get("text")
    bindings = communication.get("semantic_bindings")
    if not isinstance(text, str) or not isinstance(bindings, list):
        raise ProductLLMProviderError(
            "Structured Agent numeric repair payload is malformed."
        )
    binding_spans = _semantic_binding_spans(text=text, bindings=bindings)
    replacement_by_id = {item[0].candidate_id: item[1] for item in selected}
    for candidate in ordered_candidates:
        if text[candidate.start : candidate.end] != candidate.original_text:
            raise ProductLLMProviderError(
                "Structured Agent numeric repair candidate original text changed."
            )
        if any(
            _spans_overlap((candidate.start, candidate.end), binding_span)
            for binding_span in binding_spans
        ):
            raise ProductLLMProviderError(
                "Structured Agent numeric repair candidate overlaps a semantic binding."
            )

    original_frozen = _payload_without_communication_text(payload)
    for candidate in reversed(ordered_candidates):
        replacement = replacement_by_id[candidate.candidate_id]
        text = text[: candidate.start] + replacement + text[candidate.end :]
    communication["text"] = text
    if _payload_without_communication_text(repaired_payload) != original_frozen:
        raise ProductLLMProviderError(
            "Structured Agent numeric repair changed frozen payload fields."
        )
    return repaired_payload


def _payload_without_communication_text(payload: Any) -> Any:
    frozen = copy.deepcopy(payload)
    if not isinstance(frozen, dict):
        return frozen
    communication = frozen.get("output_payload")
    if isinstance(communication, dict):
        communication.pop("text", None)
    return frozen


def _spans_overlap(
    left: tuple[int, int],
    right: tuple[int, int],
) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _semantic_binding_substring_context(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    communication = payload.get("output_payload")
    if not isinstance(communication, Mapping):
        return None
    text = communication.get("text")
    bindings = communication.get("semantic_bindings")
    if not isinstance(text, str) or not isinstance(bindings, list):
        return None
    mismatches: list[dict[str, Any]] = []
    for index, binding in enumerate(bindings):
        if not isinstance(binding, Mapping):
            continue
        rendered_text = binding.get("rendered_text")
        if not isinstance(rendered_text, str) or rendered_text in text:
            continue
        mismatches.append(
            {
                "binding_index": index,
                "binding_id": binding.get("binding_id"),
                "source_kind": binding.get("source_kind"),
                "source_ref": binding.get("source_ref"),
                "mismatched_rendered_text": rendered_text,
            }
        )
    if not mismatches:
        return None
    return {
        "audience_role": communication.get("audience_role"),
        "communication_text": text,
        "mismatched_bindings": mismatches,
    }


def _apply_semantic_binding_repair_patch(
    *,
    payload: Any,
    repair_context: Mapping[str, Any],
    patch: _SemanticBindingRepairPatch,
) -> Any:
    communication_text = repair_context.get("communication_text")
    mismatch_entries = repair_context.get("mismatched_bindings")
    if not isinstance(communication_text, str) or not isinstance(
        mismatch_entries, list
    ):
        raise ProductLLMProviderError(
            "Structured Agent exact-substring repair context is malformed."
        )
    expected_by_index: dict[int, Mapping[str, Any]] = {}
    for entry in mismatch_entries:
        if not isinstance(entry, Mapping):
            raise ProductLLMProviderError(
                "Structured Agent exact-substring repair context is malformed."
            )
        index = entry.get("binding_index")
        if not isinstance(index, int) or index in expected_by_index:
            raise ProductLLMProviderError(
                "Structured Agent exact-substring repair context is malformed."
            )
        expected_by_index[index] = entry
    repairs_by_index: dict[int, _SemanticBindingRepair] = {}
    for repair in patch.repairs:
        if repair.binding_index in repairs_by_index:
            raise ProductLLMProviderError(
                "Structured Agent exact-substring repair duplicated a binding."
            )
        repairs_by_index[repair.binding_index] = repair
    if repairs_by_index.keys() != expected_by_index.keys():
        raise ProductLLMProviderError(
            "Structured Agent exact-substring repair did not exactly cover every "
            "mismatched binding."
        )

    repaired_payload = copy.deepcopy(payload)
    if not isinstance(repaired_payload, dict):
        raise ProductLLMProviderError(
            "Structured Agent exact-substring repair payload is malformed."
        )
    communication = repaired_payload.get("output_payload")
    if not isinstance(communication, dict):
        raise ProductLLMProviderError(
            "Structured Agent exact-substring repair payload is malformed."
        )
    bindings = communication.get("semantic_bindings")
    if communication.get("text") != communication_text or not isinstance(
        bindings, list
    ):
        raise ProductLLMProviderError(
            "Structured Agent exact-substring repair could not freeze Communication.text."
        )

    for index, expected in expected_by_index.items():
        repair = repairs_by_index[index]
        if repair.binding_id != expected.get("binding_id"):
            raise ProductLLMProviderError(
                "Structured Agent exact-substring repair changed binding identity."
            )
        if repair.replacement_rendered_text not in communication_text:
            raise ProductLLMProviderError(
                "Structured Agent exact-substring repair replacement is not an "
                "exact Communication.text substring."
            )
        try:
            binding = bindings[index]
        except IndexError as exc:
            raise ProductLLMProviderError(
                "Structured Agent exact-substring repair binding index is invalid."
            ) from exc
        if not isinstance(binding, dict):
            raise ProductLLMProviderError(
                "Structured Agent exact-substring repair binding is malformed."
            )
        original_source = (binding.get("source_kind"), binding.get("source_ref"))
        expected_source = (expected.get("source_kind"), expected.get("source_ref"))
        if original_source != expected_source:
            raise ProductLLMProviderError(
                "Structured Agent exact-substring repair source identity is malformed."
            )
        binding["rendered_text"] = repair.replacement_rendered_text
        if (binding.get("source_kind"), binding.get("source_ref")) != original_source:
            raise ProductLLMProviderError(
                "Structured Agent exact-substring repair changed source identity."
            )

    if communication.get("text") != communication_text:
        raise ProductLLMProviderError(
            "Structured Agent exact-substring repair changed Communication.text."
        )
    return repaired_payload


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


def _first_optional_setting(
    env: Mapping[str, str], names: tuple[str, ...], default: str
) -> str:
    for name in names:
        value = env.get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _first_positive_float_setting(
    env: Mapping[str, str], names: tuple[str, ...], default: float
) -> float:
    for name in names:
        value = env.get(name)
        if value is not None and value.strip():
            return _positive_float_setting(env, name, default)
    return default


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
    "LEGACY_PRODUCT_LLM_API_KEY_ENV",
    "LEGACY_PRODUCT_LLM_BASE_URL_ENV",
    "LEGACY_PRODUCT_LLM_MODEL_ENV",
    "LEGACY_PRODUCT_LLM_TIMEOUT_SECONDS_ENV",
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
