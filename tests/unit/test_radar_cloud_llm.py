from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from sleepagent.integrations.llm import (
    CloudLLMClient,
    CloudLLMConfig,
    CloudLLMInvalidJSONError,
    CloudLLMPrivacyError,
    CloudLLMRateLimitError,
    CloudLLMSchemaError,
    CloudLLMTimeoutError,
    ModelInvocationMetadata,
    ModelRouter,
    build_cloud_context_envelope,
    task_service_llm_audit_sink,
)
from sleepagent.product_runtime.schemas import (
    ContextPacket,
    EvidenceClaim,
    EvidenceLedger,
    EvidencePacket,
    RadarDataQualityStatus,
    RadarNightSummary,
    RagContext,
    ReviewStatus,
    RiskLevel,
    TaskContext,
)
class StructuredReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    score: int


class FakeResponse:
    def __init__(self, payload=None, *, status_code: int = 200, json_error=False):
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise json.JSONDecodeError("bad", "{", 0)
        return self.payload


class SequenceHTTPClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _response(content: str) -> FakeResponse:
    return FakeResponse({"choices": [{"message": {"content": content}}]})


def _config(*, retry: int = 1) -> CloudLLMConfig:
    return CloudLLMConfig(
        base_url="https://llm.example/v1",
        api_key="secret-key",
        model_id="generic-chat-model",
        timeout=4.5,
        retry=retry,
    )


def _metadata() -> ModelInvocationMetadata:
    return ModelInvocationMetadata(
        model_provider="openai-compatible",
        model_id="caller-placeholder",
        prompt_version="report.v3",
        context_packet_id="context:001",
    )


def test_cloud_client_uses_openai_compatible_configuration_without_vendor_fields() -> None:
    http = SequenceHTTPClient(_response("plain response"))
    client = CloudLLMClient(_config(retry=0), http_client=http)

    text = client.generate_text(
        messages=[{"role": "user", "content": "summarize claims"}],
        metadata=_metadata(),
    )

    assert text == "plain response"
    call = http.calls[0]
    assert call["url"] == "https://llm.example/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer secret-key"
    assert call["json"]["model"] == "generic-chat-model"
    assert call["json"]["stream"] is False
    assert "thinking" not in call["json"]
    with pytest.raises(ValueError, match="cannot override"):
        CloudLLMConfig(
            base_url="https://llm.example/v1",
            api_key="key",
            model_id="model",
            extra_body={"messages": [{"role": "user", "content": "override"}]},
        )


def test_timeout_retries_then_records_failure_summary() -> None:
    timeout = httpx.ReadTimeout("timed out")
    http = SequenceHTTPClient(timeout, timeout)
    audits = []
    client = CloudLLMClient(
        _config(retry=1),
        http_client=http,
        audit_sink=audits.append,
        sleep=lambda seconds: None,
    )

    with pytest.raises(CloudLLMTimeoutError):
        client.generate_text(
            messages=[{"role": "user", "content": "safe summary"}],
            metadata=_metadata(),
        )

    assert len(http.calls) == 2
    assert audits[-1].phase == "failed"
    assert audits[-1].attempt_count == 2
    assert audits[-1].error_summary == "CloudLLMTimeoutError"
    assert "secret-key" not in audits[-1].model_dump_json()


def test_invalid_json_content_retries_and_fails_closed() -> None:
    http = SequenceHTTPClient(_response("{bad-json"), _response("still bad"))
    client = CloudLLMClient(
        _config(retry=1), http_client=http, sleep=lambda seconds: None
    )

    with pytest.raises(CloudLLMInvalidJSONError):
        client.generate_structured(
            messages=[{"role": "user", "content": "return JSON"}],
            schema=StructuredReply,
            metadata=_metadata(),
        )

    assert len(http.calls) == 2


def test_invalid_json_response_envelope_fails_closed() -> None:
    http = SequenceHTTPClient(FakeResponse(json_error=True))
    client = CloudLLMClient(_config(retry=0), http_client=http)

    with pytest.raises(CloudLLMInvalidJSONError, match="envelope"):
        client.generate_text(
            messages=[{"role": "user", "content": "safe summary"}],
            metadata=_metadata(),
        )


def test_schema_failure_retries_once_then_router_uses_validated_fallback() -> None:
    invalid = json.dumps({"answer": "missing score"})
    http = SequenceHTTPClient(_response(invalid), _response(invalid))
    client_audits = []
    route_audits = []
    client = CloudLLMClient(
        _config(retry=1),
        http_client=http,
        audit_sink=client_audits.append,
        sleep=lambda seconds: None,
    )
    router = ModelRouter(client, audit_sink=route_audits.append)

    result = router.generate_structured(
        messages=[{"role": "user", "content": "return JSON"}],
        schema=StructuredReply,
        metadata=_metadata(),
        fallback=lambda: StructuredReply(answer="template", score=0),
        fallback_summary="structured_template",
    )

    assert result.value.answer == "template"
    assert result.metadata.output_schema_status == "fallback"
    assert result.metadata.fallback_used is True
    assert client_audits[-1].output_schema_status == "invalid"
    assert client_audits[-1].attempt_count == 2
    assert route_audits[-1].fallback_summary == "structured_template"
    assert route_audits[-1].error_summary == "CloudLLMSchemaError"


def test_structured_fallback_is_revalidated_and_fails_closed() -> None:
    http = SequenceHTTPClient(_response("{bad"), _response("{bad"))
    router = ModelRouter(
        CloudLLMClient(
            _config(retry=1), http_client=http, sleep=lambda seconds: None
        )
    )

    with pytest.raises(CloudLLMSchemaError, match="fallback"):
        router.generate_structured(
            messages=[{"role": "user", "content": "return JSON"}],
            schema=StructuredReply,
            metadata=_metadata(),
            fallback=lambda: {"answer": "still missing score"},
        )


def test_rate_limit_retries_and_can_recover() -> None:
    http = SequenceHTTPClient(
        FakeResponse({}, status_code=429),
        _response(json.dumps({"answer": "ok", "score": 1})),
    )
    client = CloudLLMClient(
        _config(retry=1), http_client=http, sleep=lambda seconds: None
    )

    result = client.generate_structured(
        messages=[{"role": "user", "content": "return JSON"}],
        schema=StructuredReply,
        metadata=_metadata(),
    )

    assert result.score == 1
    assert len(http.calls) == 2


def test_rate_limit_exhaustion_raises_specific_error() -> None:
    http = SequenceHTTPClient(
        FakeResponse({}, status_code=429),
        FakeResponse({}, status_code=429),
    )
    client = CloudLLMClient(
        _config(retry=1), http_client=http, sleep=lambda seconds: None
    )

    with pytest.raises(CloudLLMRateLimitError):
        client.generate_text(
            messages=[{"role": "user", "content": "safe"}],
            metadata=_metadata(),
        )


def test_validate_schema_is_public_and_strict() -> None:
    client = CloudLLMClient(_config())

    valid = client.validate_schema(
        payload={"answer": "ok", "score": 1}, schema=StructuredReply
    )
    assert valid.score == 1
    with pytest.raises(CloudLLMSchemaError):
        client.validate_schema(
            payload={"answer": "ok", "score": 1, "diagnosis": "not allowed"},
            schema=StructuredReply,
        )


def test_text_fallback_must_be_non_empty() -> None:
    http = SequenceHTTPClient(_response("unused"))
    router = ModelRouter(
        CloudLLMClient(
            CloudLLMConfig(
                base_url="https://llm.example/v1",
                api_key=None,
                model_id="generic-chat-model",
                retry=0,
            ),
            http_client=http,
        )
    )

    with pytest.raises(CloudLLMSchemaError, match="non-empty"):
        router.generate_text(
            messages=[{"role": "user", "content": "safe"}],
            metadata=_metadata(),
            fallback=lambda: "",
        )
    assert http.calls == []


def test_cloud_context_contains_only_summaries_claims_minimum_context_and_rag() -> None:
    context = _context()

    envelope = build_cloud_context_envelope(context)
    serialized = json.dumps(envelope, ensure_ascii=False)

    assert envelope["context_packet_id"] == "context:cloud:001"
    assert envelope["night_summaries"][0]["total_sleep_minutes"] == 380
    assert envelope["claims"][0]["claim_id"] == "claim:001"
    assert envelope["rag_context"]["snippets"] == ["reviewed sleep guidance"]
    assert "vital_snapshots" not in envelope
    assert "supplementary_documents" not in envelope
    assert "provider_secret" not in serialized
    assert "vendor payload" not in serialized.lower()


def test_raw_radar_text_is_blocked_before_any_http_request() -> None:
    http = SequenceHTTPClient(_response("unused"))
    client = CloudLLMClient(_config(), http_client=http)

    with pytest.raises(CloudLLMPrivacyError):
        client.generate_text(
            messages=[
                {"role": "user", "content": "upload raw_radar_stream now"}
            ],
            metadata=_metadata(),
        )

    assert http.calls == []


def test_router_audit_records_model_prompt_context_validation_and_fallback_only() -> None:
    http = SequenceHTTPClient(_response("{bad"), _response("{bad"))
    audits = []
    client = CloudLLMClient(
        _config(retry=1), http_client=http, sleep=lambda seconds: None
    )
    router = ModelRouter(client, audit_sink=audits.append)

    result = router.generate_structured(
        messages=[{"role": "system", "content": "use reviewed facts"}],
        schema=StructuredReply,
        metadata=_metadata().model_copy(update={"context_packet_id": None}),
        context_packet=_context(),
        fallback=lambda: StructuredReply(answer="fallback", score=0),
        fallback_summary="role_report_template",
    )

    assert result.metadata.model_id == "generic-chat-model"
    assert result.metadata.prompt_version == "report.v3"
    assert result.metadata.context_packet_id == "context:cloud:001"
    assert result.metadata.output_schema_status == "fallback"
    record = audits[-1]
    assert record.model_id == "generic-chat-model"
    assert record.prompt_version == "report.v3"
    assert record.context_packet_id == "context:cloud:001"
    assert record.fallback_summary == "role_report_template"
    assert "reviewed facts" not in record.model_dump_json()


def test_task_service_audit_adapter_records_only_minimized_invocation_summary() -> None:
    class FakeTaskService:
        def __init__(self):
            self.calls = []

        def record_audit(self, task_id, **kwargs):
            self.calls.append({"task_id": task_id, **kwargs})

    service = FakeTaskService()
    sink = task_service_llm_audit_sink(service, task_id="task-cloud")
    http = SequenceHTTPClient(_response("safe text"))
    client = CloudLLMClient(
        _config(retry=0), http_client=http, audit_sink=sink
    )

    client.generate_text(
        messages=[{"role": "user", "content": "private prompt content"}],
        metadata=_metadata(),
    )

    record = service.calls[0]
    assert record["action"] == "llm_completed"
    assert record["target_ref"] == "generic-chat-model"
    serialized = json.dumps(record, ensure_ascii=False, default=str)
    assert "private prompt content" not in serialized
    assert "secret-key" not in serialized


def _context() -> ContextPacket:
    summary = RadarNightSummary(
        radar_device_id="radar-001",
        subject_id="elder-001",
        night_of=date(2026, 7, 10),
        total_sleep_minutes=380,
        out_of_bed_count=3,
        movement_count=18,
        data_coverage_ratio=0.92,
        data_quality_status=RadarDataQualityStatus.GOOD,
        source_report_ref="night:001",
    )
    claim = EvidenceClaim(
        claim_id="claim:001",
        task_id="task-cloud",
        text="近七天夜间离床次数有所增加。",
        evidence_refs=["night:001"],
        confidence=0.72,
        risk_level=RiskLevel.WATCH,
        caveats=["非诊断。"],
        generated_by="trend",
        review_status=ReviewStatus.REVIEWED,
    )
    ledger = EvidenceLedger(
        ledger_id="ledger:cloud",
        task_id="task-cloud",
        canonical_evidence_refs=["night:001"],
        derived_metrics={"risk_level": "watch"},
        claims=[claim],
        confidence=0.72,
        review_status=ReviewStatus.REVIEWED,
    )
    return ContextPacket(
        context_packet_id="context:cloud:001",
        task_context=TaskContext(
            task_id="task-cloud",
            trace_id="trace-cloud",
            role="family",
            stage="RoleReportGeneration",
            purpose="report",
            allowed_actions=["generate_role_reports"],
        ),
        evidence_packet=EvidencePacket(
            night_summaries=[summary],
            data_quality={
                "risk_level": "watch",
                "provider_secret": "must-not-leave",
            },
            evidence_ledger=ledger,
        ),
        memory_snippets=["family prefers concise summaries"],
        rag_context=RagContext(
            chunk_ids=["seed:001"],
            citation_ids=["citation:001"],
            snippets=["reviewed sleep guidance"],
            caveats=["not diagnostic"],
            source_metadata=[
                {
                    "source_type": "medical_safety",
                    "review_status": "reviewed",
                    "provider_secret": "must-not-leave",
                }
            ],
        ),
    )
