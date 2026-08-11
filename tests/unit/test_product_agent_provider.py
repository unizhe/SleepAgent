from __future__ import annotations

import json

import pytest

from sleepagent.product_device.llm import (
    OpenAICompatibleProviderConfig,
    ProductLLMProviderError,
)
from sleepagent.product_runtime.contracts import StrictContract
from sleepagent.product_runtime.provider import (
    OpenAICompatibleStructuredAgentModel,
)


class ExampleOutput(StrictContract):
    answer: str


class RawProvider:
    is_configured = True

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.payload


def response(content: object, *, request_id: str = "provider-request-1") -> dict:
    return {
        "id": request_id,
        "choices": [{"message": {"content": json.dumps(content)}}],
    }


def test_structured_provider_binds_schema_context_and_request_id() -> None:
    raw = RawProvider(response({"answer": "ok"}))
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
