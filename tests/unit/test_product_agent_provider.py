from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError, model_validator

from sleepagent.runtime.invocation import SleepCareModelOutput
from sleepagent.runtime.provider import (
    OpenAICompatibleChatProvider,
    OpenAICompatibleProviderConfig,
    OpenAICompatibleStructuredAgentModel,
    ProductLLMProviderError,
    _apply_numeric_text_repair_patch,
    _NumericTextRepairCandidate,
    _NumericTextRepairPatch,
    _numeric_repair_candidates,
    openai_compatible_provider_config_from_env,
    provider_transport_audit_scope,
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


class _AuditObserver:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.events: list[tuple[str, dict]] = []

    def before_request(self, **kwargs) -> None:
        self.events.append(("before", kwargs))
        if self.reject:
            raise RuntimeError("audit rejected request")

    def after_response(self, **kwargs) -> None:
        self.events.append(("response", kwargs))

    def after_failure(self, **kwargs) -> None:
        self.events.append(("failure", kwargs))


class _HTTPResponse:
    status_code = 200

    def json(self) -> dict:
        return {
            "id": "request-safe",
            "usage": {"prompt_tokens": 19, "completion_tokens": 7},
            "choices": [{"message": {"content": "{}"}}],
        }


class _HTTPClient:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, *args, **kwargs) -> _HTTPResponse:
        self.calls += 1
        return _HTTPResponse()


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


def test_transport_audit_observes_exact_request_and_safe_usage_metadata() -> None:
    client = _HTTPClient()
    observer = _AuditObserver()
    provider = OpenAICompatibleChatProvider(
        api_key="configured-test-key",
        http_client=client,
    )
    messages = [{"role": "user", "content": "governed context"}]

    with provider_transport_audit_scope(observer):
        payload = provider.create_chat_completion(
            model="safe-model",
            messages=messages,
            temperature=0,
            retry=0,
        )

    assert payload["id"] == "request-safe"
    assert client.calls == 1
    assert observer.events[0] == (
        "before",
        {"model": "safe-model", "messages": messages, "attempt": 1},
    )
    assert observer.events[1][0] == "response"
    assert observer.events[1][1]["request_id_present"] is True
    assert observer.events[1][1]["input_tokens"] == 19
    assert observer.events[1][1]["output_tokens"] == 7
    assert observer.events[1][1]["latency_ms"] >= 0


def test_transport_audit_rejection_stops_before_http() -> None:
    client = _HTTPClient()
    provider = OpenAICompatibleChatProvider(
        api_key="configured-test-key",
        http_client=client,
    )
    with pytest.raises(RuntimeError, match="audit rejected request"):
        with provider_transport_audit_scope(_AuditObserver(reject=True)):
            provider.create_chat_completion(
                model="safe-model",
                messages=[{"role": "user", "content": "blocked"}],
                temperature=0,
                retry=1,
            )
    assert client.calls == 0


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
    correction = raw.calls[1]["messages"][0]["content"]
    assert "semantic correction" in correction
    assert "Regenerate the complete JSON object" in correction
    assert "semantic-binding repair patch" not in correction
    assert raw.calls[1]["messages"][-2]["role"] == "assistant"
    assert raw.calls[1]["messages"][-1]["role"] == "user"
    assert "Correct the preceding invalid JSON object in place" in (
        raw.calls[1]["messages"][-1]["content"]
    )


def _sleepcare_output(*, dated: bool) -> dict:
    text = (
        "2027年04月19日夜间记录显示睡眠约 7 小时。"
        if dated
        else "当晚记录显示睡眠约 7 小时。"
    )
    return {
        "status": "completed",
        "summary": "已生成受约束的沟通内容。",
        "output_payload": {
            "draft_id": "draft:numeric-correction",
            "audience_role": "elder",
            "text": text,
            "claim_refs": ["claim:sleep-duration"],
            "care_candidate_refs": [],
            "semantic_bindings": [
                {
                    "binding_id": "binding:sleep-duration",
                    "source_kind": "evidence_claim",
                    "source_ref": "claim:sleep-duration",
                    "rendered_text": "睡眠约 7 小时。",
                    "preserved_numbers": ["7"],
                }
            ],
            "memory_change_candidates": [],
            "context_notice": "这是受约束的夜间记录说明。",
        },
    }


def _numeric_patch_for(
    payload: dict,
    *,
    replacement: str = "近期",
) -> tuple[dict, tuple]:
    try:
        SleepCareModelOutput.model_validate(payload)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False)
    else:
        raise AssertionError("payload must fail numeric validation")
    candidates, _ = _numeric_repair_candidates(payload=payload, errors=errors)
    assert candidates
    return (
        {
            "repairs": [
                {
                    "candidate_id": candidate.candidate_id,
                    "replacement": replacement,
                }
                for candidate in candidates
            ]
        },
        candidates,
    )


def _numeric_payload_with_text(text: str) -> dict:
    payload = _sleepcare_output(dated=False)
    payload["output_payload"]["text"] = text
    return payload


def test_live_correction_repairs_unbound_date_numbers_without_relaxing_binding() -> None:
    invalid = _sleepcare_output(dated=True)
    numeric_patch, candidates = _numeric_patch_for(invalid)
    raw = SequencedRawProvider(
        [
            response(invalid, request_id="invalid-date-request"),
            response(numeric_patch, request_id="numeric-patch-request"),
        ]
    )
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    result = model.generate(
        messages=[{"role": "user", "content": "Use accepted Evidence only."}],
        schema=SleepCareModelOutput,
        prompt_version="sleepcare.numeric.v1",
        context_packet_id="context:numeric-correction",
    )

    assert result.output_payload.text == "近期夜间记录显示睡眠约 7 小时。"
    assert result.output_payload.semantic_bindings[0].preserved_numbers == ["7"]
    assert model.last_provider_request_id == "numeric-patch-request"
    assert len(raw.calls) == 2
    correction_messages = raw.calls[1]["messages"]
    correction_instruction = correction_messages[0]["content"]
    assert "numeric text repair patch" in correction_instruction
    assert "not a regenerated Agent output" in correction_instruction
    assert candidates[0].original_text == "2027年04月19日"
    assert f'"candidate_id":"{candidates[0].candidate_id}"' in (
        correction_instruction
    )
    assert '"covered_offending_tokens":["04","19","2027"]' in (
        correction_instruction
    )
    assert "offsets, source_ref, binding" in correction_instruction
    assert "Return one JSON object only" not in correction_instruction
    assert "semantic-binding repair patch" not in correction_instruction


def test_numeric_candidate_replaces_complete_iso_calendar_date() -> None:
    invalid = _numeric_payload_with_text("记录日期为 2026-03-23；睡眠约 7 小时。")
    numeric_patch, candidates = _numeric_patch_for(invalid, replacement="夜间")
    assert [candidate.original_text for candidate in candidates] == ["2026-03-23"]
    assert all(
        candidate.original_text not in {"2026", "03", "23"}
        for candidate in candidates
    )
    _, candidates_again = _numeric_patch_for(invalid, replacement="夜间")
    assert [candidate.candidate_id for candidate in candidates_again] == [
        candidate.candidate_id for candidate in candidates
    ]
    raw = SequencedRawProvider(
        [response(invalid), response(numeric_patch, request_id="numeric-patch")]
    )
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    result = model.generate(
        messages=[{"role": "user", "content": "Use accepted Evidence only."}],
        schema=SleepCareModelOutput,
        prompt_version="sleepcare.numeric-candidates.v1",
        context_packet_id="context:numeric-candidates",
    )

    assert result.output_payload.text == "记录日期为 夜间；睡眠约 7 小时。"
    expected = copy.deepcopy(invalid)
    expected["output_payload"]["text"] = result.output_payload.text
    assert result == SleepCareModelOutput.model_validate(expected)
    assert len(raw.calls) == 2


@pytest.mark.parametrize(
    ("repair_patch", "expected_error"),
    [
        (
            {
                "repairs": [
                    {
                        "candidate_id": "numeric-candidate:not-generated",
                        "replacement": "近期",
                    }
                ]
            },
            "selected an unknown candidate",
        ),
        (
            {
                "repairs": [
                    {
                        "candidate_id": "replace-with-generated-id",
                        "replacement": "明天",
                    }
                ]
            },
            "returned an invalid patch",
        ),
        (
            {
                "repairs": [
                    {
                        "candidate_id": "replace-with-generated-id",
                        "replacement": "近期",
                    }
                ],
                "communication_text": "不允许由模型返回正文。",
            },
            "returned an invalid patch",
        ),
    ],
)
def test_numeric_patch_rejects_unknown_candidate_or_replacement_escalation(
    repair_patch: dict,
    expected_error: str,
) -> None:
    invalid = _numeric_payload_with_text("记录日期为 2026-03-23；睡眠约 7 小时。")
    _, candidates = _numeric_patch_for(invalid)
    if repair_patch["repairs"][0]["candidate_id"] == "replace-with-generated-id":
        repair_patch["repairs"][0]["candidate_id"] = candidates[0].candidate_id
    raw = SequencedRawProvider([response(invalid), response(repair_patch)])
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    with pytest.raises(ProductLLMProviderError, match=expected_error):
        model.generate(
            messages=[],
            schema=SleepCareModelOutput,
            prompt_version="sleepcare.numeric-invalid-patch.v1",
            context_packet_id="context:numeric-invalid-patch",
        )

    assert len(raw.calls) == 2


def test_numeric_patch_rejects_duplicate_candidate() -> None:
    invalid = _numeric_payload_with_text("记录日期为 2026-03-23；睡眠约 7 小时。")
    _, candidates = _numeric_patch_for(invalid)
    choice = {
        "candidate_id": candidates[0].candidate_id,
        "replacement": "近期",
    }
    raw = SequencedRawProvider(
        [response(invalid), response({"repairs": [choice, choice]})]
    )
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    with pytest.raises(ProductLLMProviderError, match="duplicated a candidate"):
        model.generate(
            messages=[],
            schema=SleepCareModelOutput,
            prompt_version="sleepcare.numeric-duplicate.v1",
            context_packet_id="context:numeric-duplicate",
        )

    assert len(raw.calls) == 2


@pytest.mark.parametrize(
    "text",
    [
        "入睡时间 23:30；睡眠约 7 小时。",
        "入睡时间 23时30分；睡眠约 7 小时。",
        "02:00 离床；睡眠约 7 小时。",
        "记录时间为 晚上 23:30；睡眠约 7 小时。",
        "记录日期为 2026-03-23 23:30；睡眠约 7 小时。",
        "记录日期为 2026-03-23T23:30；睡眠约 7 小时。",
    ],
)
def test_numeric_repair_fails_closed_for_clock_time(text: str) -> None:
    invalid = _numeric_payload_with_text(text)
    raw = SequencedRawProvider([response(invalid)])
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    with pytest.raises(
        ProductLLMProviderError,
        match="no safe incidental calendar-date candidate",
    ):
        model.generate(
            messages=[],
            schema=SleepCareModelOutput,
            prompt_version="sleepcare.numeric-clock-time.v1",
            context_packet_id="context:numeric-clock-time",
        )

    assert len(raw.calls) == 1


def test_numeric_patch_rejects_stale_candidate_original_text() -> None:
    invalid = _numeric_payload_with_text("记录日期为 2026-03-23；睡眠约 7 小时。")
    try:
        SleepCareModelOutput.model_validate(invalid)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False)
    else:
        raise AssertionError("payload must fail numeric validation")
    candidates, required_spans = _numeric_repair_candidates(
        payload=invalid,
        errors=errors,
    )
    patch = _NumericTextRepairPatch.model_validate(
        {
            "repairs": [
                {
                    "candidate_id": candidates[0].candidate_id,
                    "replacement": "近期",
                }
            ]
        }
    )
    stale = copy.deepcopy(invalid)
    stale["output_payload"]["text"] = stale["output_payload"]["text"].replace(
        "2026",
        "2025",
    )

    with pytest.raises(ProductLLMProviderError, match="original text changed"):
        _apply_numeric_text_repair_patch(
            payload=stale,
            candidates=candidates,
            required_spans=required_spans,
            patch=patch,
        )


def test_numeric_patch_rejects_overlapping_internal_candidates() -> None:
    payload = _numeric_payload_with_text("2026-03-23 睡眠约 7 小时。")
    candidates = (
        _NumericTextRepairCandidate(
            candidate_id="candidate:date",
            start=0,
            end=10,
            original_text="2026-03-23",
            offending_spans=((0, 4, "2026"), (5, 7, "03"), (8, 10, "23")),
        ),
        _NumericTextRepairCandidate(
            candidate_id="candidate:overlap",
            start=5,
            end=10,
            original_text="03-23",
            offending_spans=((5, 7, "03"), (8, 10, "23")),
        ),
    )
    patch = _NumericTextRepairPatch.model_validate(
        {
            "repairs": [
                {"candidate_id": "candidate:date", "replacement": "近期"},
                {"candidate_id": "candidate:overlap", "replacement": "当晚"},
            ]
        }
    )

    with pytest.raises(ProductLLMProviderError, match="overlapping candidates"):
        _apply_numeric_text_repair_patch(
            payload=payload,
            candidates=candidates,
            required_spans=(
                (0, 4, "2026"),
                (5, 7, "03"),
                (8, 10, "23"),
            ),
            patch=patch,
        )


def test_numeric_repair_fails_closed_when_candidate_overlaps_binding() -> None:
    invalid = _numeric_payload_with_text("2026-03-23记录显示睡眠约 7 小时。")
    invalid["output_payload"]["claim_refs"].append("claim:date-separator")
    invalid["output_payload"]["semantic_bindings"].append(
        {
            "binding_id": "binding:date-separator",
            "source_kind": "evidence_claim",
            "source_ref": "claim:date-separator",
            "rendered_text": "-",
            "preserved_numbers": [],
        }
    )
    raw = SequencedRawProvider([response(invalid)])
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    with pytest.raises(ProductLLMProviderError, match="no safe incidental"):
        model.generate(
            messages=[],
            schema=SleepCareModelOutput,
            prompt_version="sleepcare.numeric-binding-overlap.v1",
            context_packet_id="context:numeric-binding-overlap",
        )

    assert len(raw.calls) == 1


def test_numeric_repair_fails_closed_without_incidental_date_time_candidate() -> None:
    invalid = _numeric_payload_with_text("记录编号 2026；睡眠约 7 小时。")
    raw = SequencedRawProvider([response(invalid)])
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    with pytest.raises(ProductLLMProviderError, match="no safe incidental"):
        model.generate(
            messages=[],
            schema=SleepCareModelOutput,
            prompt_version="sleepcare.numeric-no-candidate.v1",
            context_packet_id="context:numeric-no-candidate",
        )

    assert len(raw.calls) == 1


def test_sleepcare_validator_still_rejects_truly_unbound_numeric_text() -> None:
    with pytest.raises(
        ValidationError,
        match="every number in Communication text must be covered",
    ):
        SleepCareModelOutput.model_validate(_sleepcare_output(dated=True))


def _substring_output(
    *,
    role: str,
) -> dict:
    if role == "elder":
        text = "当晚记录显示睡眠情况总体平稳。"
        rendered_text = "夜间记录显示睡眠情况总体平稳。"
    else:
        text = "夜间记录已整理，整体情况平稳。"
        rendered_text = "夜间记录已经整理，整体情况稳定。"
    return {
        "status": "completed",
        "summary": "已生成受约束的沟通内容。",
        "output_payload": {
            "draft_id": f"draft:substring:{role}",
            "audience_role": role,
            "text": text,
            "claim_refs": [f"claim:{role}:summary"],
            "care_candidate_refs": [],
            "semantic_bindings": [
                {
                    "binding_id": f"binding:{role}:summary",
                    "source_kind": "evidence_claim",
                    "source_ref": f"claim:{role}:summary",
                    "rendered_text": rendered_text,
                    "preserved_numbers": [],
                }
            ],
            "memory_change_candidates": [],
            "context_notice": "这是受约束的夜间记录说明。",
        },
    }


@pytest.mark.parametrize(
    ("role", "expected_text"),
    [
        ("elder", "当晚记录显示睡眠情况总体平稳。"),
        ("family", "夜间记录已整理，整体情况平稳。"),
    ],
)
def test_exact_substring_error_uses_binding_only_repair_with_frozen_text(
    role: str,
    expected_text: str,
) -> None:
    invalid = _substring_output(role=role)
    with pytest.raises(
        ValidationError,
        match="rendered_text must be an exact substring",
    ):
        SleepCareModelOutput.model_validate(invalid)

    raw = SequencedRawProvider(
        [
            response(invalid, request_id=f"invalid-{role}-substring"),
            response(
                {
                    "repairs": [
                        {
                            "binding_index": 0,
                            "binding_id": f"binding:{role}:summary",
                            "replacement_rendered_text": expected_text,
                        }
                    ]
                },
                request_id=f"corrected-{role}-substring",
            ),
        ]
    )
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    result = model.generate(
        messages=[{"role": "user", "content": "Use accepted Evidence only."}],
        schema=SleepCareModelOutput,
        prompt_version="sleepcare.substring.v1",
        context_packet_id=f"context:substring:{role}",
    )

    final_text = result.output_payload.text
    final_rendered = result.output_payload.semantic_bindings[0].rendered_text
    assert final_text == expected_text
    assert final_text == invalid["output_payload"]["text"]
    assert final_rendered in final_text
    assert SleepCareModelOutput.model_validate(result.model_dump()) == result
    assert result.output_payload.semantic_bindings[0].source_ref == (
        invalid["output_payload"]["semantic_bindings"][0]["source_ref"]
    )
    assert result.output_payload.semantic_bindings[0].source_kind == (
        invalid["output_payload"]["semantic_bindings"][0]["source_kind"]
    )
    assert raw.calls[1]["messages"][-2] == {
        "role": "assistant",
        "content": json.dumps(invalid),
    }
    correction_instruction = raw.calls[1]["messages"][0]["content"]
    assert "semantic-binding repair patch" in correction_instruction
    assert "not a regenerated Agent output" in correction_instruction
    assert "Communication.text is immutable" in correction_instruction
    assert '"replacement_rendered_text"' in correction_instruction
    assert "Regenerate the complete JSON object" not in correction_instruction
    assert f'"audience_role":"{role}"' in correction_instruction
    assert f'"binding_id":"binding:{role}:summary"' in correction_instruction
    assert '"communication_text"' in correction_instruction


def _exact_then_numeric_payloads() -> tuple[dict, dict]:
    initial = _sleepcare_output(dated=True)
    initial["output_payload"]["semantic_bindings"][0]["rendered_text"] = (
        "当晚记录显示睡眠约 7 小时。"
    )
    patched = json.loads(json.dumps(initial))
    patched["output_payload"]["semantic_bindings"][0]["rendered_text"] = (
        "睡眠约 7 小时。"
    )
    return initial, patched


def test_exact_substring_patch_can_chain_once_to_numeric_candidate_patch() -> None:
    initial, patched = _exact_then_numeric_payloads()
    with pytest.raises(
        ValidationError,
        match="rendered_text must be an exact substring",
    ):
        SleepCareModelOutput.model_validate(initial)
    with pytest.raises(
        ValidationError,
        match="every number in Communication text must be covered",
    ):
        SleepCareModelOutput.model_validate(patched)
    numeric_patch, candidates = _numeric_patch_for(patched)

    raw = SequencedRawProvider(
        [
            response(initial, request_id="exact-invalid-request"),
            response(
                {
                    "repairs": [
                        {
                            "binding_index": 0,
                            "binding_id": "binding:sleep-duration",
                            "replacement_rendered_text": "睡眠约 7 小时。",
                        }
                    ]
                },
                request_id="binding-patch-request",
            ),
            response(numeric_patch, request_id="numeric-patch-request"),
        ]
    )
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    result = model.generate(
        messages=[{"role": "user", "content": "Use accepted Evidence only."}],
        schema=SleepCareModelOutput,
        prompt_version="sleepcare.exact-then-numeric.v1",
        context_packet_id="context:exact-then-numeric",
    )

    assert result.output_payload.text == "近期夜间记录显示睡眠约 7 小时。"
    assert model.last_provider_request_id == "numeric-patch-request"
    assert len(raw.calls) == 3
    numeric_messages = raw.calls[2]["messages"]
    assert candidates[0].candidate_id in numeric_messages[0]["content"]
    assert "numeric text repair patch" in numeric_messages[0]["content"]
    assert "complete Agent output" in numeric_messages[0]["content"]
    assert "semantic-binding repair patch" not in numeric_messages[0]["content"]


def test_exact_then_numeric_pipeline_stops_after_failed_numeric_correction() -> None:
    class NumericPostPatchFailureOutput(SleepCareModelOutput):
        @model_validator(mode="after")
        def reject_numeric_patch_for_test(self) -> "NumericPostPatchFailureOutput":
            raise ValueError(
                "every number in Communication text must be covered by a semantic "
                "binding; remove these unbound tokens: ['99']"
            )

    initial, patched = _exact_then_numeric_payloads()
    numeric_patch, _ = _numeric_patch_for(patched)
    raw = SequencedRawProvider(
        [
            response(initial, request_id="exact-invalid-request"),
            response(
                {
                    "repairs": [
                        {
                            "binding_index": 0,
                            "binding_id": "binding:sleep-duration",
                            "replacement_rendered_text": "睡眠约 7 小时。",
                        }
                    ]
                },
                request_id="binding-patch-request",
            ),
            response(numeric_patch, request_id="numeric-patch-request"),
        ]
    )
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    with pytest.raises(
        ProductLLMProviderError,
        match="one constrained numeric repair attempt",
    ):
        model.generate(
            messages=[{"role": "user", "content": "Use accepted Evidence only."}],
            schema=NumericPostPatchFailureOutput,
            prompt_version="sleepcare.exact-then-numeric.v1",
            context_packet_id="context:exact-then-numeric:fail-closed",
        )

    assert len(raw.calls) == 3
    assert "numeric text repair patch" in raw.calls[2]["messages"][0]["content"]


def test_numeric_patch_fails_closed_on_post_patch_non_numeric_validation() -> None:
    class NonNumericPostPatchFailureOutput(SleepCareModelOutput):
        @model_validator(mode="after")
        def reject_numeric_patch_for_test(self) -> "NonNumericPostPatchFailureOutput":
            raise ValueError("post-numeric-patch non-numeric validation failure")

    invalid = _numeric_payload_with_text("记录日期为 2026-03-23；睡眠约 7 小时。")
    numeric_patch, _ = _numeric_patch_for(invalid)
    raw = SequencedRawProvider(
        [response(invalid), response(numeric_patch, request_id="numeric-patch")]
    )
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    with pytest.raises(
        ProductLLMProviderError,
        match="one constrained numeric repair attempt",
    ):
        model.generate(
            messages=[],
            schema=NonNumericPostPatchFailureOutput,
            prompt_version="sleepcare.post-numeric-failure.v1",
            context_packet_id="context:post-numeric-failure",
        )

    assert len(raw.calls) == 2


def test_exact_patch_does_not_fall_back_for_post_patch_non_numeric_error() -> None:
    class PostPatchFailureOutput(SleepCareModelOutput):
        @model_validator(mode="after")
        def reject_valid_binding_for_test(self) -> "PostPatchFailureOutput":
            raise ValueError("post-patch non-numeric validation failure")

    invalid = _substring_output(role="elder")
    raw = SequencedRawProvider(
        [
            response(invalid, request_id="exact-invalid-request"),
            response(
                {
                    "repairs": [
                        {
                            "binding_index": 0,
                            "binding_id": "binding:elder:summary",
                            "replacement_rendered_text": (
                                "当晚记录显示睡眠情况总体平稳。"
                            ),
                        }
                    ]
                },
                request_id="binding-patch-request",
            ),
        ]
    )
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    with pytest.raises(
        ProductLLMProviderError,
        match="after constrained exact-substring repair",
    ):
        model.generate(
            messages=[{"role": "user", "content": "Use accepted Evidence only."}],
            schema=PostPatchFailureOutput,
            prompt_version="sleepcare.post-patch-failure.v1",
            context_packet_id="context:post-patch-failure",
        )

    assert len(raw.calls) == 2


@pytest.mark.parametrize(
    ("repair_patch", "expected_error"),
    [
        (
            {
                "repairs": [
                    {
                        "binding_index": 0,
                        "binding_id": "binding:elder:summary",
                        "replacement_rendered_text": "正文中不存在的伪造文本。",
                    }
                ]
            },
            "replacement is not an exact Communication.text substring",
        ),
        (
            {
                "repairs": [
                    {
                        "binding_index": 0,
                        "binding_id": "binding:elder:summary",
                        "replacement_rendered_text": "当晚记录显示睡眠情况总体平稳。",
                        "source_ref": "claim:forged",
                    }
                ]
            },
            "returned an invalid patch",
        ),
        (
            {
                "repairs": [
                    {
                        "binding_index": 0,
                        "binding_id": "binding:elder:summary",
                        "replacement_rendered_text": "当晚记录显示睡眠情况总体平稳。",
                    }
                ],
                "communication_text": "试图同时改写正文。",
            },
            "returned an invalid patch",
        ),
    ],
)
def test_exact_substring_repair_rejects_illegal_or_source_mutating_patch(
    repair_patch: dict,
    expected_error: str,
) -> None:
    invalid = _substring_output(role="elder")
    raw = SequencedRawProvider([response(invalid), response(repair_patch)])
    model = OpenAICompatibleStructuredAgentModel(provider=raw)

    with pytest.raises(ProductLLMProviderError, match=expected_error):
        model.generate(
            messages=[{"role": "user", "content": "Use accepted Evidence only."}],
            schema=SleepCareModelOutput,
            prompt_version="sleepcare.substring.v1",
            context_packet_id="context:substring:fail-closed",
        )


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
