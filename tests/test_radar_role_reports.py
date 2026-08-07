from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from sleepagent.radar_agent.agents import (
    ContextPacket,
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
    ModelRouter,
)
from sleepagent.radar_agent.orchestrator import (
    FullOrchestratorAgent,
    OrchestratorDecision,
    WorkflowNodeName,
)
from sleepagent.radar_agent.persistence import RadarPersistenceStore, RadarSubject
from sleepagent.radar_agent.provider import ReplayRadarProvider
from sleepagent.radar_agent.runtime import RadarTaskStatus, TaskService
from sleepagent.radar_agent.schemas import (
    EvidenceClaim,
    EvidenceLedger,
    HumanConfirmationRequest,
    QuestionnaireEntry,
    RadarDataQualityStatus,
    RadarDevice,
    RadarDeviceStatus,
    RadarNightSummary,
    ReviewStatus,
    RiskLevel,
    RoleReportArtifact,
)


class NeverHTTPClient:
    def post(self, url, *, headers, json):
        raise AssertionError("fault injection must fail before HTTP")


class StaticOrchestrator:
    def __init__(self, decision: OrchestratorDecision) -> None:
        self.decision = decision

    def run(self, context: ContextPacket) -> OrchestratorDecision:
        return self.decision


def test_three_roles_share_one_fact_snapshot_but_have_distinct_presentations() -> None:
    context = _context()
    result = ReportAgent().run(context)
    reports = {
        report["role"]: RoleReportArtifact.model_validate(report)
        for report in result.output_payload["reports"]
    }

    assert set(reports) == {"elder", "family", "doctor"}
    assert {report.source_ledger_id for report in reports.values()} == {
        "ledger-role-reports"
    }
    assert {report.risk_level for report in reports.values()} == {RiskLevel.ESCALATE}
    assert len({tuple(report.claim_ids) for report in reports.values()}) == 1
    assert len(
        {
            tuple(claim.model_dump_json() for claim in report.facts)
            for report in reports.values()
        }
    ) == 1
    for report in reports.values():
        assert all(claim.text in report.content for claim in report.facts)
        assert {
            "本报告由 AI 辅助整理，内容来自结构化证据。",
            "本报告仅用于睡眠健康观察。",
            "本报告不构成临床诊断或医疗建议。",
            "本项目不宣称 HIPAA/FDA、医疗器械或临床诊断合规。",
        }.issubset(set(report.safety_notices))
        assert all(notice in report.content for notice in report.safety_notices)

    elder = reports["elder"]
    family = reports["family"]
    doctor = reports["doctor"]
    assert "建议让家人协助" in elder.content
    assert len(elder.content) < len(family.content) < len(doctor.content)
    assert "【趋势】" in family.content
    assert "【异常线索】" in family.content
    assert family.confirmation_actions == [
        "确认导出医生材料",
        "确认预约评估建议卡片",
    ]
    assert "【结构化证据链】" in doctor.content
    assert "【数据质量】" in doctor.content
    assert "【补充问卷】" in doctor.content
    assert "【来源】" in doctor.content
    assert "【完整 caveat】" in doctor.content


def test_doctor_report_contains_quality_questionnaire_sources_and_full_caveats() -> None:
    doctor = next(
        RoleReportArtifact.model_validate(report)
        for report in ReportAgent().run(_context()).output_payload["reports"]
        if report["role"] == "doctor"
    )

    assert doctor.data_quality["status"] == "partial"
    assert doctor.data_quality["coverage_ratio"] == 0.78
    assert doctor.data_quality["missing_intervals"] == ["02:10-02:35"]
    assert doctor.questionnaire_entries[0].question_id == "q-daytime-sleepiness"
    assert doctor.questionnaire_entries[0].answer == "明显"
    assert "night-summary:role-report" in doctor.source_refs
    assert "source:questionnaire:001" in doctor.source_refs
    assert doctor.structured_summary["source_ledger_id"] == "ledger-role-reports"
    assert doctor.structured_summary["evidence_chain"][0]["claim_id"] == "claim-trend"
    assert doctor.structured_summary["non_diagnostic_boundary"] is True
    assert "数据存在缺失区间。" in doctor.caveats
    assert any("不能替代 PSG" in caveat for caveat in doctor.caveats)

    tampered = doctor.model_dump(mode="json")
    tampered["risk_level"] = "watch"
    with pytest.raises(ValueError, match="structured summary must match"):
        RoleReportArtifact.model_validate(tampered)
    compliance_claim = doctor.model_dump(mode="json")
    compliance_claim["content"] += "\nHIPAA compliant and FDA approved."
    with pytest.raises(ValueError, match="cannot claim"):
        RoleReportArtifact.model_validate(compliance_claim)


def test_runtime_saves_initial_reports_and_every_modification_as_new_version() -> None:
    service, store, task, context = _runtime()
    report_result = ReportAgent().run(context)
    decision = OrchestratorDecision(
        task_id=task.task_id,
        node=WorkflowNodeName.PUBLISH_ARTIFACTS,
        accepted_results=[report_result],
        evidence_ledger=context.evidence_packet.evidence_ledger,
    )

    service.execute(task.task_id, StaticOrchestrator(decision), context)

    all_versions = store.list_task_artifact_versions(task.task_id)
    report_versions = [version for version in all_versions if version.report]
    assert len(report_versions) == 3
    assert {version.version for version in report_versions} == {1}
    family_v1 = next(
        version for version in report_versions if version.report.role == "family"
    )
    family_v2 = service.save_artifact(
        task.task_id,
        family_v1.report.model_copy(
            update={"content": family_v1.report.content + "\n已由家属补充批注。"}
        ),
    )

    family_history = store.list_task_artifact_versions(
        task.task_id,
        artifact_id=family_v1.artifact_id,
    )
    assert [version.version for version in family_history] == [1, 2]
    assert family_v2.report.content.endswith("已由家属补充批注。")
    assert family_v2.metadata["source_ledger_id"] == "ledger-role-reports"
    assert family_v2.metadata["prompt_version"] == "role-report-template.v1"
    assert family_v2.metadata["generation_mode"] == "template"


def test_real_full_orchestrator_publishes_three_versioned_reports_from_one_ledger() -> None:
    service, store, task, _ = _runtime()
    context = ContextPacket(
        task_context=TaskContext(
            task_id=task.task_id,
            trace_id=task.trace_id,
            role="family",
            purpose="orchestration",
            allowed_actions=["run_full_chain"],
        ),
        evidence_packet=EvidencePacket(
            data_quality={"night_of": date(2026, 7, 11).isoformat()}
        ),
    )
    decision = service.execute(
        task.task_id,
        FullOrchestratorAgent(
            radar_data_agent=RadarDataAgent(
                ReplayRadarProvider(scenario="normal_night")
            )
        ),
        context,
    )

    versions = store.list_task_artifact_versions(task.task_id)
    reports = [version.report for version in versions if version.report is not None]
    assert len(reports) == 3
    assert {report.role for report in reports} == {"elder", "family", "doctor"}
    assert {report.source_ledger_id for report in reports} == {
        decision.evidence_ledger.ledger_id
    }
    assert {report.risk_level.value for report in reports} == {
        decision.evidence_ledger.derived_metrics["risk_level"]
    }
    assert len({tuple(report.claim_ids) for report in reports}) == 1
    assert service.get_task(task.task_id).status == RadarTaskStatus.COMPLETED
    assert all(
        not confirmation.blocks_daily_flow
        for confirmation in store.list_confirmations(task.task_id)
    )


def test_llm_outage_doctor_report_can_be_exported_as_structured_fallback() -> None:
    service, store, task, context = _runtime()
    router = ModelRouter(
        CloudLLMClient(
            CloudLLMConfig(
                base_url="https://llm.invalid/v1",
                api_key="fault-test",
                model_id="test-model",
                retry=1,
            ),
            http_client=NeverHTTPClient(),
            fault_injector=DeterministicLLMFaultInjector(
                LLMFaultMode.UNAVAILABLE
            ),
            sleep=lambda _: None,
        )
    )
    report_result = ReportAgent(model_router=router).run(context)
    confirmation = HumanConfirmationRequest(
        confirmation_id="confirm-doctor-export-fallback",
        task_id=task.task_id,
        action_type="export_doctor_material",
        requested_role="family",
        reason="Export structured doctor material.",
    )
    decision = OrchestratorDecision(
        task_id=task.task_id,
        node=WorkflowNodeName.PUBLISH_ARTIFACTS,
        accepted_results=[report_result],
        confirmation_requests=[confirmation],
        evidence_ledger=context.evidence_packet.evidence_ledger,
    )
    service.execute(task.task_id, StaticOrchestrator(decision), context)
    service.resolve_confirmation(
        task.task_id,
        confirmation.confirmation_id,
        approved=True,
        actor_id="family-user",
        actor_role="family",
    )

    exported = service.export_doctor_material(
        task.task_id,
        confirmation_id=confirmation.confirmation_id,
    )
    same_export = service.export_doctor_material(
        task.task_id,
        confirmation_id=confirmation.confirmation_id,
    )

    assert exported.artifact_type == "doctor_material_export"
    assert same_export.artifact_version_id == exported.artifact_version_id
    assert len(
        store.list_task_artifact_versions(
            task.task_id, artifact_id=exported.artifact_id
        )
    ) == 1
    assert exported.payload["generation_mode"] == "fallback"
    assert exported.payload["source_ledger_id"] == "ledger-role-reports"
    assert exported.payload["risk_level"] == "escalate"
    assert exported.payload["evidence_chain"][0]["claim_id"] == "claim-trend"
    assert exported.payload["data_quality"]["status"] == "partial"
    assert exported.payload["questionnaire_entries"][0]["question_id"] == (
        "q-daytime-sleepiness"
    )
    assert "source:questionnaire:001" in exported.payload["source_refs"]
    assert any("不能替代 PSG" in item for item in exported.payload["caveats"])
    assert "本报告由 AI 辅助整理，内容来自结构化证据。" in exported.payload[
        "safety_notices"
    ]
    assert "本项目不宣称 HIPAA/FDA、医疗器械或临床诊断合规。" in (
        exported.payload["safety_notices"]
    )
    assert exported.payload["non_diagnostic_boundary"] is True
    assert any(
        log.action == "doctor_material_exported"
        for log in store.list_audit_logs(task.task_id)
    )


def _runtime():
    store = RadarPersistenceStore.connect_sqlite(sqlite3.connect(":memory:"))
    store.save_subject(
        RadarSubject(subject_id="elder-role-report", display_name="Demo elder")
    )
    store.save_device(
        RadarDevice(
            radar_device_id="radar-role-report",
            display_name="Demo radar",
            provider="replay",
            status=RadarDeviceStatus.ONLINE,
            bound_subject_id="elder-role-report",
        )
    )
    service = TaskService(
        store, validate_bindings=False, require_authorization=False
    )
    task = service.create_task(
        subject_id="elder-role-report",
        radar_device_id="radar-role-report",
        role="family",
        scenario="escalate_candidate",
    )
    context = _context(task_id=task.task_id, trace_id=task.trace_id)
    return service, store, task, context


def _context(
    *,
    task_id: str = "task-role-reports",
    trace_id: str = "trace-role-reports",
) -> ContextPacket:
    summary = RadarNightSummary(
        radar_device_id="radar-role-report",
        subject_id="elder-role-report",
        night_of=date(2026, 7, 11),
        device_status=RadarDeviceStatus.ONLINE,
        total_sleep_minutes=315,
        out_of_bed_count=6,
        movement_count=35,
        data_coverage_ratio=0.78,
        data_quality_status=RadarDataQualityStatus.PARTIAL,
        confidence_label="low_confidence",
        invalid_reading_count=4,
        missing_intervals=["02:10-02:35"],
        quality_reasons=["夜间存在短时缺失。"],
        caveats=["数据存在缺失区间。"],
        source_report_ref="night-summary:role-report",
    )
    questionnaire = QuestionnaireEntry(
        entry_id="questionnaire:role-report:001",
        subject_id="elder-role-report",
        role="family",
        question_id="q-daytime-sleepiness",
        answer="明显",
        prompt_text="近期白天是否明显困倦？",
        evidence_ref="source:questionnaire:001",
    )
    claims = [
        EvidenceClaim(
            claim_id="claim-trend",
            task_id=task_id,
            text="近七晚夜间离床次数较个人基线上升。",
            evidence_refs=["night-summary:role-report"],
            confidence=0.76,
            risk_level=RiskLevel.WATCH,
            caveats=["趋势受部分缺失数据影响。"],
            generated_by="trend",
            review_status=ReviewStatus.REVIEWED,
        ),
        EvidenceClaim(
            claim_id="claim-risk",
            task_id=task_id,
            text="多项连续观察线索叠加，建议整理材料进一步评估。",
            evidence_refs=[
                "night-summary:role-report",
                "source:questionnaire:001",
            ],
            confidence=0.71,
            risk_level=RiskLevel.ESCALATE,
            caveats=["该线索不构成临床诊断。"],
            generated_by="risk_signal",
            review_status=ReviewStatus.REVIEWED,
        ),
    ]
    ledger = EvidenceLedger(
        ledger_id="ledger-role-reports",
        task_id=task_id,
        canonical_evidence_refs=[
            "night-summary:role-report",
            "source:questionnaire:001",
        ],
        derived_metrics={
            "risk_level": "escalate",
            "data_quality_status": "partial",
            "data_coverage_ratio": 0.78,
        },
        questionnaire_entries=[questionnaire],
        claims=claims,
        confidence=0.71,
        uncertainty="部分缺失区间降低趋势置信度。",
        caveats=["毫米波雷达仅用于健康观察。"],
        review_status=ReviewStatus.REVIEWED,
    )
    return ContextPacket(
        context_packet_id=f"context:{task_id}:reports",
        task_context=TaskContext(
            task_id=task_id,
            trace_id=trace_id,
            role="family",
            purpose="report",
        ),
        evidence_packet=EvidencePacket(
            night_summaries=[summary],
            questionnaire_entries=[questionnaire],
            evidence_ledger=ledger,
        ),
    )
