from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from sleepagent.radar_agent.agents import (
    ContextPacket,
    EvidencePacket,
    MemoryAgent,
    RadarDataAgent,
    TaskContext,
)
from sleepagent.radar_agent.memory import (
    MemoryPrivacyFilter,
    MemoryWriteDecision,
    OrchestratedMemoryWriter,
    ShortTermDialogueTurn,
    build_short_term_memory,
)
from sleepagent.radar_agent.orchestrator import FullOrchestratorAgent
from sleepagent.radar_agent.persistence import (
    RadarPersistenceStore,
    RadarSubject,
    RadarUserRoleBinding,
)
from sleepagent.radar_agent.provider import ReplayRadarProvider
from sleepagent.radar_agent.runtime import TaskService
from sleepagent.radar_agent.schemas import (
    EvidenceClaim,
    EvidenceLedger,
    HumanConfirmationRequest,
    MemoryCandidate,
    RadarDataQualityStatus,
    RadarDevice,
    RadarDeviceStatus,
    RadarNightSummary,
    ReviewStatus,
    RiskLevel,
    RoleReportArtifact,
)


NOW = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)


def test_short_term_memory_keeps_current_task_night_dialogue_and_report_only() -> None:
    context = _context()
    report = RoleReportArtifact(
        artifact_id="report:task-memory:family",
        task_id="task-memory",
        role="family",
        title="Family report",
        content="Current task report",
        evidence_refs=["night:001"],
    )
    context.evidence_packet.data_quality["current_report_artifact"] = report.model_dump(
        mode="json"
    )
    context.evidence_packet.data_quality["current_dialogue_turns"] = [
        {
            "role": "user",
            "content": "这段敏感对话只用于当前任务",
            "sensitive": True,
            "consented_for_long_term": False,
        }
    ]

    short_term = build_short_term_memory(context)

    assert short_term.task_id == "task-memory"
    assert short_term.latest_night_summary.night_of == date(2026, 7, 10)
    assert short_term.dialogue_turns[0].sensitive is True
    assert short_term.current_report_artifact == report
    assert short_term.expires_with_task is True
    assert not hasattr(short_term, "approved")


def test_memory_agent_proposes_all_long_term_layers_without_writing() -> None:
    result = MemoryAgent().run(
        _context(
            data_quality={
                "trend_result": _trend_result(),
                "risk_signal_change": "watch_from_info",
                "preference_updates": {
                    "expression_style": "简洁温和",
                    "family_focus": ["夜间离床"],
                    "doctor_report_format": "结构化摘要",
                },
                "care_events": [
                    {"event_type": "watch_reminder", "status": "candidate"},
                    {"event_type": "watch_reminder", "status": "candidate"},
                    {"event_type": "care_plan", "status": "candidate"},
                    {"event_type": "family_viewed", "status": "confirmed"},
                ],
            }
        )
    )
    proposal = result.output_payload["memory_proposal"]
    candidates = proposal["candidates"]

    assert proposal["write_performed"] is False
    assert {item["memory_type"] for item in candidates} == {
        "trend",
        "preference",
        "care_event",
    }
    trend = next(item for item in candidates if item["memory_type"] == "trend")
    assert set(trend["payload"]["windows"]) == {"7", "30", "90"}
    assert trend["payload"]["risk_signal_change"] == "watch_from_info"
    assert all(item["approved"] is False for item in candidates)
    actions = {
        item["action_type"] for item in proposal["confirmation_requests"]
    }
    assert "enable_persistent_family_reminder" in actions
    assert "enable_care_plan" in actions
    assert "write_long_term_memory" in actions
    assert all(
        item["requested_role"] == "family"
        for item in proposal["confirmation_requests"]
    )
    confirmation_ids = [
        item["confirmation_id"] for item in proposal["confirmation_requests"]
    ]
    assert len(confirmation_ids) == len(set(confirmation_ids))


@pytest.mark.parametrize(
    ("payload", "privacy_tags", "reason"),
    [
        ({"windows": {}, "raw_radar_stream": [1, 2]}, [], "forbidden_fields"),
        (
            {"expression_style": "brief"},
            ["sensitive_dialogue"],
            "unauthorized_sensitive_dialogue",
        ),
        (
            {"expression_style": "brief", "medication_free_text": "drug detail"},
            [],
            "forbidden_fields",
        ),
        (
            {"event_type": "care_plan", "status": "candidate", "family_conflict": "text"},
            [],
            "forbidden_fields",
        ),
        (
            {"event_type": "care_plan", "status": "candidate", "financial_info": "text"},
            [],
            "forbidden_fields",
        ),
        (
            {"expression_style": "包含家庭矛盾和财务信息"},
            [],
            "forbidden_content",
        ),
        (
            {"expression_style": "每天服药剂量为 100 毫克"},
            [],
            "forbidden_content",
        ),
    ],
)
def test_privacy_filter_rejects_default_long_term_exclusions(
    payload: dict,
    privacy_tags: list[str],
    reason: str,
) -> None:
    memory_type = "trend" if "windows" in payload else (
        "preference" if "expression_style" in payload else "care_event"
    )
    candidate = _candidate(
        memory_type=memory_type,
        payload=payload,
        privacy_tags=privacy_tags,
    )

    decision = MemoryPrivacyFilter().review(candidate)

    assert decision.allowed is False
    assert decision.sanitized_payload == {}
    assert any(item.startswith(reason) for item in decision.reasons)


def test_orchestrator_requires_matching_approved_family_confirmation() -> None:
    candidate = _candidate(
        memory_type="preference",
        payload={"expression_style": "简洁温和"},
    )
    orchestrator = FullOrchestratorAgent(
        radar_data_agent=RadarDataAgent(ReplayRadarProvider())
    )

    missing = orchestrator.review_memory_write(candidate, None)
    wrong_role = orchestrator.review_memory_write(
        candidate, _confirmation(candidate, requested_role="doctor")
    )
    pending = orchestrator.review_memory_write(
        candidate, _confirmation(candidate, status="pending", resolved_by=None)
    )
    wrong_action = orchestrator.review_memory_write(
        candidate, _confirmation(candidate, action_type="enable_care_plan")
    )
    approved = orchestrator.review_memory_write(candidate, _confirmation(candidate))

    assert missing.approved_for_write is False
    assert "family_confirmation_required" in missing.reasons
    assert "family_confirmation_required" in wrong_role.reasons
    assert "confirmation_not_approved" in pending.reasons
    assert "confirmation_action_mismatch" in wrong_action.reasons
    assert approved.approved_for_write is True
    assert approved.memory.privacy_reviewed is True
    assert approved.memory.payload == {"expression_style": "简洁温和"}
    with pytest.raises(PermissionError):
        OrchestratedMemoryWriter().decide(
            candidate, _confirmation(candidate), actor="memory_agent"
        )


def test_full_orchestrator_routes_trends_preferences_and_care_to_memory_candidates() -> None:
    orchestrator = FullOrchestratorAgent(
        radar_data_agent=RadarDataAgent(ReplayRadarProvider(scenario="normal_night"))
    )
    context = _context(
        data_quality={
            "preference_updates": {"expression_style": "简洁温和"},
            "care_events": [
                {"event_type": "care_plan", "status": "candidate"}
            ],
        }
    )

    decision = orchestrator.run(context)
    memory_result = next(
        item for item in decision.accepted_results if item.agent_name.value == "memory"
    )
    candidates = memory_result.output_payload["memory_proposal"]["candidates"]

    assert {item["memory_type"] for item in candidates} == {
        "trend",
        "preference",
        "care_event",
    }
    trend = next(item for item in candidates if item["memory_type"] == "trend")
    assert set(trend["payload"]["windows"]) == {"7", "30", "90"}
    assert all(item["approved"] is False for item in candidates)
    assert any(
        item.action_type == "enable_care_plan"
        and item.requested_role == "family"
        for item in decision.confirmation_requests
    )
    assert orchestrator.last_short_term_memory is not None
    assert orchestrator.last_short_term_memory.task_id == context.task_context.task_id
    assert orchestrator.last_short_term_memory.current_report_artifact.role == "family"

    orchestrator.answer(context, decision.evidence_ledger)
    assert [
        turn.role for turn in orchestrator.last_short_term_memory.dialogue_turns[-2:]
    ] == ["user", "assistant"]


def test_task_service_persists_only_orchestrator_approved_memory_decision() -> None:
    service, store, task_id = _task_service()
    candidate = _candidate(
        task_id=task_id,
        memory_type="care_event",
        payload={
            "event_type": "family_viewed",
            "status": "confirmed",
            "occurred_at": NOW.isoformat(),
        },
    )
    candidate = candidate.model_copy(update={"subject_id": "elder-001"})
    confirmation = _persist_approved_confirmation(service, task_id, candidate)
    approved = OrchestratedMemoryWriter().decide(
        candidate, confirmation, actor="orchestrator"
    )

    saved = service.persist_memory_decision(task_id, approved)
    again = service.persist_memory_decision(task_id, approved)

    assert saved == again
    assert store.list_memory_summaries("elder-001") == [saved]
    assert saved.confirmation_id is not None
    assert saved.source_candidate_id == candidate.candidate_id
    assert any(
        item.action == "memory_written"
        for item in store.list_audit_logs(task_id)
    )
    rejected = MemoryWriteDecision(
        candidate_id=candidate.candidate_id,
        task_id=task_id,
        reasons=["privacy_rejected"],
    )
    with pytest.raises(PermissionError):
        service.persist_memory_decision(task_id, rejected)


def test_malicious_candidate_never_reaches_long_term_store() -> None:
    service, store, task_id = _task_service()
    candidate = _candidate(
        task_id=task_id,
        memory_type="preference",
        payload={
            "expression_style": "brief",
            "medication_free_text": "unstructured medication details",
        },
    ).model_copy(update={"subject_id": "elder-001"})
    decision = OrchestratedMemoryWriter().decide(
        candidate, _confirmation(candidate), actor="orchestrator"
    )

    assert decision.approved_for_write is False
    with pytest.raises(PermissionError):
        service.persist_memory_decision(task_id, decision)
    assert store.list_memory_summaries("elder-001") == []


def test_task_service_rejects_unpersisted_confirmation_and_tampered_memory() -> None:
    service, _, task_id = _task_service()
    candidate = _candidate(
        task_id=task_id,
        memory_type="preference",
        payload={"expression_style": "brief"},
    ).model_copy(update={"subject_id": "elder-001"})
    unpersisted = OrchestratedMemoryWriter().decide(
        candidate, _confirmation(candidate), actor="orchestrator"
    )
    with pytest.raises(PermissionError, match="confirmation"):
        service.persist_memory_decision(task_id, unpersisted)

    persisted = _persist_approved_confirmation(service, task_id, candidate)
    approved = OrchestratedMemoryWriter().decide(
        candidate, persisted, actor="orchestrator"
    )
    tampered = approved.model_copy(
        update={
            "memory": approved.memory.model_copy(
                update={"summary": "家庭矛盾和财务信息"}
            )
        }
    )
    with pytest.raises(PermissionError, match="does not match"):
        service.persist_memory_decision(task_id, tampered)


def test_raw_evidence_reference_is_not_eligible_for_long_term_memory() -> None:
    candidate = _candidate(
        memory_type="preference",
        payload={"expression_style": "brief"},
    ).model_copy(update={"evidence_refs": ["raw-event:vendor-001"]})

    decision = MemoryPrivacyFilter().review(candidate)

    assert decision.allowed is False
    assert "raw_evidence_refs_not_allowed" in decision.reasons


def test_privacy_filter_rejects_unknown_nested_trend_fields() -> None:
    windows = {
        key: {
            "window_days": int(key),
            "status": "computed",
            "metrics": {"sleep_minutes": 380},
            "evidence_refs": ["night:001"],
        }
        for key in ("7", "30", "90")
    }
    windows["7"]["private_note"] = "not part of the trend schema"
    candidate = _candidate(
        memory_type="trend",
        payload={
            "windows": windows,
            "risk_level": "watch",
            "risk_signal_change": "watch_from_info",
        },
    )

    decision = MemoryPrivacyFilter().review(candidate)

    assert decision.allowed is False
    assert "invalid_structured_payload" in decision.reasons


def _context(*, data_quality: dict | None = None) -> ContextPacket:
    summary = RadarNightSummary(
        radar_device_id="radar-001",
        subject_id="elder-001",
        night_of=date(2026, 7, 10),
        total_sleep_minutes=380,
        out_of_bed_count=4,
        movement_count=20,
        data_coverage_ratio=0.93,
        data_quality_status=RadarDataQualityStatus.GOOD,
        source_report_ref="night:001",
    )
    claim = EvidenceClaim(
        claim_id="claim:001",
        task_id="task-memory",
        text="近几晚夜间离床次数增加。",
        evidence_refs=["night:001"],
        confidence=0.75,
        risk_level=RiskLevel.WATCH,
        caveats=["非诊断。"],
        generated_by="trend",
        review_status=ReviewStatus.REVIEWED,
    )
    ledger = EvidenceLedger(
        ledger_id="ledger:memory",
        task_id="task-memory",
        canonical_evidence_refs=["night:001"],
        derived_metrics={"risk_level": "watch"},
        claims=[claim],
        confidence=0.75,
        review_status=ReviewStatus.REVIEWED,
    )
    return ContextPacket(
        task_context=TaskContext(
            task_id="task-memory",
            trace_id="trace-memory",
            role="family",
            purpose="memory",
        ),
        evidence_packet=EvidencePacket(
            night_summaries=[summary],
            data_quality=data_quality or {},
            evidence_ledger=ledger,
        ),
    )


def _trend_result() -> dict:
    return {
        "windows": {
            key: [
                {
                    "metric_name": "sleep_minutes",
                    "status": "computed",
                    "value": 370 + int(key),
                    "evidence_refs": ["night:001"],
                }
            ]
            for key in ("7", "30", "90")
        }
    }


def _candidate(
    *,
    task_id: str = "task-memory",
    memory_type: str,
    payload: dict,
    privacy_tags: list[str] | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        candidate_id=f"candidate:{task_id}:{memory_type}",
        subject_id="elder-001",
        task_id=task_id,
        memory_type=memory_type,
        summary="Minimized structured summary.",
        payload=payload,
        evidence_refs=["night:001"],
        privacy_tags=privacy_tags or [],
    )


def _confirmation(
    candidate: MemoryCandidate,
    *,
    requested_role: str = "family",
    status: str = "approved",
    action_type: str | None = None,
    resolved_by: str | None = "family-user-001",
) -> HumanConfirmationRequest:
    return HumanConfirmationRequest(
        confirmation_id=f"confirm:{candidate.candidate_id}",
        task_id=candidate.task_id,
        action_type=action_type or candidate.confirmation_action,
        requested_role=requested_role,
        reason="Family approval required.",
        status=status,
        created_at=NOW,
        resolved_at=NOW if status == "approved" else None,
        resolved_by=resolved_by,
    )


def _task_service() -> tuple[TaskService, RadarPersistenceStore, str]:
    store = RadarPersistenceStore.connect_sqlite(sqlite3.connect(":memory:"))
    store.save_subject(RadarSubject(subject_id="elder-001", display_name="Elder"))
    store.save_role_binding(
        RadarUserRoleBinding(
            role_binding_id="family-binding-001",
            user_id="family-user-001",
            subject_id="elder-001",
            role="family",
            display_name="Family",
        )
    )
    store.save_device(
        RadarDevice(
            radar_device_id="radar-001",
            display_name="Radar",
            provider="replay",
            status=RadarDeviceStatus.ONLINE,
            bound_subject_id="elder-001",
        )
    )
    service = TaskService(store, require_authorization=False)
    task = service.create_task(
        subject_id="elder-001",
        radar_device_id="radar-001",
        role="family",
        role_binding_ids=["family-binding-001"],
    )
    return service, store, task.task_id


def _persist_approved_confirmation(
    service: TaskService,
    task_id: str,
    candidate: MemoryCandidate,
) -> HumanConfirmationRequest:
    from sleepagent.radar_agent.runtime import RadarTaskStatus

    service.transition_task(task_id, RadarTaskStatus.RUNNING)
    pending = HumanConfirmationRequest(
        confirmation_id=f"confirm:{candidate.candidate_id}",
        task_id=task_id,
        action_type=candidate.confirmation_action,
        requested_role="family",
        reason="Family approval required.",
        created_at=NOW,
    )
    service.request_confirmation(task_id, pending)
    return service.resolve_confirmation(
        task_id,
        pending.confirmation_id,
        approved=True,
        actor_id="family-user-001",
        actor_role="family",
    )
