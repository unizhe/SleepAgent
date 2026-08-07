from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from sleepagent.product_device.schemas import (
    RadarDevice as ProductRadarDevice,
    RadarVitalSnapshot as ProductRadarVitalSnapshot,
    RawVendorEvent as ProductRawVendorEvent,
)
from sleepagent.radar_agent.schemas import (
    A2AMessage,
    AgentResult,
    ConflictRecord,
    ContextPacket,
    EvidenceClaim,
    EvidenceLedger,
    EvidencePacket,
    HumanConfirmationRequest,
    MemoryCandidate,
    QuestionnaireBank,
    QuestionnaireEntry,
    QuestionnairePolicy,
    RadarAgentName,
    RadarBedPresence,
    RadarDevice,
    RadarDeviceStatus,
    RadarNightSummary,
    RadarRawEvent,
    RadarVitalSnapshot,
    ReviewStatus,
    RiskLevel,
    RoleReportArtifact,
    StandardTerminologyMapping,
    SupplementaryDocument,
    TaskContext,
)


NOW = datetime(2026, 7, 10, 1, 30, tzinfo=timezone.utc)


def test_canonical_radar_schemas_are_independent_from_product_device_aliases() -> None:
    assert RadarRawEvent is not ProductRawVendorEvent
    assert RadarDevice is not ProductRadarDevice
    assert RadarVitalSnapshot is not ProductRadarVitalSnapshot


def test_canonical_snapshot_serializes_with_standard_mapping_fields() -> None:
    snapshot = RadarVitalSnapshot(
        snapshot_id="snapshot-001",
        radar_device_id="radar-device-001",
        subject_id="elder-001",
        measured_at=NOW,
        heart_rate_bpm=62,
        breath_rate_bpm=15,
        body_movement=0.2,
        bed_presence=RadarBedPresence.IN_BED,
        source_raw_event_ids=["raw-001"],
        standard_mappings=StandardTerminologyMapping(
            fhir_resource="Observation",
            fhir_profile="SleepAgentRadarVitalSnapshot",
            loinc_codes=["8867-4"],
            snomed_codes=["364075005"],
            local_codes={"sleepagent": "radar_vital_snapshot"},
        ),
    )

    payload = snapshot.model_dump(mode="json")
    roundtrip = RadarVitalSnapshot.model_validate(payload)

    assert payload["standard_mappings"]["fhir_resource"] == "Observation"
    assert payload["standard_mappings"]["loinc_codes"] == ["8867-4"]
    assert payload["standard_mappings"]["snomed_codes"] == ["364075005"]
    assert roundtrip.measured_at == NOW
    assert roundtrip.bed_presence == RadarBedPresence.IN_BED


def test_all_named_canonical_models_validate_and_serialize() -> None:
    raw = _raw_event()
    device = RadarDevice(
        radar_device_id="radar-device-001",
        display_name="Bedroom radar",
        provider="replay",
        status=RadarDeviceStatus.ONLINE,
        source_raw_event_ids=[raw.raw_event_id],
    )
    snapshot = _snapshot()
    summary = RadarNightSummary(
        radar_device_id=device.radar_device_id,
        subject_id="elder-001",
        night_of=date(2026, 7, 9),
        sleep_start_at=NOW - timedelta(hours=7),
        sleep_end_at=NOW,
        total_sleep_minutes=420,
        sleep_score=82,
        out_of_bed_count=1,
        movement_count=4,
        data_coverage_ratio=0.92,
        source_snapshot_ids=[snapshot.snapshot_id],
        source_raw_event_ids=[raw.raw_event_id],
    )
    questionnaire = QuestionnaireEntry(
        entry_id="questionnaire-entry-001",
        subject_id="elder-001",
        role="family",
        question_id="q-daytime-sleepiness",
        answer="mild",
        evidence_ref="questionnaire:q-daytime-sleepiness",
    )
    bank = QuestionnaireBank(
        bank_id="bank-v1",
        version="2026-07-10",
        questions={"q-daytime-sleepiness": "白天是否明显困倦？"},
        reviewed=True,
    )
    policy = QuestionnairePolicy(
        policy_id="policy-watch-v1",
        version="2026-07-10",
        trigger="watch_risk_signal",
        allowed_question_ids=["q-daytime-sleepiness"],
        applicable_roles=["family"],
    )
    document = SupplementaryDocument(
        document_id="doc-001",
        subject_id="elder-001",
        document_type="prior_psg_summary",
        title="既往 PSG 摘要",
        summary="仅作为补充材料摘要进入证据账本。",
    )
    claim = EvidenceClaim(
        claim_id="claim-001",
        task_id="task-001",
        text="昨夜数据覆盖率足以生成夜间摘要。",
        evidence_refs=[summary.source_report_ref or "night-summary:2026-07-09"],
        confidence=0.9,
        generated_by="radar_data",
        review_status=ReviewStatus.REVIEWED,
    )
    ledger = EvidenceLedger(
        ledger_id="ledger-001",
        task_id="task-001",
        raw_evidence_refs=[raw.raw_event_id],
        canonical_evidence_refs=[snapshot.snapshot_id],
        derived_metrics={"data_coverage_ratio": summary.data_coverage_ratio},
        claims=[claim],
        confidence=0.9,
    )
    message = A2AMessage(
        message_id="a2a-001",
        sender="trend",
        receiver="risk_signal",
        task_id="task-001",
        intent="review_watch_signal",
        evidence_refs=[claim.claim_id],
        confidence=0.8,
        risk_level=RiskLevel.WATCH,
    )
    conflict = ConflictRecord(
        conflict_id="conflict-001",
        task_id="task-001",
        sources=["radar_data", "risk_signal"],
        summary="数据质量和风险线索表达冲突。",
        decision="数据质量优先，风险表达降级为 watch。",
        final_status="downgraded",
        evidence_refs=[claim.claim_id],
    )
    report = RoleReportArtifact(
        artifact_id="report-family-001",
        task_id="task-001",
        role="family",
        title="家属版夜间摘要",
        content="昨夜数据可用于观察，但不是诊断结论。",
        claim_ids=[claim.claim_id],
        evidence_refs=[claim.claim_id],
    )
    confirmation = HumanConfirmationRequest(
        confirmation_id="confirm-001",
        task_id="task-001",
        action_type="export_doctor_report",
        requested_role="family",
        reason="导出医生材料前需要家属确认。",
        evidence_refs=[report.artifact_id],
    )
    memory = MemoryCandidate(
        candidate_id="memory-001",
        subject_id="elder-001",
        task_id="task-001",
        memory_type="trend",
        summary="近 7 天夜间离床次数需继续观察。",
        evidence_refs=[claim.claim_id],
    )
    context = ContextPacket(
        task_context=TaskContext(
            task_id="task-001",
            trace_id="trace-001",
            purpose="analysis",
        ),
        evidence_packet=EvidencePacket(
            evidence_refs=[claim.claim_id],
            vital_snapshots=[snapshot],
            night_summaries=[summary],
            questionnaire_entries=[questionnaire],
            supplementary_documents=[document],
            data_quality={"coverage": summary.data_coverage_ratio},
        ),
        a2a_messages=[message],
    )
    result = AgentResult(
        agent_name=RadarAgentName.RISK_SIGNAL,
        claims=[claim],
        evidence_refs=[claim.claim_id],
        confidence=0.8,
        next_requests=[message],
        output_payload={"risk_level": RiskLevel.WATCH.value},
    )

    instances = [
        raw,
        device,
        snapshot,
        summary,
        questionnaire,
        bank,
        policy,
        document,
        claim,
        ledger,
        message,
        conflict,
        report,
        confirmation,
        memory,
        context,
        result,
    ]

    for item in instances:
        reloaded = type(item).model_validate_json(item.model_dump_json())
        assert reloaded == item


@pytest.mark.parametrize(
    ("model", "kwargs", "message"),
    [
        (
            RadarVitalSnapshot,
            {
                "snapshot_id": "bad-snapshot",
                "radar_device_id": "radar-device-001",
                "measured_at": NOW,
                "heart_rate_bpm": 300,
            },
            "less than or equal to 240",
        ),
        (
            RadarNightSummary,
            {
                "radar_device_id": "radar-device-001",
                "night_of": date(2026, 7, 9),
                "data_coverage_ratio": 1.2,
            },
            "less than or equal to 1",
        ),
        (
            QuestionnaireBank,
            {
                "bank_id": "empty-reviewed-bank",
                "version": "2026-07-10",
                "reviewed": True,
            },
            "require questions",
        ),
        (
            RadarRawEvent,
            {
                "raw_event_id": "raw-invalid-use",
                "provider": "replay",
                "event_type": "VitalSignsDataEvent",
                "data_use": "analysis",
            },
            "Input should be",
        ),
    ],
)
def test_canonical_schema_validation_rejects_invalid_values(model, kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        model(**kwargs)


@pytest.mark.parametrize("purpose", ["analysis", "report", "chat", "risk", "alert"])
def test_business_context_rejects_direct_raw_event_reads(purpose: str) -> None:
    raw_payload = _raw_event().model_dump(mode="json")

    with pytest.raises(ValueError, match="raw vendor payloads"):
        ContextPacket(
            task_context=TaskContext(
                task_id="task-raw-boundary",
                trace_id="trace-raw-boundary",
                purpose=purpose,
            ),
            evidence_packet={
                "evidence_refs": ["raw-001"],
                "data_quality": {"direct_raw_read": raw_payload},
            },
        )


def test_agent_result_and_ledger_reject_direct_raw_payloads_but_allow_refs() -> None:
    raw = _raw_event()

    with pytest.raises(ValueError, match="raw vendor payloads"):
        AgentResult(
            agent_name=RadarAgentName.REPORT,
            output_payload={"direct_raw_read": raw},
        )

    with pytest.raises(ValueError, match="raw vendor payloads"):
        EvidenceLedger(
            ledger_id="ledger-invalid-raw",
            task_id="task-001",
            derived_metrics={"direct_raw_read": raw.model_dump(mode="json")},
        )

    ledger = EvidenceLedger(
        ledger_id="ledger-refs-ok",
        task_id="task-001",
        raw_evidence_refs=[raw.raw_event_id],
        derived_metrics={"data_coverage_ratio": 0.95},
    )

    assert ledger.raw_evidence_refs == [raw.raw_event_id]


def _raw_event() -> RadarRawEvent:
    return RadarRawEvent(
        raw_event_id="raw-001",
        provider="replay",
        event_type="VitalSignsDataEvent",
        received_at=NOW,
        event_timestamp=NOW,
        vendor_device_id="vendor-device-001",
        raw_payload={"type": "VitalSignsDataEvent", "data": {"HeartRate": 62}},
        data_payload={"HeartRate": 62, "BreathRate": 15, "OnBed": 1},
        data_use="replay",
    )


def _snapshot() -> RadarVitalSnapshot:
    return RadarVitalSnapshot(
        snapshot_id="snapshot-001",
        radar_device_id="radar-device-001",
        subject_id="elder-001",
        measured_at=NOW,
        heart_rate_bpm=62,
        breath_rate_bpm=15,
        body_movement=0.2,
        bed_presence=RadarBedPresence.IN_BED,
        source_raw_event_ids=["raw-001"],
    )
