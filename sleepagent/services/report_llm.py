from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from sleepagent.schemas import (
    LLMReportDraft,
    MockSleepReport,
    ReportSummary,
    RetrievedReportKnowledgeChunk,
    SleepAnalysisResult,
)
from sleepagent.services.report_templates import (
    MEDICAL_DISCLAIMER,
    _build_report_knowledge_query,
    _build_report_summary,
    generate_mock_sleep_report,
)
from sleepagent.services.report_retrievers import (
    ReportKnowledgeRetriever,
    ReportRetrieverConfig,
    retrieve_report_context,
)


DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
LLM_REPORT_DRAFT_SCHEMA_VERSION = "stage7.llm_report_draft.v1"
UNSAFE_LLM_REPORT_PATTERNS = (
    "诊断为",
    "确诊",
    "已患",
    "患有阻塞性睡眠呼吸暂停",
    "无需就医",
    "不需要就医",
    "不必就医",
    "无需医生",
    "可以自行治疗",
    "停止用药",
    "马上服用",
    "必须服用",
    "diagnosed with",
    "you have obstructive sleep apnea",
    "you definitely have",
    "no need to see a doctor",
    "do not seek medical care",
    "stop taking",
    "start taking",
)


class LLMReportValidationError(ValueError):
    """Raised when LLM report JSON fails the Stage 7 draft schema."""


class DeepSeekAPIError(RuntimeError):
    """Raised when the DeepSeek API boundary cannot return usable content."""


class DeepSeekMissingAPIKeyError(DeepSeekAPIError):
    """Raised when no DeepSeek API key is available for a live request."""


@dataclass(frozen=True)
class DeepSeekReportGeneratorConfig:
    model: str = DEFAULT_DEEPSEEK_MODEL
    api_key_env: str = DEEPSEEK_API_KEY_ENV
    base_url: str = DEFAULT_DEEPSEEK_BASE_URL
    temperature: float = 0.2
    timeout_seconds: float = 30.0
    thinking_type: str = "disabled"


class DeepSeekChatClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_key_env: str = DEEPSEEK_API_KEY_ENV,
        base_url: str = DEFAULT_DEEPSEEK_BASE_URL,
        timeout_seconds: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get(api_key_env)
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    def create_chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        thinking_type: str = "disabled",
    ) -> dict[str, Any]:
        if not self.api_key:
            raise DeepSeekMissingAPIKeyError(
                f"DeepSeek API key is missing. Set {self.api_key_env}."
            )

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "thinking": {"type": thinking_type},
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        client = self._http_client or httpx.Client(timeout=self.timeout_seconds)
        try:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise DeepSeekAPIError("DeepSeek chat completion request failed.") from exc
        except json.JSONDecodeError as exc:
            raise DeepSeekAPIError("DeepSeek response was not valid JSON.") from exc
        finally:
            if self._http_client is None:
                client.close()

    def create_report_draft(
        self,
        *,
        messages: list[dict[str, str]],
        config: DeepSeekReportGeneratorConfig,
    ) -> LLMReportDraft:
        response = self.create_chat_completion(
            model=config.model,
            messages=messages,
            temperature=config.temperature,
            thinking_type=config.thinking_type,
        )
        content = _extract_deepseek_message_content(response)
        try:
            return validate_llm_report_json(content)
        except LLMReportValidationError as exc:
            raise DeepSeekAPIError(
                "DeepSeek response did not match the report draft schema."
            ) from exc


def validate_llm_report_json(raw_json: str | bytes | dict[str, Any]) -> LLMReportDraft:
    try:
        payload = json.loads(raw_json) if isinstance(raw_json, (str, bytes)) else raw_json
        draft = LLMReportDraft.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise LLMReportValidationError("LLM report output failed JSON validation.") from exc
    _validate_llm_report_safety(draft)
    return draft


def build_deepseek_report_messages(
    summary: ReportSummary,
    retrieved_knowledge: list[RetrievedReportKnowledgeChunk],
) -> list[dict[str, str]]:
    context_lines = [
        f"- {item.chunk.chunk_id}: {item.chunk.content}"
        for item in retrieved_knowledge
    ]
    safety_lines = [
        f"- {note}"
        for item in retrieved_knowledge
        for note in item.chunk.safety_notes
    ]
    user_payload = {
        "summary": summary.model_dump(mode="json"),
        "retrieved_context": context_lines,
        "safety_notes": safety_lines,
        "required_json_schema": {
            "schema_version": LLM_REPORT_DRAFT_SCHEMA_VERSION,
            "elder_report": "non-empty string",
            "professional_report": "non-empty string",
            "care_suggestions": ["non-empty string"],
            "safety_warnings": ["non-empty string"],
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "You generate cautious sleep-health reports for SleepAgent. "
                "Return JSON only. Do not diagnose. Use wording such as "
                "'提示', '可能', and '建议进一步检查'. Mention urgent care for "
                "chest pain, severe breathing difficulty, or abnormal consciousness."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False),
        },
    ]


def build_report_from_llm_draft(
    analysis: SleepAnalysisResult,
    draft: LLMReportDraft,
) -> MockSleepReport:
    summary = _build_report_summary(analysis)
    care_suggestions = list(draft.care_suggestions)
    for warning in draft.safety_warnings:
        if warning not in care_suggestions:
            care_suggestions.append(warning)

    return MockSleepReport(
        summary=summary,
        elder_report=draft.elder_report,
        professional_report=draft.professional_report,
        care_suggestions=care_suggestions,
        medical_disclaimer=MEDICAL_DISCLAIMER,
        generated_at=analysis.generated_at,
    )


def generate_sleep_report_with_llm_fallback(
    analysis: SleepAnalysisResult,
    *,
    llm_response_json: str | bytes | dict[str, Any] | None = None,
    retriever: ReportKnowledgeRetriever | None = None,
    retriever_config: ReportRetrieverConfig | None = None,
) -> MockSleepReport:
    if llm_response_json is None:
        return generate_mock_sleep_report(
            analysis,
            retriever=retriever,
            retriever_config=retriever_config,
        )

    try:
        draft = validate_llm_report_json(llm_response_json)
    except LLMReportValidationError:
        return generate_mock_sleep_report(
            analysis,
            retriever=retriever,
            retriever_config=retriever_config,
        )
    return build_report_from_llm_draft(analysis, draft)


def generate_sleep_report_with_deepseek_fallback(
    analysis: SleepAnalysisResult,
    *,
    client: DeepSeekChatClient | None = None,
    config: DeepSeekReportGeneratorConfig | None = None,
    retriever: ReportKnowledgeRetriever | None = None,
    retriever_config: ReportRetrieverConfig | None = None,
) -> MockSleepReport:
    resolved_config = config or DeepSeekReportGeneratorConfig()
    preview = build_deepseek_report_request_preview(
        analysis,
        retriever=retriever,
        retriever_config=retriever_config,
        config=resolved_config,
    )
    resolved_client = client or DeepSeekChatClient(
        api_key_env=resolved_config.api_key_env,
        base_url=resolved_config.base_url,
        timeout_seconds=resolved_config.timeout_seconds,
    )
    try:
        draft = resolved_client.create_report_draft(
            messages=preview["messages"],
            config=resolved_config,
        )
    except DeepSeekAPIError:
        return generate_mock_sleep_report(
            analysis,
            retriever=retriever,
            retriever_config=retriever_config,
        )
    return build_report_from_llm_draft(analysis, draft)


def build_deepseek_report_request_preview(
    analysis: SleepAnalysisResult,
    *,
    retriever: ReportKnowledgeRetriever | None = None,
    retriever_config: ReportRetrieverConfig | None = None,
    config: DeepSeekReportGeneratorConfig | None = None,
) -> dict[str, Any]:
    resolved_config = config or DeepSeekReportGeneratorConfig()
    summary = _build_report_summary(analysis)
    retrieved_knowledge = retrieve_report_context(
        _build_report_knowledge_query(summary),
        top_k=3,
        retriever=retriever,
        config=retriever_config,
    )
    return {
        "model": resolved_config.model,
        "temperature": resolved_config.temperature,
        "api_key_env": resolved_config.api_key_env,
        "base_url": resolved_config.base_url,
        "thinking_type": resolved_config.thinking_type,
        "messages": build_deepseek_report_messages(summary, retrieved_knowledge),
    }


def _extract_deepseek_message_content(response: dict[str, Any]) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DeepSeekAPIError("DeepSeek response did not include message content.") from exc
    if not isinstance(content, str) or not content.strip():
        raise DeepSeekAPIError("DeepSeek response message content was empty.")
    return content


def _validate_llm_report_safety(draft: LLMReportDraft) -> None:
    text_fields = [
        draft.elder_report,
        draft.professional_report,
        *draft.care_suggestions,
        *draft.safety_warnings,
    ]
    combined_text = "\n".join(text_fields).lower()
    for pattern in UNSAFE_LLM_REPORT_PATTERNS:
        if pattern.lower() in combined_text:
            raise LLMReportValidationError(
                "LLM report output failed medical safety validation."
            )
