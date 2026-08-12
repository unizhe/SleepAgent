from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Generic, Literal, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

if TYPE_CHECKING:
    from sleepagent.product_runtime.schemas import ContextPacket


SchemaT = TypeVar("SchemaT", bound=BaseModel)
AuditSink = Callable[["LLMInvocationAudit"], None]


def _default_audit_sink(record: "LLMInvocationAudit") -> None:
    from sleepagent.observability import log_event

    log_event("radar_llm_invocation", **record.model_dump(mode="json"))


def task_service_llm_audit_sink(
    task_service: Any,
    *,
    task_id: str,
    actor: str = "model_router",
) -> AuditSink:
    """Persist minimized LLM invocation summaries in WorkflowRuntime audit."""

    def sink(record: LLMInvocationAudit) -> None:
        task_service.record_audit(
            task_id,
            actor=actor,
            action=f"llm_{record.phase}",
            target_ref=record.model_id,
            summary=(
                record.fallback_summary
                or record.error_summary
                or record.output_schema_status
            ),
            payload=_minimized_llm_audit_payload(record),
        )

    return sink


def _minimized_llm_audit_payload(record: "LLMInvocationAudit") -> dict[str, Any]:
    """Explicit allowlist prevents future audit fields from leaking prompt data."""

    return {
        "phase": record.phase,
        "model_provider": record.model_provider,
        "model_id": record.model_id,
        "prompt_version": record.prompt_version,
        "context_packet_id": record.context_packet_id,
        "output_schema_status": record.output_schema_status,
        "attempt_count": record.attempt_count,
        "input_summary": {
            key: value
            for key, value in record.input_summary.items()
            if key
            in {
                "message_count",
                "roles",
                "prompt_sha256",
                "context_packet_id",
            }
        },
        "output_summary": {
            key: value
            for key, value in record.output_summary.items()
            if key in {"schema", "content_characters"}
        },
        "error_summary": record.error_summary,
        "fallback_summary": record.fallback_summary,
        "created_at": record.created_at.isoformat(),
    }


class CloudLLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(..., min_length=1)
    api_key: str | None = Field(default=None, repr=False, exclude=True)
    model_id: str = Field(..., min_length=1)
    timeout: float = Field(default=30.0, gt=0)
    retry: int = Field(default=1, ge=0, le=5)
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_output_tokens: int = Field(default=1600, ge=1)
    retry_backoff_seconds: float = Field(default=0, ge=0)
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def protect_openai_compatible_core_fields(self) -> "CloudLLMConfig":
        reserved = {
            "model",
            "messages",
            "temperature",
            "max_tokens",
            "stream",
            "response_format",
        }
        overlap = sorted(reserved & set(self.extra_body))
        if overlap:
            raise ValueError(
                "extra_body cannot override core fields: " + ", ".join(overlap)
            )
        return self


class ModelInvocationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_provider: str = Field(default="openai-compatible", min_length=1)
    model_id: str = Field(..., min_length=1)
    prompt_version: str = Field(..., min_length=1)
    context_packet_id: str | None = None
    output_schema_status: Literal[
        "not_validated",
        "pending",
        "valid",
        "invalid",
        "fallback",
        "not_applicable",
    ] = "not_validated"
    fallback_used: bool = False
    fallback_summary: str | None = None
    fallback_reason: str | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LLMInvocationAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: Literal["completed", "failed", "fallback"]
    model_provider: str
    model_id: str
    prompt_version: str
    context_packet_id: str | None = None
    output_schema_status: Literal[
        "not_validated", "valid", "invalid", "fallback", "not_applicable"
    ]
    attempt_count: int = Field(ge=0)
    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    error_summary: str | None = None
    fallback_summary: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CloudLLMError(RuntimeError):
    retryable = False


class CloudLLMNotConfiguredError(CloudLLMError):
    pass


class CloudLLMTimeoutError(CloudLLMError):
    retryable = True


class CloudLLMRateLimitError(CloudLLMError):
    retryable = True


class CloudLLMProviderError(CloudLLMError):
    retryable = True


class CloudLLMInvalidJSONError(CloudLLMError):
    retryable = True


class CloudLLMSchemaError(CloudLLMError):
    retryable = True


class CloudLLMPrivacyError(CloudLLMError):
    pass


class HTTPClientLike(Protocol):
    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]):
        ...


class LLMFaultInjector(Protocol):
    """Test/demo hook for deterministic provider and output failures."""

    def before_request(self, *, attempt: int, structured: bool) -> None: ...

    def transform_content(self, content: str, *, structured: bool) -> str: ...


class CloudLLMClient:
    """Vendor-neutral client for the OpenAI-compatible chat-completions API."""

    def __init__(
        self,
        config: CloudLLMConfig,
        *,
        http_client: HTTPClientLike | None = None,
        audit_sink: AuditSink | None = None,
        fault_injector: LLMFaultInjector | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._http_client = http_client
        self._audit_sink = audit_sink or _default_audit_sink
        self._fault_injector = fault_injector
        self._sleep = sleep
        self.last_provider_request_id: str | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.config.api_key)

    def generate_structured(
        self,
        *,
        messages: list[dict[str, str]],
        schema: type[SchemaT],
        metadata: ModelInvocationMetadata,
        max_retries: int | None = None,
    ) -> SchemaT:
        safe_messages = validate_cloud_messages(messages)

        def parse(content: str) -> SchemaT:
            try:
                payload = json.loads(content)
            except (json.JSONDecodeError, TypeError) as exc:
                raise CloudLLMInvalidJSONError(
                    "LLM message content was not valid JSON"
                ) from exc
            return self.validate_schema(payload=payload, schema=schema)

        return self._generate(
            messages=safe_messages,
            metadata=metadata,
            response_format={"type": "json_object"},
            parse=parse,
            schema_name=schema.__name__,
            max_retries=1 if max_retries is None else min(max_retries, 1),
        )

    def generate_text(
        self,
        *,
        messages: list[dict[str, str]],
        metadata: ModelInvocationMetadata,
    ) -> str:
        safe_messages = validate_cloud_messages(messages)
        return self._generate(
            messages=safe_messages,
            metadata=metadata,
            response_format=None,
            parse=lambda content: content,
            schema_name=None,
            max_retries=self.config.retry,
        )

    def validate_schema(
        self,
        *,
        payload: dict[str, Any],
        schema: type[SchemaT],
    ) -> SchemaT:
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            raise CloudLLMSchemaError(
                f"LLM output failed {schema.__name__} validation"
            ) from exc

    def complete_raw(
        self,
        *,
        messages: list[dict[str, str]],
        metadata: ModelInvocationMetadata,
        response_format: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Compatibility boundary for legacy adapters; new code uses generate_*()."""

        safe_messages = validate_cloud_messages(messages)
        return self._request_with_retries(
            messages=safe_messages,
            metadata=metadata,
            response_format=response_format,
            parse=lambda response, content: response,
            schema_name=None,
        )

    def _generate(
        self,
        *,
        messages: list[dict[str, str]],
        metadata: ModelInvocationMetadata,
        response_format: dict[str, str] | None,
        parse: Callable[[str], SchemaT],
        schema_name: str | None,
        max_retries: int,
    ) -> SchemaT:
        return self._request_with_retries(
            messages=messages,
            metadata=metadata,
            response_format=response_format,
            parse=lambda response, content: parse(content),
            schema_name=schema_name,
            max_retries=max_retries,
        )

    def _request_with_retries(
        self,
        *,
        messages: list[dict[str, str]],
        metadata: ModelInvocationMetadata,
        response_format: dict[str, str] | None,
        parse: Callable[[dict[str, Any], str], SchemaT],
        schema_name: str | None,
        max_retries: int | None = None,
    ) -> SchemaT:
        metadata = metadata.model_copy(
            update={"model_id": self.config.model_id}
        )
        input_summary = _input_summary(messages, metadata.context_packet_id)
        if not self.config.api_key:
            error = CloudLLMNotConfiguredError("LLM API key is not configured")
            self._audit_sink(
                LLMInvocationAudit(
                    phase="failed",
                    model_provider=metadata.model_provider,
                    model_id=metadata.model_id,
                    prompt_version=metadata.prompt_version,
                    context_packet_id=metadata.context_packet_id,
                    output_schema_status="not_validated",
                    attempt_count=0,
                    input_summary=input_summary,
                    error_summary=error.__class__.__name__,
                )
            )
            raise error
        attempts = 0
        last_error: CloudLLMError | None = None
        retries = (
            self.config.retry
            if max_retries is None
            else min(self.config.retry, max_retries)
        )
        structured = schema_name is not None
        for attempts in range(1, retries + 2):
            try:
                if self._fault_injector is not None:
                    self._fault_injector.before_request(
                        attempt=attempts,
                        structured=structured,
                    )
                response = self._post(messages, response_format=response_format)
                content = _extract_message_content(response)
                if self._fault_injector is not None:
                    content = self._fault_injector.transform_content(
                        content,
                        structured=structured,
                    )
                value = parse(response, content)
                self._audit_sink(
                    LLMInvocationAudit(
                        phase="completed",
                        model_provider=metadata.model_provider,
                        model_id=metadata.model_id,
                        prompt_version=metadata.prompt_version,
                        context_packet_id=metadata.context_packet_id,
                        output_schema_status=(
                            "valid" if schema_name else "not_applicable"
                        ),
                        attempt_count=attempts,
                        input_summary=input_summary,
                        output_summary={
                            "schema": schema_name,
                            "content_characters": len(content),
                        },
                    )
                )
                return value
            except CloudLLMError as exc:
                last_error = exc
                if not exc.retryable or attempts > retries:
                    break
                self._sleep(self.config.retry_backoff_seconds * attempts)
        assert last_error is not None
        self._audit_sink(
            LLMInvocationAudit(
                phase="failed",
                model_provider=metadata.model_provider,
                model_id=metadata.model_id,
                prompt_version=metadata.prompt_version,
                context_packet_id=metadata.context_packet_id,
                output_schema_status=(
                    "invalid"
                    if isinstance(last_error, CloudLLMSchemaError)
                    else "not_validated"
                ),
                attempt_count=attempts,
                input_summary=input_summary,
                error_summary=last_error.__class__.__name__,
            )
        )
        raise last_error

    def _post(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, str] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model_id,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "stream": False,
            **self.config.extra_body,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        owned = self._http_client is None
        client = self._http_client or httpx.Client(timeout=self.config.timeout)
        try:
            try:
                response = client.post(
                    f"{self.config.base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                )
            except httpx.TimeoutException as exc:
                raise CloudLLMTimeoutError("LLM request timed out") from exc
            except httpx.HTTPError as exc:
                raise CloudLLMProviderError("LLM request failed") from exc
            status = int(getattr(response, "status_code", 200))
            if status == 429:
                raise CloudLLMRateLimitError("LLM provider rate limited the request")
            if status >= 400:
                error = CloudLLMProviderError(f"LLM provider returned HTTP {status}")
                error.retryable = status >= 500
                raise error
            try:
                response_payload = response.json()
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                raise CloudLLMInvalidJSONError(
                    "LLM response envelope was not valid JSON"
                ) from exc
            if not isinstance(response_payload, dict):
                raise CloudLLMInvalidJSONError("LLM response envelope must be an object")
            provider_request_id = response_payload.get("id")
            self.last_provider_request_id = (
                str(provider_request_id) if provider_request_id else None
            )
            return response_payload
        finally:
            if owned:
                client.close()


@dataclass(frozen=True)
class ModelRouteResult(Generic[SchemaT]):
    value: SchemaT
    metadata: ModelInvocationMetadata


class ModelRouter:
    """Cloud-only v1 router with provider-neutral fallback and audit policy."""

    def __init__(
        self,
        client: CloudLLMClient,
        *,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self.client = client
        self._audit_sink = audit_sink or _default_audit_sink

    def generate_structured(
        self,
        *,
        messages: list[dict[str, str]],
        schema: type[SchemaT],
        metadata: ModelInvocationMetadata,
        context_packet: ContextPacket | None = None,
        fallback: Callable[[], SchemaT] | None = None,
        fallback_summary: str = "template_fallback",
        result_validator: Callable[[SchemaT], SchemaT] | None = None,
    ) -> ModelRouteResult[SchemaT]:
        metadata = _route_metadata(metadata, self.client, context_packet)
        try:
            routed_messages = _messages_with_context(messages, context_packet)
            value = self.client.generate_structured(
                messages=routed_messages,
                schema=schema,
                metadata=metadata,
            )
            if result_validator is not None:
                try:
                    value = result_validator(value)
                except (ValidationError, ValueError):
                    try:
                        value = self.client.generate_structured(
                            messages=routed_messages,
                            schema=schema,
                            metadata=metadata,
                            max_retries=0,
                        )
                        value = result_validator(value)
                    except (CloudLLMError, ValidationError, ValueError) as retry_exc:
                        raise CloudLLMSchemaError(
                            "LLM output failed application safety validation"
                        ) from retry_exc
            return ModelRouteResult(
                value=value,
                metadata=metadata.model_copy(
                    update={"output_schema_status": "valid"}
                ),
            )
        except CloudLLMError as exc:
            if fallback is None:
                raise
            raw_fallback = fallback()
            try:
                value = schema.model_validate(
                    raw_fallback.model_dump(mode="python")
                    if isinstance(raw_fallback, BaseModel)
                    else raw_fallback
                )
                if result_validator is not None:
                    value = result_validator(value)
            except (ValidationError, ValueError, TypeError) as fallback_exc:
                raise CloudLLMSchemaError(
                    "structured fallback failed schema or safety validation"
                ) from fallback_exc
            fallback_metadata = metadata.model_copy(
                update={
                    "output_schema_status": "fallback",
                    "fallback_used": True,
                    "fallback_summary": fallback_summary,
                    "fallback_reason": exc.__class__.__name__,
                }
            )
            self._record_fallback(fallback_metadata, exc, fallback_summary)
            return ModelRouteResult(value=value, metadata=fallback_metadata)

    def generate_text(
        self,
        *,
        messages: list[dict[str, str]],
        metadata: ModelInvocationMetadata,
        context_packet: ContextPacket | None = None,
        fallback: Callable[[], str] | None = None,
        fallback_summary: str = "text_fallback",
    ) -> ModelRouteResult[str]:
        metadata = _route_metadata(metadata, self.client, context_packet)
        try:
            routed_messages = _messages_with_context(messages, context_packet)
            value = self.client.generate_text(
                messages=routed_messages,
                metadata=metadata,
            )
            return ModelRouteResult(
                value=value,
                metadata=metadata.model_copy(
                    update={"output_schema_status": "not_applicable"}
                ),
            )
        except CloudLLMError as exc:
            if fallback is None:
                raise
            value = fallback()
            if not isinstance(value, str) or not value.strip():
                raise CloudLLMSchemaError(
                    "text fallback must return non-empty text"
                )
            fallback_metadata = metadata.model_copy(
                update={
                    "output_schema_status": "fallback",
                    "fallback_used": True,
                    "fallback_summary": fallback_summary,
                    "fallback_reason": exc.__class__.__name__,
                }
            )
            self._record_fallback(fallback_metadata, exc, fallback_summary)
            return ModelRouteResult(value=value, metadata=fallback_metadata)

    def _record_fallback(
        self,
        metadata: ModelInvocationMetadata,
        error: CloudLLMError,
        fallback_summary: str,
    ) -> None:
        self._audit_sink(
            LLMInvocationAudit(
                phase="fallback",
                model_provider=metadata.model_provider,
                model_id=metadata.model_id,
                prompt_version=metadata.prompt_version,
                context_packet_id=metadata.context_packet_id,
                output_schema_status="fallback",
                attempt_count=0,
                error_summary=error.__class__.__name__,
                fallback_summary=fallback_summary,
            )
        )


_DATA_QUALITY_ALLOWLIST = {
    "data_quality_status",
    "data_coverage_ratio",
    "quality_reasons",
    "blocked_reasons",
    "risk_level",
    "risk_signal_change",
    "trend_result",
    "user_question",
}
_RAW_MARKERS = (
    "radarrawevent",
    "raw_radar",
    "raw_event",
    "vendor_payload",
    "raw_payload",
)


def build_cloud_context_envelope(context: ContextPacket) -> dict[str, Any]:
    """Return the only ContextPacket projection permitted to leave the process."""

    ledger = context.evidence_packet.evidence_ledger
    claims = [] if ledger is None else [
        {
            "claim_id": claim.claim_id,
            "text": claim.text,
            "evidence_refs": claim.evidence_refs,
            "confidence": claim.confidence,
            "risk_level": claim.risk_level.value,
            "uncertainty": claim.uncertainty,
            "caveats": claim.caveats,
        }
        for claim in ledger.claims
    ]
    envelope = {
        "context_packet_id": context.context_packet_id,
        "task_context": {
            "task_id": context.task_context.task_id,
            "trace_id": context.task_context.trace_id,
            "role": context.task_context.role,
            "stage": context.task_context.stage,
            "purpose": context.task_context.purpose,
            "allowed_actions": context.task_context.allowed_actions,
        },
        "night_summaries": [
            _cloud_night_summary(summary)
            for summary in context.evidence_packet.night_summaries
        ],
        "claims": claims,
        "data_quality": {
            key: value
            for key, value in context.evidence_packet.data_quality.items()
            if key in _DATA_QUALITY_ALLOWLIST
        },
        "memory_snippets": list(context.memory_snippets),
        "rag_context": {
            "chunk_ids": context.rag_context.chunk_ids,
            "citation_ids": context.rag_context.citation_ids,
            "snippets": context.rag_context.snippets,
            "caveats": context.rag_context.caveats,
            "source_metadata": [
                {
                    key: value
                    for key, value in item.items()
                    if key
                    in {
                        "chunk_id",
                        "citation_id",
                        "source_type",
                        "review_status",
                        "version",
                        "applicable_role",
                        "safety_notes",
                    }
                }
                for item in context.rag_context.source_metadata
            ],
        },
        "safety_policy": context.safety_policy.model_dump(mode="json"),
    }
    _assert_no_raw_radar(envelope)
    return envelope


def _cloud_night_summary(summary: Any) -> dict[str, Any]:
    allowed = {
        "night_of",
        "timezone_name",
        "night_boundary_start_at",
        "night_boundary_end_at",
        "device_status",
        "sleep_start_at",
        "sleep_end_at",
        "total_sleep_minutes",
        "sleep_score",
        "out_of_bed_count",
        "movement_count",
        "data_coverage_ratio",
        "data_quality_status",
        "confidence_label",
        "health_conclusion_allowed",
        "invalid_reading_count",
        "abnormal_reading_count",
        "missing_intervals",
        "out_of_bed_intervals",
        "not_in_bed_intervals",
        "quality_reasons",
        "blocked_reasons",
        "caveats",
        "explainable_metrics",
    }
    payload = summary.model_dump(mode="json")
    return {key: value for key, value in payload.items() if key in allowed}


def validate_cloud_messages(
    messages: list[dict[str, str]],
) -> list[dict[str, str]]:
    validated: list[dict[str, str]] = []
    for message in messages:
        if set(message) != {"role", "content"}:
            raise CloudLLMPrivacyError("cloud messages require role/content only")
        role = message.get("role")
        content = message.get("content")
        if role not in {"system", "developer", "user", "assistant"}:
            raise CloudLLMPrivacyError("unsupported cloud message role")
        if not isinstance(content, str) or not content.strip():
            raise CloudLLMPrivacyError("cloud message content must be non-empty text")
        _assert_no_raw_radar(content)
        validated.append({"role": role, "content": content})
    return validated


def _messages_with_context(
    messages: list[dict[str, str]],
    context: ContextPacket | None,
) -> list[dict[str, str]]:
    values = validate_cloud_messages(messages)
    if context is None:
        return values
    envelope = build_cloud_context_envelope(context)
    return [
        *values,
        {
            "role": "user",
            "content": "SleepAgent minimized context:\n"
            + json.dumps(envelope, ensure_ascii=False, sort_keys=True),
        },
    ]


def _route_metadata(
    metadata: ModelInvocationMetadata,
    client: CloudLLMClient,
    context: ContextPacket | None,
) -> ModelInvocationMetadata:
    return metadata.model_copy(
        update={
            "model_id": client.config.model_id,
            "context_packet_id": (
                context.context_packet_id
                if context is not None and context.context_packet_id
                else metadata.context_packet_id
            ),
        }
    )


def _assert_no_raw_radar(value: Any) -> None:
    serialized = json.dumps(value, ensure_ascii=False, default=str).lower()
    if any(marker in serialized for marker in _RAW_MARKERS):
        raise CloudLLMPrivacyError("raw radar/vendor payload cannot be sent to cloud LLM")


def _extract_message_content(response: dict[str, Any]) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise CloudLLMInvalidJSONError(
            "LLM response did not include message content"
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise CloudLLMInvalidJSONError("LLM response message content was empty")
    return content


def _input_summary(
    messages: list[dict[str, str]],
    context_packet_id: str | None,
) -> dict[str, Any]:
    digest = hashlib.sha256(
        json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "message_count": len(messages),
        "roles": [message["role"] for message in messages],
        "prompt_sha256": digest,
        "context_packet_id": context_packet_id,
    }


__all__ = [
    "AuditSink",
    "CloudLLMClient",
    "CloudLLMConfig",
    "CloudLLMError",
    "CloudLLMInvalidJSONError",
    "CloudLLMNotConfiguredError",
    "CloudLLMPrivacyError",
    "CloudLLMProviderError",
    "CloudLLMRateLimitError",
    "CloudLLMSchemaError",
    "CloudLLMTimeoutError",
    "LLMInvocationAudit",
    "LLMFaultInjector",
    "ModelInvocationMetadata",
    "ModelRouteResult",
    "ModelRouter",
    "build_cloud_context_envelope",
    "task_service_llm_audit_sink",
    "validate_cloud_messages",
]
