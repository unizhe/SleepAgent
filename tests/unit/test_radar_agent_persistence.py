from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from sleepagent.persistence import (
    POSTGRES_BASELINE_SQL,
    RadarAlertRecord,
    RadarAuditLogEntry,
    RadarMemorySummary,
    RadarPersistenceStore,
    RadarReportArtifactVersion,
    RadarSubject,
    RadarUserRoleBinding,
    RawRadarVectorWriteError,
    VectorDocument,
    apply_sqlite_schema,
    split_sql_statements,
    validate_vector_document,
)
from sleepagent.product_runtime.task_runtime import RadarAgentTask, RadarTaskStatus
from sleepagent.product_runtime.schemas import (
    EvidenceClaim,
    EvidenceLedger,
    RadarBedPresence,
    RadarDevice,
    RadarDeviceStatus,
    RadarNightSummary,
    RadarVitalSnapshot,
    ReviewStatus,
    RiskLevel,
    RoleReportArtifact,
)


NOW = datetime(2026, 7, 10, 2, 0, tzinfo=timezone.utc)


def test_canonical_schema_contains_current_persistence_contracts() -> None:
    statements = split_sql_statements()
    required_tables = {
        "radar_subjects",
        "radar_user_roles",
        "radar_tasks",
        "radar_devices",
        "radar_vital_snapshots",
        "radar_night_summaries",
        "radar_evidence_ledgers",
        "radar_evidence_claims",
        "radar_alerts",
        "radar_report_artifacts",
        "radar_report_artifact_versions",
        "radar_audit_logs",
        "radar_memory_summaries",
        "product_habit_profile_states",
        "product_habit_profile_commits",
        "product_care_context_states",
        "product_memory_context_states",
        "product_commit_journal",
        "product_episode_results",
    }

    assert statements
    for table in required_tables:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in POSTGRES_BASELINE_SQL

    assert "source_claim_ids JSONB NOT NULL" in POSTGRES_BASELINE_SQL
    assert "prompt_version TEXT NOT NULL" in POSTGRES_BASELINE_SQL
    assert "model_provider TEXT NOT NULL" in POSTGRES_BASELINE_SQL
    assert "model_id TEXT NOT NULL" in POSTGRES_BASELINE_SQL
    assert "generation_mode TEXT NOT NULL" in POSTGRES_BASELINE_SQL
    assert "UNIQUE(artifact_id, version_number)" in POSTGRES_BASELINE_SQL
    assert "raw_radar" not in POSTGRES_BASELINE_SQL.lower()
    assert "radar_a2a_messages" not in POSTGRES_BASELINE_SQL
    assert "radar_dynamic_goals" not in POSTGRES_BASELINE_SQL


def test_sqlite_schema_applies_current_tables_without_retired_runtime() -> None:
    connection = sqlite3.connect(":memory:")
    apply_sqlite_schema(connection)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }

    assert "radar_tasks" in tables
    assert "radar_report_artifact_versions" in tables
    assert "radar_memory_summaries" in tables
    assert "product_habit_profile_states" in tables
    assert "product_habit_profile_commits" in tables
    assert "product_habit_question_suppressions" in tables
    assert "product_habit_question_episodes" in tables
    assert "product_habit_question_selections" in tables
    assert "product_habit_question_cooldowns" in tables
    assert "product_episode_result_revisions" in tables
    assert "product_induction_manifests" in tables
    assert "product_induction_jobs" in tables
    assert "product_induction_receipts" in tables
    assert "product_episode_digests" in tables
    assert "product_episode_digest_status_events" in tables
    assert "product_memory_read_receipts" in tables
    assert "product_longitudinal_control" in tables
    assert "product_longitudinal_subject_epochs" in tables
    assert "product_offline_skill_outcomes" in tables
    assert "product_human_decisions" in tables
    assert "product_human_decision_events" in tables
    assert "product_pending_habit_change_sets" in tables
    assert connection.execute(
        "SELECT version FROM sleepagent_schema_migrations"
    ).fetchone() == ("001_initial_schema",)
    assert "radar_dynamic_goals" not in tables
    assert "radar_a2a_messages" not in tables


def test_radar_agent_persistence_basic_crud_round_trip() -> None:
    store = RadarPersistenceStore.connect_sqlite(sqlite3.connect(":memory:"))
    subject = RadarSubject(
        subject_id="elder-001",
        display_name="Elder Demo",
        timezone_name="Asia/Shanghai",
        created_at=NOW,
        updated_at=NOW,
    )
    role = RadarUserRoleBinding(
        role_binding_id="role-family-001",
        user_id="family-user-001",
        subject_id=subject.subject_id,
        role="family",
        display_name="Family Demo",
        permissions=["read_report", "confirm_export"],
        created_at=NOW,
        updated_at=NOW,
    )
    task = RadarAgentTask(
        task_id="task-001",
        trace_id="trace-001",
        subject_id=subject.subject_id,
        radar_device_id="radar-device-001",
        role="family",
        scenario="frequent_out_of_bed",
        status=RadarTaskStatus.CREATED,
        created_at=NOW,
        updated_at=NOW,
    )
    device = RadarDevice(
        radar_device_id=task.radar_device_id,
        display_name="Bedroom radar",
        provider="replay",
        status=RadarDeviceStatus.ONLINE,
        bound_subject_id=subject.subject_id,
        registered_at=NOW,
        updated_at=NOW,
    )
    snapshot = RadarVitalSnapshot(
        snapshot_id="snapshot-001",
        radar_device_id=device.radar_device_id,
        subject_id=subject.subject_id,
        measured_at=NOW,
        received_at=NOW,
        heart_rate_bpm=62,
        breath_rate_bpm=15,
        body_movement=0.2,
        bed_presence=RadarBedPresence.IN_BED,
    )
    summary = RadarNightSummary(
        radar_device_id=device.radar_device_id,
        subject_id=subject.subject_id,
        night_of=date(2026, 7, 9),
        total_sleep_minutes=420,
        out_of_bed_count=2,
        movement_count=5,
        data_coverage_ratio=0.91,
        generated_at=NOW,
    )
    claim = EvidenceClaim(
        claim_id="claim-001",
        task_id=task.task_id,
        text="昨夜离床次数较近期基线偏多。",
        evidence_refs=["night-summary:2026-07-09"],
        confidence=0.78,
        risk_level=RiskLevel.WATCH,
        generated_by="trend",
        review_status=ReviewStatus.REVIEWED,
    )
    ledger = EvidenceLedger(
        ledger_id="ledger-001",
        task_id=task.task_id,
        raw_evidence_refs=["raw-replay-001"],
        canonical_evidence_refs=[snapshot.snapshot_id],
        derived_metrics={"out_of_bed_count": summary.out_of_bed_count},
        claims=[claim],
        confidence=0.78,
        review_status=ReviewStatus.REVIEWED,
        updated_at=NOW,
    )
    alert = RadarAlertRecord(
        alert_id="alert-001",
        task_id=task.task_id,
        subject_id=subject.subject_id,
        risk_level=RiskLevel.WATCH,
        title="离床趋势观察",
        message="建议家属关注连续几晚离床变化。",
        evidence_refs=[claim.claim_id],
        created_at=NOW,
    )
    artifact = RoleReportArtifact(
        artifact_id="artifact-family-001",
        task_id=task.task_id,
        role="family",
        title="家属版夜间摘要",
        content="昨夜离床次数偏多，建议继续观察。",
        claim_ids=[claim.claim_id],
        evidence_refs=[claim.claim_id],
        generation_mode="llm",
        generated_at=NOW,
    )
    version_1 = RadarReportArtifactVersion(
        artifact_version_id="artifact-family-001-v1",
        artifact_id=artifact.artifact_id,
        version_number=1,
        content=artifact.content,
        source_claim_ids=[claim.claim_id],
        prompt_version="role_report.v1",
        model_provider="openai-compatible",
        model_id="demo-model",
        generation_mode="llm",
        created_at=NOW,
    )
    version_2 = version_1.model_copy(
        update={
            "artifact_version_id": "artifact-family-001-v2",
            "version_number": 2,
            "content": "昨夜离床次数偏多，建议结合白天状态继续观察。",
        }
    )
    audit = RadarAuditLogEntry(
        audit_id="audit-001",
        task_id=task.task_id,
        actor="orchestrator",
        action="publish_report_artifact",
        target_ref=artifact.artifact_id,
        summary="Published family report artifact.",
        payload={"artifact_version_id": version_1.artifact_version_id},
        created_at=NOW,
    )
    memory = RadarMemorySummary(
        memory_summary_id="memory-001",
        subject_id=subject.subject_id,
        task_id=task.task_id,
        memory_type="trend",
        summary="近 7 天离床次数需要继续观察。",
        evidence_refs=[claim.claim_id],
        generated_at=NOW,
    )

    assert store.save_subject(subject) == subject
    assert store.get_subject(subject.subject_id) == subject
    assert store.save_role_binding(role) == role
    assert store.list_role_bindings(subject.subject_id) == [role]

    assert store.save_task(task) == task
    running_task = task.model_copy(
        update={"status": RadarTaskStatus.RUNNING, "updated_at": NOW}
    )
    store.save_task(running_task)
    assert store.get_task(task.task_id).status == RadarTaskStatus.RUNNING

    assert store.save_device(device) == device
    assert store.get_device(device.radar_device_id) == device
    assert store.save_vital_snapshot(snapshot) == snapshot
    assert store.list_vital_snapshots(device.radar_device_id) == [snapshot]
    assert store.save_night_summary(summary) == summary
    assert store.get_night_summary(device.radar_device_id, summary.night_of) == summary

    assert store.save_evidence_ledger(ledger) == ledger
    assert store.get_evidence_ledger(ledger.ledger_id) == ledger
    assert store.save_alert(alert) == alert
    assert store.list_alerts(subject.subject_id) == [alert]

    assert store.save_report_artifact(artifact, version_1) == artifact
    assert store.get_report_artifact(artifact.artifact_id) == artifact
    assert store.save_report_artifact_version(version_2) == version_2
    versions = store.list_report_artifact_versions(artifact.artifact_id)
    assert [version.version_number for version in versions] == [1, 2]
    assert versions[0].source_claim_ids == [claim.claim_id]
    assert versions[0].prompt_version == "role_report.v1"
    assert versions[0].model_provider == "openai-compatible"
    assert versions[0].model_id == "demo-model"
    assert versions[0].generation_mode == "llm"

    assert store.save_audit_log(audit) == audit
    assert store.list_audit_logs(task.task_id) == [audit]
    assert store.save_memory_summary(memory) == memory
    assert store.list_memory_summaries(subject.subject_id) == [memory]


def test_vector_store_interface_rejects_raw_radar_stream_documents() -> None:
    reviewed_summary = VectorDocument(
        document_id="doc-reviewed-summary",
        collection="reviewed_seed_knowledge",
        text="毫米波雷达摘要只能作为健康观察参考。",
        metadata={"source_type": "reviewed_seed"},
    )
    raw_payload = VectorDocument(
        document_id="doc-raw-payload",
        collection="radar_raw_stream",
        text="raw_payload: {HeartRate: 62}",
        metadata={"raw_payload": {"HeartRate": 62}},
    )

    assert validate_vector_document(reviewed_summary) == reviewed_summary
    with pytest.raises(RawRadarVectorWriteError):
        validate_vector_document(raw_payload)
