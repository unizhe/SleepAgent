from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from sleepagent.integrations.llm import CloudLLMConfig
from sleepagent.observability import STATE
from sleepagent.product_device.llm import (
    OpenAICompatibleChatProvider,
    OpenAICompatibleProviderConfig,
    ProductLLMProviderError,
)
from sleepagent.product_runtime.contracts import StrictContract
from sleepagent.product_runtime.provider import (
    OpenAICompatibleStructuredAgentModel,
)
from tests.support.openai_compatible_server import (
    LoopbackOpenAICompatibleServer,
    OpenAIRequestRecord,
    ScriptedOpenAIResponse,
)


pytestmark = pytest.mark.unit

API_KEY = "sk-loopback-secret-that-must-not-leak"
MODEL_ID = "loopback-product-model"


class ExampleOutput(StrictContract):
    answer: str


@pytest.fixture(autouse=True)
def _reset_observability_state() -> Iterator[None]:
    STATE.reset_for_tests()
    yield
    STATE.reset_for_tests()


def _completion_response(
    content: str,
    *,
    request_id: str = "loopback-request-1",
    status_code: int = 200,
    delay_seconds: float = 0,
) -> ScriptedOpenAIResponse:
    return ScriptedOpenAIResponse(
        status_code=status_code,
        body={
            "id": request_id,
            "choices": [
                {"message": {"role": "assistant", "content": content}}
            ],
        },
        delay_seconds=delay_seconds,
    )


def _structured_model(
    server: LoopbackOpenAICompatibleServer,
    *,
    timeout_seconds: float = 1,
    retry: int = 1,
) -> OpenAICompatibleStructuredAgentModel:
    config = OpenAICompatibleProviderConfig(
        model=MODEL_ID,
        base_url=server.base_url,
        temperature=0,
        timeout_seconds=timeout_seconds,
        retry=retry,
        max_output_tokens=64,
        thinking_type="disabled",
    )
    return OpenAICompatibleStructuredAgentModel(
        config=config,
        provider=OpenAICompatibleChatProvider(
            api_key=API_KEY,
            base_url=server.base_url,
            timeout_seconds=timeout_seconds,
        ),
    )


def _generate(model: OpenAICompatibleStructuredAgentModel) -> ExampleOutput:
    return model.generate(
        messages=[{"role": "user", "content": "Return the safe answer."}],
        schema=ExampleOutput,
        prompt_version="loopback-product.v1",
        context_packet_id="context:loopback:1",
    )


def test_cloud_provider_config_does_not_publicly_serialize_api_key() -> None:
    config = CloudLLMConfig(
        base_url="http://127.0.0.1:1/v1",
        api_key=API_KEY,
        model_id=MODEL_ID,
    )

    assert API_KEY not in repr(config)
    assert API_KEY not in config.model_dump_json()
    assert "api_key" not in config.model_dump()


def test_structured_product_model_uses_real_openai_compatible_http_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="sleepagent.observability")
    with LoopbackOpenAICompatibleServer(
        _completion_response(json.dumps({"answer": "ok"}))
    ) as server:
        model = _structured_model(server)

        assert _generate(model) == ExampleOutput(answer="ok")

        assert server.request_count == 1
        assert server.errors == ()
        request = server.requests[0]
        assert request.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        assert request.json_body["model"] == MODEL_ID
        assert request.json_body["temperature"] == 0
        assert request.json_body["max_tokens"] == 64
        assert request.json_body["response_format"] == {"type": "json_object"}
        assert request.json_body["thinking"] == {"type": "disabled"}
        assert "ExampleOutput" in request.json_body["messages"][0]["content"]
        assert request.json_body["messages"][-1] == {
            "role": "user",
            "content": "Return the safe answer.",
        }
        assert API_KEY not in json.dumps(request.json_body, sort_keys=True)
        assert model.last_provider_request_id == "loopback-request-1"

    _assert_secret_not_public(caplog=caplog)


def test_retryable_provider_failure_recovers_on_second_real_http_attempt() -> None:
    def responder(
        request: OpenAIRequestRecord,
        index: int,
    ) -> ScriptedOpenAIResponse:
        if index == 0:
            return ScriptedOpenAIResponse(
                status_code=503,
                body={"error": {"message": "temporarily unavailable"}},
            )
        return _completion_response(
            json.dumps({"answer": "recovered"}),
            request_id="loopback-request-retry-2",
        )

    with LoopbackOpenAICompatibleServer(responder) as server:
        model = _structured_model(server)

        assert _generate(model) == ExampleOutput(answer="recovered")
        assert server.request_count == 2
        assert model.last_provider_request_id == "loopback-request-retry-2"
        assert server.errors == ()


@pytest.mark.parametrize(
    ("failure", "expected_message", "expected_requests"),
    [
        ("timeout", "Product LLM request failed", 2),
        ("http_5xx", "Product LLM request failed", 2),
        ("invalid_envelope_json", "Product LLM request failed", 2),
        ("invalid_content_json", "returned invalid JSON", 1),
        ("schema_invalid", "failed ExampleOutput validation", 1),
    ],
)
def test_structured_product_model_failure_and_retry_semantics_over_real_http(
    failure: str,
    expected_message: str,
    expected_requests: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="sleepagent.observability")
    if failure == "timeout":
        scripted = _completion_response(
            json.dumps({"answer": "too late"}),
            delay_seconds=0.15,
        )
        timeout_seconds = 0.02
    elif failure == "http_5xx":
        scripted = ScriptedOpenAIResponse(
            status_code=503,
            body={"error": {"message": "temporarily unavailable"}},
        )
        timeout_seconds = 1
    elif failure == "invalid_envelope_json":
        scripted = ScriptedOpenAIResponse(body="not-json")
        timeout_seconds = 1
    elif failure == "invalid_content_json":
        scripted = _completion_response("not-json")
        timeout_seconds = 1
    else:
        scripted = _completion_response(
            json.dumps({"answer": "ok", "unexpected": True})
        )
        timeout_seconds = 1

    with LoopbackOpenAICompatibleServer(scripted) as server:
        model = _structured_model(
            server,
            timeout_seconds=timeout_seconds,
            retry=1,
        )

        with pytest.raises(ProductLLMProviderError, match=expected_message) as exc:
            _generate(model)

        assert server.request_count == expected_requests
        assert server.errors == ()
        for request in server.requests:
            assert API_KEY not in json.dumps(request.json_body, sort_keys=True)

    _assert_secret_not_public(caplog=caplog, error=exc.value)


def _assert_secret_not_public(
    *,
    caplog: pytest.LogCaptureFixture,
    error: BaseException | None = None,
) -> None:
    public_surfaces = [
        caplog.text,
        json.dumps(STATE.snapshot(), sort_keys=True),
    ]
    current = error
    while current is not None:
        public_surfaces.extend((str(current), repr(current)))
        current = current.__cause__
    assert all(API_KEY not in surface for surface in public_surfaces)
