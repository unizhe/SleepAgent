from __future__ import annotations

import json

import pytest
from pydantic import model_validator

from sleepagent.runtime.provider import (
    OpenAICompatibleProviderConfig,
    OpenAICompatibleStructuredAgentModel,
    ProductLLMProviderError,
    openai_compatible_provider_config_from_env,
)
from sleepagent.runtime.contracts import StrictContract


class ExampleOutput(StrictContract):
    answer: str


class SemanticOutput(StrictContract):
    answer: str

    @model_validator(mode="after")
    def require_corrected_answer(self) -> "SemanticOutput":
        if self.answer != "corrected":
            raise ValueError("answer requires semantic correction")
        return self


class RawProvider:
    is_configured = True

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.payload


class SequencedRawProvider(RawProvider):
    def __init__(self, payloads: list[dict]) -> None:
        super().__init__(payloads[-1])
        self.payloads = payloads

    def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.payloads.pop(0)


def response(
    content: object,
    *,
    request_id: str = "provider-request-1",
    prompt_tokens: int | None = None,
) -> dict:
    value = {
        "id": request_id,
        "choices": [{"message": {"content": json.dumps(content)}}],
    }
    if prompt_tokens is not None:
        value["usage"] = {"prompt_tokens": prompt_tokens}
    return value


def test_structured_provider_binds_schema_context_and_request_id() -> None:
    raw = RawProvider(response({"answer": "ok"}, prompt_tokens=37))
    model = OpenAICompatibleStructuredAgentModel(
        config=OpenAICompatibleProviderConfig(
            model="provider-model",
            temperature=0,
        ),
        provider=raw,
    )
    result = model.generate(
        messages=[{"role": "user", "content": "answer safely"}],
        schema=ExampleOutput,
        prompt_version="prompt.v1",
        context_packet_id="context:1",
    )

    assert result == ExampleOutput(answer="ok")
    assert model.provider == "openai-compatible"
    assert model.model_id == "provider-model"
    assert model.last_provider_request_id == "provider-request-1"
    assert model.last_provider_input_tokens == 37
    sent = raw.calls[0]["messages"]
    assert sent[-1] == {"role": "user", "content": "answer safely"}
    assert "ExampleOutput" in sent[0]["content"]
    assert "context:1" in sent[0]["content"]


@pytest.mark.parametrize(
    "payload,error",
    [
        (
            {
                "id": "provider-request-invalid-schema",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"answer": "ok", "unexpected": True}
                            )
                        }
                    }
                ],
            },
            "failed ExampleOutput validation",
        ),
        (
            {
                "id": "provider-request-invalid-json",
                "choices": [{"message": {"content": "not-json"}}],
            },
            "invalid JSON",
        ),
        (
            {"id": "provider-request-empty", "choices": []},
            "did not include message content",
        ),
    ],
)
def test_structured_provider_fails_closed_on_invalid_output(
    payload: dict,
    error: str,
) -> None:
    model = OpenAICompatibleStructuredAgentModel(
        provider=RawProvider(payload)
    )
    with pytest.raises(ProductLLMProviderError, match=error):
        model.generate(
            messages=[],
            schema=ExampleOutput,
            prompt_version="prompt.v1",
            context_packet_id="context:1",
        )


def test_structured_provider_does_not_invent_missing_request_id() -> None:
    model = OpenAICompatibleStructuredAgentModel(
        provider=RawProvider(response({"answer": "ok"}, request_id=""))
    )
    assert model.generate(
        messages=[],
        schema=ExampleOutput,
        prompt_version="prompt.v1",
        context_packet_id="context:1",
    ).answer == "ok"
    assert model.last_provider_request_id is None


def test_structured_provider_allows_one_live_contract_correction() -> None:
    raw = SequencedRawProvider(
        [
            response({"unexpected": "field"}, request_id="failed-request"),
            response(
                {"answer": "corrected"},
                request_id="corrected-request",
                prompt_tokens=51,
            ),
        ]
    )
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    result = model.generate(
        messages=[{"role": "user", "content": "answer safely"}],
        schema=ExampleOutput,
        prompt_version="prompt.v1",
        context_packet_id="context:1",
    )

    assert result.answer == "corrected"
    assert len(raw.calls) == 2
    assert "previous live response failed" in raw.calls[1]["messages"][0]["content"]
    assert model.last_provider_request_id == "corrected-request"
    assert model.last_provider_input_tokens == 51


def test_live_contract_correction_serializes_model_validator_context() -> None:
    raw = SequencedRawProvider(
        [response({"answer": "wrong"}), response({"answer": "corrected"})]
    )
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    result = model.generate(
        messages=[],
        schema=SemanticOutput,
        prompt_version="prompt.v1",
        context_packet_id="context:semantic",
    )

    assert result.answer == "corrected"
    assert "semantic correction" in raw.calls[1]["messages"][0]["content"]
    assert "remove every reported number" in raw.calls[1]["messages"][0]["content"]
    assert "Never invent a source ref" in raw.calls[1]["messages"][0]["content"]
    assert "remove the entire date/time phrase" in raw.calls[1]["messages"][0]["content"]
    assert "number-free word '近期'" in raw.calls[1]["messages"][0]["content"]
    assert "Do not respell the digits" in raw.calls[1]["messages"][0]["content"]
    assert "copied verbatim" in raw.calls[1]["messages"][0]["content"]
    assert "exact contiguous substring" in raw.calls[1]["messages"][0]["content"]
    assert "remove the binding" in raw.calls[1]["messages"][0]["content"]


def test_provider_config_reuses_legacy_local_deepseek_names_without_router() -> None:
    config = openai_compatible_provider_config_from_env(
        {
            "DEEPSEEK_API_KEY": " ",
            "SLEEPAGENT_PRODUCT_LLM_MODEL": "",
            "SLEEPAGENT_PRODUCT_LLM_BASE_URL": " ",
            "SLEEPAGENT_PRODUCT_LLM_TIMEOUT_SECONDS": "",
            "SLEEPAGENT_RADAR_AGENT_LLM_API_KEY": "configured-secret",
            "SLEEPAGENT_RADAR_AGENT_LLM_MODEL_ID": "legacy-configured-model",
            "SLEEPAGENT_RADAR_AGENT_LLM_BASE_URL": "https://api.deepseek.com",
            "SLEEPAGENT_RADAR_AGENT_LLM_TIMEOUT_SECONDS": "17",
        }
    )

    assert config.api_key_env == "SLEEPAGENT_RADAR_AGENT_LLM_API_KEY"
    assert config.model == "legacy-configured-model"
    assert config.base_url == "https://api.deepseek.com"
    assert config.timeout_seconds == 17


def test_provider_config_default_budget_fits_strict_agent_contracts() -> None:
    config = openai_compatible_provider_config_from_env({})

    assert config.max_output_tokens == 4000
