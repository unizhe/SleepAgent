from __future__ import annotations

import json
from datetime import date

import pytest

from sleepagent.radar_agent.agents import (
    ContextPacket,
    DialogueAgent,
    EvidencePacket,
    RadarDataAgent,
    ReportAgent,
    TaskContext,
)
from sleepagent.radar_agent.llm import (
    CloudLLMClient,
    CloudLLMConfig,
    DeterministicLLMFaultInjector,
    LLMFaultMode,
    ModelInvocationMetadata,
    ModelRouter,
)
from sleepagent.radar_agent.orchestrator import FullOrchestratorAgent, WorkflowNodeName
from sleepagent.radar_agent.provider import ReplayRadarProvider
from sleepagent.radar_agent.questionnaire import ModelRouterToneRewriter
from sleepagent.radar_agent.questionnaire.llm_tone import QuestionnaireToneChoice
from sleepagent.radar_agent.schemas import (
    EvidenceClaim,
    EvidenceLedger,
    RadarDataQualityStatus,
    RadarNightSummary,
    RagContext,
    ReviewStatus,
    RiskLevel,
)


class FakeResponse:
    status_code = 200

    def __init__(self, content: str) -> None:
        self.content = content

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class SequenceHTTPClient:
    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.calls = []

    def post(self, url, *, headers, json):
        self.calls.append(json)
        return FakeResponse(self.contents.pop(0))


def _router(
    *,
    http: SequenceHTTPClient,
    fault_mode: LLMFaultMode = LLMFaultMode.NONE,
    retry: int = 5,
) -> ModelRouter:
    return ModelRouter(
        CloudLLMClient(
            CloudLLMConfig(
                base_url="https://llm.invalid/v1",
                api_key="fault-test",
                model_id="test-model",
                retry=retry,
            ),
            http_client=http,
            fault_injector=DeterministicLLMFaultInjector(fault_mode),
            sleep=lambda _: None,
        )
    )


@pytest.mark.parametrize(
    "fault_mode",
    [LLMFaultMode.INVALID_JSON, LLMFaultMode.INVALID_SCHEMA],
)
def test_structured_failure_retries_only_once_even_when_configured_higher(
    fault_mode: LLMFaultMode,
) -> None:
    http = SequenceHTTPClient('{"tone":"warm"}', '{"tone":"warm"}')
    router = _router(http=http, fault_mode=fault_mode, retry=5)

    result = router.generate_structured(
        messages=[{"role": "user", "content": "choose tone"}],
        schema=QuestionnaireToneChoice,
        metadata=ModelInvocationMetadata(
            model_id="test-model", prompt_version="fault.v1"
        ),
        fallback=lambda: QuestionnaireToneChoice(tone="neutral"),
    )

    assert len(http.calls) == 2
    assert result.value.tone == "neutral"
    assert result.metadata.fallback_used is True


def test_llm_unavailable_keeps_reports_risk_alerts_and_main_chain_running() -> None:
    http = SequenceHTTPClient()
    report_agent = ReportAgent(
        model_router=_router(http=http, fault_mode=LLMFaultMode.UNAVAILABLE)
    )
    orchestrator = FullOrchestratorAgent(
        radar_data_agent=RadarDataAgent(
            ReplayRadarProvider(scenario="normal_night")
        ),
        report_agent=report_agent,
    )

    decision = orchestrator.run(_orchestration_context())

    assert decision.node == WorkflowNodeName.PUBLISH_ARTIFACTS
    assert decision.evidence_ledger is not None
    assert decision.evidence_ledger.derived_metrics["risk_level"] == "info"
    report_result = next(
        item for item in decision.accepted_results if item.agent_name.value == "report"
    )
    assert {
        item["generation_mode"] for item in report_result.output_payload["reports"]
    } == {"fallback"}
    assert "agent_unavailable" in report_result.safety_flags
    assert any(message.intent == "agent_unavailable" for message in decision.a2a_messages)
    assert any(item.agent_name.value == "alert_care" for item in decision.accepted_results)
    assert http.calls == []


def test_chat_unavailable_returns_ledger_grounded_short_fallback() -> None:
    result = DialogueAgent(
        model_router=_router(
            http=SequenceHTTPClient(), fault_mode=LLMFaultMode.TIMEOUT
        )
    ).run(_grounded_context(purpose="chat"))

    dialogue = result.output_payload["dialogue"]
    assert dialogue["generation_mode"] == "fallback"
    assert "详细解释服务暂时不可用" in dialogue["answer"]
    assert "近几晚夜间离床次数较个人基线增加" in dialogue["answer"]
    assert set(dialogue["evidence_refs"]).issubset(set(result.evidence_refs))
    assert "agent_unavailable" in result.safety_flags


def test_questionnaire_llm_can_choose_tone_but_cannot_change_reviewed_body() -> None:
    original = "昨晚是否起夜？"
    warm_http = SequenceHTTPClient('{"tone":"warm"}')
    rewritten = ModelRouterToneRewriter(_router(http=warm_http, retry=0)).rewrite_question(
        text=original,
        role="elder",
    )
    assert rewritten.endswith(original)

    bad_http = SequenceHTTPClient('{"tone":"warm"}', '{"tone":"warm"}')
    fallback_rewriter = ModelRouterToneRewriter(
        _router(http=bad_http, fault_mode=LLMFaultMode.INVALID_SCHEMA)
    )
    assert fallback_rewriter.rewrite_question(text=original, role="elder") == original
    assert fallback_rewriter.last_status == {
        "status": "fallback",
        "fallback_used": True,
        "fallback_reason": "CloudLLMSchemaError",
    }


def test_doctor_llm_medical_judgment_is_retried_once_then_template_used() -> None:
    context = _grounded_context(purpose="report")
    templates = ReportAgent().run(context).output_payload["reports"]
    responses: list[str] = []
    for report in templates:
        payload = {
            "role": report["role"],
            "tone": {"elder": "warm", "family": "concise", "doctor": "clinical"}[
                report["role"]
            ],
            "claim_ids": report["claim_ids"],
            "evidence_refs": report["evidence_refs"],
            "caveats": report["caveats"],
            "risk_level": "watch",
        }
        if report["role"] == "doctor":
            payload["content"] = "已确诊睡眠呼吸暂停。"
            responses.extend([json.dumps(payload, ensure_ascii=False)] * 2)
        else:
            responses.append(json.dumps(payload, ensure_ascii=False))
    http = SequenceHTTPClient(*responses)

    result = ReportAgent(model_router=_router(http=http, retry=1)).run(context)
    doctor = next(
        report for report in result.output_payload["reports"] if report["role"] == "doctor"
    )

    assert len(http.calls) == 4
    assert doctor["generation_mode"] == "fallback"
    assert "确诊" not in doctor["content"]
    assert any(
        "毫米波雷达不能替代 PSG" in caveat for caveat in doctor["caveats"]
    )


def _orchestration_context() -> ContextPacket:
    return ContextPacket(
        task_context=TaskContext(
            task_id="task-llm-outage",
            trace_id="trace-llm-outage",
            role="family",
            purpose="orchestration",
            allowed_actions=["run_full_chain"],
        ),
        evidence_packet=EvidencePacket(
            data_quality={"night_of": date(2026, 7, 9).isoformat()}
        ),
    )


def _grounded_context(*, purpose: str) -> ContextPacket:
    summary = RadarNightSummary(
        radar_device_id="radar-001",
        subject_id="elder-001",
        night_of=date(2026, 7, 10),
        total_sleep_minutes=380,
        out_of_bed_count=4,
        movement_count=20,
        data_coverage_ratio=0.93,
        data_quality_status=RadarDataQualityStatus.GOOD,
        source_report_ref="night-summary:001",
    )
    claim = EvidenceClaim(
        claim_id="claim-watch",
        task_id="task-grounded",
        text="近几晚夜间离床次数较个人基线增加。",
        evidence_refs=["night-summary:001"],
        confidence=0.74,
        risk_level=RiskLevel.WATCH,
        caveats=["雷达观察不能替代临床判断。"],
        generated_by="trend",
        review_status=ReviewStatus.REVIEWED,
    )
    ledger = EvidenceLedger(
        ledger_id="ledger-grounded",
        task_id="task-grounded",
        canonical_evidence_refs=["night-summary:001"],
        derived_metrics={"risk_level": "watch", "data_quality_status": "good"},
        claims=[claim],
        confidence=0.74,
        caveats=["仅作健康观察参考。"],
        review_status=ReviewStatus.REVIEWED,
    )
    return ContextPacket(
        context_packet_id="context:grounded",
        task_context=TaskContext(
            task_id="task-grounded",
            trace_id="trace-grounded",
            role="family",
            purpose=purpose,
        ),
        evidence_packet=EvidencePacket(
            night_summaries=[summary],
            data_quality={"user_question": "昨晚怎么样？"},
            evidence_ledger=ledger,
        ),
        rag_context=RagContext(
            chunk_ids=["seed:001"],
            citation_ids=["citation:001"],
            snippets=["夜间离床变化应结合连续趋势观察。"],
        ),
    )
