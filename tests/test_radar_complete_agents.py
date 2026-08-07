from __future__ import annotations

from datetime import date

from sleepagent.radar_agent.agents import (
    AlertCareAgent,
    ContextPacket,
    DialogueAgent,
    EvidencePacket,
    MemoryAgent,
    RAGAgent,
    ReportAgent,
    TaskContext,
)
from sleepagent.radar_agent.schemas import (
    EvidenceClaim,
    EvidenceLedger,
    QuestionnaireBank,
    RadarDataQualityStatus,
    RadarNightSummary,
    ReviewStatus,
    RiskLevel,
)


def test_rag_agent_only_returns_reviewed_versioned_seed_sources() -> None:
    result = RAGAgent().run(_context(purpose="rag", data_quality={"rag_query": "雷达 数据 质量"}))

    assert result.claims == []
    assert result.evidence_refs
    assert all(source["review_status"] == "reviewed" for source in result.output_payload["sources"])
    assert all(source["version"] for source in result.output_payload["sources"])
    assert "reviewed_seed_only" in result.safety_flags


def test_report_agent_generates_three_roles_from_one_fact_set() -> None:
    result = ReportAgent().run(_context(purpose="report"))
    reports = result.output_payload["reports"]

    assert {report["role"] for report in reports} == {"elder", "family", "doctor"}
    assert len({tuple(report["claim_ids"]) for report in reports}) == 1
    assert len({tuple(report["evidence_refs"]) for report in reports}) == 1
    assert all(report["caveats"] for report in reports)
    assert result.claims == []


def test_dialogue_agent_is_ledger_grounded_and_uses_reviewed_question_bank() -> None:
    bank = QuestionnaireBank(
        bank_id="sleep-followup",
        version="1.0.0",
        reviewed=True,
        questions={"daytime_sleepiness": "今天白天是否明显困倦？", "device_position": "设备位置是否变化？"},
    )
    result = DialogueAgent().run(
        _context(
            purpose="chat",
            data_quality={
                "user_question": "为什么最近夜里醒得多？",
                "questionnaire_bank": bank.model_dump(mode="json"),
            },
        )
    )
    response = result.output_payload["dialogue"]

    assert response["evidence_refs"] == result.evidence_refs
    assert response["questionnaire_ids"][:2] == ["daytime_sleepiness", "device_position"]
    assert 2 <= len(response["questionnaire_ids"]) <= 3
    assert all(
        item["question_source"] in {"bank", "skill"}
        for item in response["questionnaire_candidates"]
    )
    assert result.claims == []


def test_alert_care_agent_proposes_but_does_not_execute_confirmed_action() -> None:
    result = AlertCareAgent().run(_context(purpose="alert"))
    decision = result.output_payload["alert_care"]

    assert decision["risk_level"] == "watch"
    assert decision["external_action_executed"] is False
    assert decision["confirmation_requests"][0]["status"] == "pending"
    assert "no_external_side_effect" in result.safety_flags


def test_memory_agent_only_returns_confirmation_gated_candidate() -> None:
    result = MemoryAgent().run(_context(purpose="memory"))
    proposal = result.output_payload["memory_proposal"]

    assert proposal["write_performed"] is False
    assert proposal["candidates"]
    assert proposal["candidates"][0]["requires_confirmation"] is True
    assert proposal["candidates"][0]["approved"] is False
    assert "no_long_term_write" in result.safety_flags


def _context(*, purpose: str, data_quality: dict | None = None) -> ContextPacket:
    summary = RadarNightSummary(
        radar_device_id="radar-001",
        subject_id="elder-001",
        night_of=date(2026, 7, 10),
        total_sleep_minutes=380,
        out_of_bed_count=4,
        movement_count=20,
        data_coverage_ratio=0.93,
        data_quality_status=RadarDataQualityStatus.GOOD,
        source_report_ref="night-summary:radar-001:2026-07-10",
    )
    claim = EvidenceClaim(
        claim_id="claim-watch",
        task_id="task-complete",
        text="近几晚夜间离床次数较个人基线增加。",
        evidence_refs=[summary.source_report_ref],
        confidence=0.74,
        risk_level=RiskLevel.WATCH,
        caveats=["雷达观察不能替代临床判断。"],
        generated_by="trend",
        review_status=ReviewStatus.REVIEWED,
    )
    ledger = EvidenceLedger(
        ledger_id="ledger-complete",
        task_id="task-complete",
        canonical_evidence_refs=[summary.source_report_ref],
        derived_metrics={"risk_level": "watch", "data_quality_status": "good"},
        claims=[claim],
        confidence=0.74,
        caveats=["仅作健康观察参考。"],
        review_status=ReviewStatus.REVIEWED,
    )
    return ContextPacket(
        task_context=TaskContext(
            task_id="task-complete",
            trace_id="trace-complete",
            role="family",
            purpose=purpose,
        ),
        evidence_packet=EvidencePacket(
            night_summaries=[summary],
            data_quality=data_quality or {},
            evidence_ledger=ledger,
        ),
    )
