from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

import pytest

from sleepagent.radar_agent.agents import (
    ContextPacket,
    EvidencePacket,
    ReportAgent,
    TaskContext,
)
from sleepagent.radar_agent.llm import (
    CloudLLMClient,
    CloudLLMConfig,
    CloudLLMPrivacyError,
    ModelInvocationMetadata,
    task_service_llm_audit_sink,
)
from sleepagent.radar_agent.orchestrator import OrchestratorDecision, WorkflowNodeName
from sleepagent.radar_agent.persistence import (
    RadarAlertRecord,
    RadarDataAuthorization,
    RadarMemorySummary,
    RadarPersistenceStore,
    RadarSubject,
    RadarUserRoleBinding,
)
from sleepagent.radar_agent.runtime import (
    AuthorizationRequired,
    RadarTaskStatus,
    RoleAccessDenied,
    TaskService,
)
from sleepagent.radar_agent.schemas import (
    EvidenceClaim,
    EvidenceLedger,
    QuestionnaireEntry,
    RadarDataQualityStatus,
    RadarDevice,
    RadarDeviceStatus,
    RadarNightSummary,
    ReviewStatus,
    RiskLevel,
    SupplementaryDocument,
)


NOW = datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc)
ALL_SCOPES = [
    "process_radar_summary",
    "process_questionnaire",
    "process_supplementary_document",
    "export_data",
    "delete_data",
]


class StaticOrchestrator:
    def __init__(self, decision: OrchestratorDecision | None = None) -> None:
        self.calls = 0
        self.decision = decision

    def run(self, context: ContextPacket) -> OrchestratorDecision:
        self.calls += 1
        return self.decision or OrchestratorDecision(
            task_id=context.task_context.task_id,
            node=WorkflowNodeName.PUBLISH_ARTIFACTS,
        )


class FakeResponse:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "safe response"}}]}


class FakeHTTPClient:
    def post(self, url, *, headers, json):
        return FakeResponse()


def test_task_creation_and_processing_fail_without_authorization_or_role_permission() -> None:
    service, _, bindings = _runtime()
    with pytest.raises(AuthorizationRequired):
        service.create_task(
            subject_id="elder-private",
            radar_device_id="radar-private",
            role="family",
            actor_id="family-user",
            role_binding_ids=[bindings["family"].role_binding_id],
        )
    authorization = _grant(service, bindings["family"], ["process_radar_summary"])
    with pytest.raises(RoleAccessDenied):
        service.create_task(
            subject_id="elder-private",
            radar_device_id="radar-private",
            role="family",
            actor_id="intruder-user",
            role_binding_ids=[bindings["family"].role_binding_id],
            authorization_id=authorization.authorization_id,
        )


@pytest.mark.parametrize(
    ("extra_payload", "missing_scope"),
    [
        ("questionnaire", "process_questionnaire"),
        ("supplementary", "process_supplementary_document"),
    ],
)
def test_questionnaire_and_supplementary_material_need_explicit_scope(
    extra_payload: str,
    missing_scope: str,
) -> None:
    service, _, bindings = _runtime()
    authorization = _grant(service, bindings["family"], ["process_radar_summary"])
    task = _task(service, bindings["family"], authorization)
    context = _context(task.task_id, task.trace_id)
    if extra_payload == "questionnaire":
        context = context.model_copy(
            update={
                "evidence_packet": context.evidence_packet.model_copy(
                    update={"questionnaire_entries": [_questionnaire()]}
                )
            }
        )
    else:
        context = context.model_copy(
            update={
                "evidence_packet": context.evidence_packet.model_copy(
                    update={"supplementary_documents": [_document()]}
                )
            }
        )
    orchestrator = StaticOrchestrator()

    with pytest.raises(AuthorizationRequired, match=missing_scope):
        service.execute(task.task_id, orchestrator, context)

    assert orchestrator.calls == 0
    assert service.get_task(task.task_id).status == RadarTaskStatus.CREATED


def test_revoked_authorization_stops_processing_before_orchestrator() -> None:
    service, _, bindings = _runtime()
    authorization = _grant(service, bindings["family"], ALL_SCOPES)
    task = _task(service, bindings["family"], authorization)
    service.revoke_data_authorization(
        authorization.authorization_id,
        actor_id="family-user",
        role_binding_id=bindings["family"].role_binding_id,
    )
    orchestrator = StaticOrchestrator()

    with pytest.raises(AuthorizationRequired, match="not active"):
        service.execute(
            task.task_id,
            orchestrator,
            _context(task.task_id, task.trace_id),
        )
    assert orchestrator.calls == 0


def test_authorized_export_is_role_scoped_minimized_and_contains_owned_artifacts() -> None:
    service, store, bindings = _runtime()
    authorization = _grant(service, bindings["family"], ALL_SCOPES)
    task = _task(service, bindings["family"], authorization)
    ledger = _ledger(task.task_id)
    service.execute(
        task.task_id,
        StaticOrchestrator(
            OrchestratorDecision(
                task_id=task.task_id,
                node=WorkflowNodeName.PUBLISH_ARTIFACTS,
                evidence_ledger=ledger,
            )
        ),
        _context(
            task.task_id,
            task.trace_id,
            questionnaire=True,
            supplementary=True,
            ledger=ledger,
        ),
    )
    store.save_night_summary(_summary())

    with pytest.raises(RoleAccessDenied):
        service.export_subject_data(
            "elder-private",
            actor_id="doctor-user",
            role_binding_id=bindings["doctor"].role_binding_id,
            authorization_id=authorization.authorization_id,
        )
    exported = service.export_subject_data(
        "elder-private",
        actor_id="family-user",
        role_binding_id=bindings["family"].role_binding_id,
        authorization_id=authorization.authorization_id,
    )
    serialized = json.dumps(exported, ensure_ascii=False)

    assert exported["schema_version"] == "radar-subject-export.v1"
    assert exported["tasks"][0]["task_id"] == task.task_id
    assert exported["night_summaries"][0]["source_report_ref"] == "night:private"
    ledger_payload = next(
        item["evidence_ledger"]
        for item in exported["artifacts"]
        if item["evidence_ledger"] is not None
    )
    assert ledger_payload["questionnaire_entries"][0]["answer"] == "明显"
    assert ledger_payload["supplementary_documents"][0]["document_id"] == "doc-001"
    assert "raw_radar_stream" in exported["excluded"]
    assert "raw_payload" not in serialized
    assert "secret-api-key" not in serialized
    assert "provider-secret-value" not in serialized


def test_role_filtered_report_reads_prevent_cross_role_access() -> None:
    service, _, bindings = _runtime()
    authorization = _grant(service, bindings["family"], ALL_SCOPES)
    task = _task(service, bindings["family"], authorization)
    ledger = _ledger(task.task_id)
    report_context = _context(
        task.task_id,
        task.trace_id,
        questionnaire=True,
        supplementary=True,
        ledger=ledger,
    )
    report_result = ReportAgent().run(report_context)
    service.execute(
        task.task_id,
        StaticOrchestrator(
            OrchestratorDecision(
                task_id=task.task_id,
                node=WorkflowNodeName.PUBLISH_ARTIFACTS,
                accepted_results=[report_result],
                evidence_ledger=ledger,
            )
        ),
        report_context,
    )

    elder_reports = service.list_role_reports(
        "elder-private",
        actor_id="elder-user",
        role_binding_id=bindings["elder"].role_binding_id,
        authorization_id=authorization.authorization_id,
    )
    family_reports = service.list_role_reports(
        "elder-private",
        actor_id="family-user",
        role_binding_id=bindings["family"].role_binding_id,
        authorization_id=authorization.authorization_id,
    )
    doctor_reports = service.list_role_reports(
        "elder-private",
        actor_id="doctor-user",
        role_binding_id=bindings["doctor"].role_binding_id,
        authorization_id=authorization.authorization_id,
    )

    assert {item.report.role for item in elder_reports} == {"elder"}
    assert {item.report.role for item in family_reports} == {"elder", "family"}
    assert {item.report.role for item in doctor_reports} == {"doctor"}
    with pytest.raises(RoleAccessDenied):
        service.list_role_reports(
            "elder-private",
            actor_id="family-user",
            role_binding_id=bindings["doctor"].role_binding_id,
            authorization_id=authorization.authorization_id,
        )


def test_authorized_subject_deletion_cascades_private_data_but_keeps_hashed_receipt() -> None:
    service, store, bindings = _runtime()
    authorization = _grant(service, bindings["family"], ALL_SCOPES)
    task = _task(service, bindings["family"], authorization)
    service.execute(
        task.task_id,
        StaticOrchestrator(
            OrchestratorDecision(
                task_id=task.task_id,
                node=WorkflowNodeName.PUBLISH_ARTIFACTS,
                evidence_ledger=_ledger(task.task_id),
            )
        ),
        _context(task.task_id, task.trace_id, ledger=_ledger(task.task_id)),
    )
    store.save_night_summary(_summary())
    store.save_memory_summary(
        RadarMemorySummary(
            memory_summary_id="memory-private",
            subject_id="elder-private",
            task_id=task.task_id,
            memory_type="trend",
            summary="private trend",
        )
    )
    store.save_alert(
        RadarAlertRecord(
            alert_id="alert-private",
            task_id=task.task_id,
            subject_id="elder-private",
            title="private alert",
            message="private message",
        )
    )

    with pytest.raises(RoleAccessDenied):
        service.delete_subject_data(
            "elder-private",
            actor_id="doctor-user",
            role_binding_id=bindings["doctor"].role_binding_id,
            authorization_id=authorization.authorization_id,
        )
    receipt = service.delete_subject_data(
        "elder-private",
        actor_id="family-user",
        role_binding_id=bindings["family"].role_binding_id,
        authorization_id=authorization.authorization_id,
    )

    assert receipt["subject_ref"].startswith("subject:[hash:")
    assert receipt["deleted"]["tasks"] == 1
    with pytest.raises(KeyError):
        store.get_subject("elder-private")
    with pytest.raises(KeyError):
        store.get_task(task.task_id)
    with pytest.raises(KeyError):
        store.get_device("radar-private")
    with pytest.raises(KeyError):
        store.get_data_authorization(authorization.authorization_id)
    remaining_json = json.dumps(
        [json.loads(row[0]) for row in store._fetchall("SELECT audit_json FROM radar_audit_logs")],
        ensure_ascii=False,
    )
    assert "elder-private" not in remaining_json
    assert "family-user" not in remaining_json


def test_llm_audit_persists_only_allowlisted_metadata_not_secret_or_sensitive_text() -> None:
    service, store, bindings = _runtime()
    authorization = _grant(service, bindings["family"], ALL_SCOPES)
    task = _task(service, bindings["family"], authorization)
    client = CloudLLMClient(
        CloudLLMConfig(
            base_url="https://llm.invalid/v1",
            api_key="secret-api-key",
            model_id="safe-model",
            retry=0,
        ),
        http_client=FakeHTTPClient(),
        audit_sink=task_service_llm_audit_sink(service, task_id=task.task_id),
    )
    sensitive = "老人姓名张三，昨晚自述胸痛，这是敏感原文。"
    client.generate_text(
        messages=[{"role": "user", "content": sensitive}],
        metadata=ModelInvocationMetadata(
            model_id="safe-model",
            prompt_version="privacy-test.v1",
        ),
    )
    logs = store.list_audit_logs(task.task_id)
    serialized = json.dumps([item.model_dump(mode="json") for item in logs], ensure_ascii=False)

    assert "secret-api-key" not in serialized
    assert sensitive not in serialized
    assert "safe response" not in serialized
    assert "messages" not in serialized
    llm_log = next(item for item in logs if item.action == "llm_completed")
    assert set(llm_log.payload) == {
        "trace_id",
        "phase",
        "model_provider",
        "model_id",
        "prompt_version",
        "context_packet_id",
        "output_schema_status",
        "attempt_count",
        "input_summary",
        "output_summary",
        "error_summary",
        "fallback_summary",
        "created_at",
    }
    before = len(logs)
    with pytest.raises(CloudLLMPrivacyError):
        client.generate_text(
            messages=[{"role": "user", "content": "发送 raw_radar_stream 原始数据"}],
            metadata=ModelInvocationMetadata(
                model_id="safe-model",
                prompt_version="privacy-test.v1",
            ),
        )
    assert len(store.list_audit_logs(task.task_id)) == before


def _runtime():
    store = RadarPersistenceStore.connect_sqlite(sqlite3.connect(":memory:"))
    store.save_subject(RadarSubject(subject_id="elder-private", display_name="Private Elder"))
    bindings = {
        "elder": RadarUserRoleBinding(
            role_binding_id="binding-elder-private",
            user_id="elder-user",
            subject_id="elder-private",
            role="elder",
            display_name="Elder",
            permissions=["process_health_data", "read_reports"],
        ),
        "family": RadarUserRoleBinding(
            role_binding_id="binding-family-private",
            user_id="family-user",
            subject_id="elder-private",
            role="family",
            display_name="Family",
            permissions=[
                "manage_authorization",
                "process_health_data",
                "export_data",
                "delete_data",
                "read_reports",
            ],
        ),
        "doctor": RadarUserRoleBinding(
            role_binding_id="binding-doctor-private",
            user_id="doctor-user",
            subject_id="elder-private",
            role="doctor",
            display_name="Doctor",
            permissions=["process_health_data", "read_doctor_material"],
        ),
    }
    for binding in bindings.values():
        store.save_role_binding(binding)
    store.save_device(
        RadarDevice(
            radar_device_id="radar-private",
            display_name="Private Radar",
            provider="replay",
            status=RadarDeviceStatus.ONLINE,
            bound_subject_id="elder-private",
        )
    )
    service = TaskService(store, clock=lambda: NOW)
    return service, store, bindings


def _grant(
    service: TaskService,
    binding: RadarUserRoleBinding,
    scopes: list[str],
) -> RadarDataAuthorization:
    return service.grant_data_authorization(
        subject_id=binding.subject_id,
        actor_id=binding.user_id,
        role_binding_id=binding.role_binding_id,
        scopes=scopes,
    )


def _task(
    service: TaskService,
    binding: RadarUserRoleBinding,
    authorization: RadarDataAuthorization,
):
    return service.create_task(
        subject_id="elder-private",
        radar_device_id="radar-private",
        role=binding.role,
        actor_id=binding.user_id,
        role_binding_ids=[binding.role_binding_id],
        authorization_id=authorization.authorization_id,
        provider_input={
            "raw_payload": {"secret": "provider-secret-value"},
            "api_key": "secret-api-key",
        },
    )


def _context(
    task_id: str,
    trace_id: str,
    *,
    questionnaire: bool = False,
    supplementary: bool = False,
    ledger: EvidenceLedger | None = None,
) -> ContextPacket:
    return ContextPacket(
        task_context=TaskContext(
            task_id=task_id,
            trace_id=trace_id,
            role="family",
            purpose="orchestration",
        ),
        evidence_packet=EvidencePacket(
            questionnaire_entries=[_questionnaire()] if questionnaire else [],
            supplementary_documents=[_document()] if supplementary else [],
            evidence_ledger=ledger,
        ),
    )


def _ledger(task_id: str) -> EvidenceLedger:
    claim = EvidenceClaim(
        claim_id=f"claim:{task_id}",
        task_id=task_id,
        text="近七晚睡眠连续性发生变化。",
        evidence_refs=["night:private", "questionnaire:private"],
        confidence=0.7,
        risk_level=RiskLevel.WATCH,
        caveats=["非诊断观察。"],
        generated_by="trend",
        review_status=ReviewStatus.REVIEWED,
    )
    return EvidenceLedger(
        ledger_id=f"ledger:{task_id}",
        task_id=task_id,
        canonical_evidence_refs=["night:private", "questionnaire:private"],
        derived_metrics={"risk_level": "watch", "data_quality_status": "partial"},
        questionnaire_entries=[_questionnaire()],
        supplementary_documents=[_document()],
        claims=[claim],
        confidence=0.7,
        caveats=["仅供健康观察。"],
        review_status=ReviewStatus.REVIEWED,
    )


def _questionnaire() -> QuestionnaireEntry:
    return QuestionnaireEntry(
        entry_id="questionnaire-private",
        subject_id="elder-private",
        role="family",
        question_id="q-sleepiness",
        answer="明显",
        evidence_ref="questionnaire:private",
    )


def _document() -> SupplementaryDocument:
    return SupplementaryDocument(
        document_id="doc-001",
        subject_id="elder-private",
        document_type="doctor_note",
        title="既往材料",
        summary="敏感补充材料摘要",
        review_status=ReviewStatus.REVIEWED,
        caveats=["由用户授权提供。"],
    )


def _summary() -> RadarNightSummary:
    return RadarNightSummary(
        radar_device_id="radar-private",
        subject_id="elder-private",
        night_of=date(2026, 7, 11),
        data_coverage_ratio=0.8,
        data_quality_status=RadarDataQualityStatus.PARTIAL,
        source_report_ref="night:private",
    )
