import json
import subprocess
import sys

import pytest

from sleepagent.preprocessing import generate_mock_sleep_analysis
from sleepagent.services import (
    DeepSeekAPIError,
    DeepSeekChatClient,
    DeepSeekMissingAPIKeyError,
    DeepSeekReportGeneratorConfig,
    DEFAULT_DEEPSEEK_MODEL,
    LLMReportValidationError,
    build_deepseek_report_request_preview,
    generate_sleep_report_with_deepseek_fallback,
    generate_sleep_report_with_llm_fallback,
    validate_llm_report_json,
)


def _valid_llm_payload() -> dict:
    return {
        "schema_version": "stage7.llm_report_draft.v1",
        "elder_report": "这是一份谨慎的老人易懂版报告。",
        "professional_report": "Professional report with cautious wording.",
        "care_suggestions": ["建议继续观察睡眠和白天困倦情况。"],
        "safety_warnings": ["如出现胸痛或严重呼吸困难，应及时就医。"],
    }


class FakeDeepSeekResponse:
    def __init__(self, payload: dict, *, raise_status: bool = False) -> None:
        self.payload = payload
        self.raise_status = raise_status

    def raise_for_status(self) -> None:
        if self.raise_status:
            raise RuntimeError("bad status")

    def json(self) -> dict:
        return self.payload


class FakeHTTPClient:
    def __init__(self, response_payload: dict) -> None:
        self.response_payload = response_payload
        self.post_calls = []
        self.closed = False

    def post(self, url: str, *, headers: dict, json: dict):
        self.post_calls.append({"url": url, "headers": headers, "json": json})
        return FakeDeepSeekResponse(self.response_payload)

    def close(self) -> None:
        self.closed = True


class FakeDeepSeekDraftClient:
    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls = []

    def create_report_draft(self, *, messages, config):
        self.calls.append({"messages": messages, "config": config})
        if self.should_fail:
            raise DeepSeekAPIError("simulated failure")
        return validate_llm_report_json(_valid_llm_payload())


def test_validate_llm_report_json_accepts_strict_payload() -> None:
    draft = validate_llm_report_json(json.dumps(_valid_llm_payload()))

    assert draft.schema_version == "stage7.llm_report_draft.v1"
    assert draft.elder_report.startswith("这是一份")
    assert draft.safety_warnings


def test_validate_llm_report_json_rejects_extra_fields() -> None:
    payload = _valid_llm_payload()
    payload["diagnosis"] = "obstructive sleep apnea"

    with pytest.raises(LLMReportValidationError):
        validate_llm_report_json(payload)


def test_validate_llm_report_json_rejects_wrong_schema_version() -> None:
    payload = _valid_llm_payload()
    payload["schema_version"] = "stage7.other"

    with pytest.raises(LLMReportValidationError):
        validate_llm_report_json(payload)


def test_validate_llm_report_json_allows_cautious_disclaimer_language() -> None:
    payload = _valid_llm_payload()
    payload["professional_report"] = (
        "本报告不能替代医生诊断，仅作为风险提示，建议结合完整 PSG 资料评估。"
    )

    draft = validate_llm_report_json(payload)

    assert "不能替代医生诊断" in draft.professional_report


def test_validate_llm_report_json_rejects_unsafe_diagnostic_language() -> None:
    payload = _valid_llm_payload()
    payload["elder_report"] = "你已确诊阻塞性睡眠呼吸暂停。"

    with pytest.raises(LLMReportValidationError):
        validate_llm_report_json(payload)


def test_generate_sleep_report_with_llm_fallback_uses_valid_llm_sections() -> None:
    analysis = generate_mock_sleep_analysis(duration_hours=0.5, seed=13)

    report = generate_sleep_report_with_llm_fallback(
        analysis,
        llm_response_json=_valid_llm_payload(),
    )

    assert report.summary.record_id == analysis.metadata.record_id
    assert report.elder_report == _valid_llm_payload()["elder_report"]
    assert "及时就医" in report.care_suggestions[-1]
    assert report.generated_at == analysis.generated_at


def test_generate_sleep_report_with_llm_fallback_uses_template_for_invalid_json() -> None:
    analysis = generate_mock_sleep_analysis(duration_hours=0.5, seed=14)

    report = generate_sleep_report_with_llm_fallback(
        analysis,
        llm_response_json="{not-json",
    )

    assert "模拟睡眠报告" in report.elder_report
    assert "Retrieved local context chunks:" in report.professional_report


def test_generate_sleep_report_with_llm_fallback_uses_template_for_unsafe_draft() -> None:
    analysis = generate_mock_sleep_analysis(duration_hours=0.5, seed=14)
    payload = _valid_llm_payload()
    payload["care_suggestions"] = ["不用找医生，可以自行治疗。"]

    report = generate_sleep_report_with_llm_fallback(
        analysis,
        llm_response_json=payload,
    )

    assert "模拟睡眠报告" in report.elder_report
    assert "Retrieved local context chunks:" in report.professional_report


def test_build_deepseek_report_request_preview_is_api_key_free() -> None:
    analysis = generate_mock_sleep_analysis(duration_hours=0.5, seed=15)

    preview = build_deepseek_report_request_preview(analysis)

    assert preview["model"] == DEFAULT_DEEPSEEK_MODEL
    assert preview["base_url"] == "https://api.deepseek.com"
    assert preview["api_key_env"] == "DEEPSEEK_API_KEY"
    assert preview["thinking_type"] == "disabled"
    assert preview["messages"][0]["role"] == "system"
    assert "Return JSON only" in preview["messages"][0]["content"]
    assert "required_json_schema" in preview["messages"][1]["content"]


def test_deepseek_chat_client_requires_api_key() -> None:
    client = DeepSeekChatClient(api_key=None, api_key_env="MISSING_DEEPSEEK_KEY")

    with pytest.raises(DeepSeekMissingAPIKeyError):
        client.create_chat_completion(
            model=DEFAULT_DEEPSEEK_MODEL,
            messages=[],
            temperature=0.2,
        )


def test_deepseek_chat_client_posts_openai_compatible_payload() -> None:
    response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(_valid_llm_payload(), ensure_ascii=False)
                }
            }
        ]
    }
    http_client = FakeHTTPClient(response_payload)
    client = DeepSeekChatClient(
        api_key="test-key",
        base_url="https://api.deepseek.com/",
        http_client=http_client,
    )

    draft = client.create_report_draft(
        messages=[{"role": "user", "content": "return json"}],
        config=DeepSeekReportGeneratorConfig(),
    )

    assert draft.elder_report == _valid_llm_payload()["elder_report"]
    call = http_client.post_calls[0]
    assert call["url"] == "https://api.deepseek.com/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["json"]["model"] == DEFAULT_DEEPSEEK_MODEL
    assert call["json"]["response_format"] == {"type": "json_object"}
    assert call["json"]["thinking"] == {"type": "disabled"}
    assert call["json"]["stream"] is False


def test_deepseek_chat_client_rejects_invalid_report_content() -> None:
    response_payload = {"choices": [{"message": {"content": "{\"bad\": true}"}}]}
    client = DeepSeekChatClient(
        api_key="test-key",
        http_client=FakeHTTPClient(response_payload),
    )

    with pytest.raises(DeepSeekAPIError):
        client.create_report_draft(
            messages=[{"role": "user", "content": "return json"}],
            config=DeepSeekReportGeneratorConfig(),
        )


def test_generate_sleep_report_with_deepseek_fallback_uses_client_draft() -> None:
    analysis = generate_mock_sleep_analysis(duration_hours=0.5, seed=16)
    client = FakeDeepSeekDraftClient()

    report = generate_sleep_report_with_deepseek_fallback(analysis, client=client)

    assert client.calls
    assert report.elder_report == _valid_llm_payload()["elder_report"]
    assert report.generated_at == analysis.generated_at


def test_generate_sleep_report_with_deepseek_fallback_uses_template_on_api_error() -> None:
    analysis = generate_mock_sleep_analysis(duration_hours=0.5, seed=17)
    client = FakeDeepSeekDraftClient(should_fail=True)

    report = generate_sleep_report_with_deepseek_fallback(analysis, client=client)

    assert client.calls
    assert "模拟睡眠报告" in report.elder_report
    assert "Retrieved local context chunks:" in report.professional_report


def test_run_deepseek_report_smoke_help_is_import_safe() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/run_deepseek_report_smoke.py", "--help"],
        cwd=".",
        check=True,
        capture_output=True,
        text=True,
    )

    assert "guarded live DeepSeek smoke" in completed.stdout
