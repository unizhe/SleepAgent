from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from sleepagent.schemas.alert import AlertEvent
from sleepagent.schemas.data_management import StoredAnalysisRecord
from sleepagent.schemas.sleep import RiskLevel, SleepAnalysisResult
from sleepagent.services.data_management import build_stored_analysis_record


ALERT_EVENTS_FILENAME = "alert_events.jsonl"
DEFAULT_HIGH_RISK_AHI_THRESHOLD = 15.0
DEFAULT_HIGH_RISK_SUSPECTED_APNEA_THRESHOLD = 20

_SAFE_IDENTIFIER_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


class LocalJsonlAlertEventRepository:
    """Append-only local alert event store for Stage 9 MVP work."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.alert_events_path = self.root_dir / ALERT_EVENTS_FILENAME

    def record_alert_event(self, event: AlertEvent) -> AlertEvent:
        self.alert_events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.alert_events_path.open("a", encoding="utf-8") as file:
            file.write(event.model_dump_json())
            file.write("\n")
        return event

    def list_alert_events(
        self,
        *,
        subject_id: str | None = None,
        record_id: str | None = None,
    ) -> list[AlertEvent]:
        if not self.alert_events_path.exists():
            return []

        events: list[AlertEvent] = []
        for line in self.alert_events_path.read_text(encoding="utf-8").splitlines():
            stripped_line = line.strip()
            if not stripped_line:
                continue
            event = AlertEvent.model_validate_json(stripped_line)
            if subject_id is not None and event.subject_id != subject_id:
                continue
            if record_id is not None and event.record_id != record_id:
                continue
            events.append(event)
        return events


def build_high_risk_alert_event(
    analysis_record: StoredAnalysisRecord,
    *,
    alert_id: str | None = None,
    created_at: datetime | None = None,
) -> AlertEvent | None:
    if analysis_record.risk_level != RiskLevel.HIGH:
        return None

    resolved_created_at = created_at or datetime.now(timezone.utc)
    analysis = analysis_record.analysis
    summary = analysis.respiratory_summary
    trigger_reasons = _build_trigger_reasons(analysis)
    return AlertEvent(
        alert_id=alert_id
        or build_alert_event_id(analysis_record, resolved_created_at),
        subject_id=analysis_record.subject_id,
        record_id=analysis_record.record_id,
        source_analysis_id=analysis_record.analysis_id,
        risk_level=analysis_record.risk_level,
        ahi=summary.ahi,
        hypopnea_count=summary.hypopnea_count,
        suspected_apnea_count=summary.suspected_apnea_count,
        trigger_reasons=trigger_reasons,
        message=_build_alert_message(analysis_record, trigger_reasons),
        created_at=resolved_created_at,
        local_recorded_at=resolved_created_at,
    )


def record_high_risk_alert_if_needed(
    repository: LocalJsonlAlertEventRepository,
    analysis_record: StoredAnalysisRecord,
    *,
    created_at: datetime | None = None,
) -> AlertEvent | None:
    event = build_high_risk_alert_event(
        analysis_record,
        created_at=created_at,
    )
    if event is None:
        return None
    return repository.record_alert_event(event)


def record_high_risk_alert_for_analysis_if_needed(
    repository: LocalJsonlAlertEventRepository,
    analysis: SleepAnalysisResult,
    *,
    analysis_id: str | None = None,
    stored_at: datetime | None = None,
    created_at: datetime | None = None,
) -> AlertEvent | None:
    analysis_record = build_stored_analysis_record(
        analysis,
        analysis_id=analysis_id,
        stored_at=stored_at,
    )
    return record_high_risk_alert_if_needed(
        repository,
        analysis_record,
        created_at=created_at,
    )


def build_alert_event_id(
    analysis_record: StoredAnalysisRecord,
    created_at: datetime,
) -> str:
    timestamp = created_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (
        f"alert-{_safe_identifier(analysis_record.subject_id)}-"
        f"{_safe_identifier(analysis_record.record_id)}-{timestamp}"
    )


def _build_trigger_reasons(analysis: SleepAnalysisResult) -> list[str]:
    summary = analysis.respiratory_summary
    reasons = ["risk_level=high"]
    if summary.ahi >= DEFAULT_HIGH_RISK_AHI_THRESHOLD:
        reasons.append(f"AHI>={DEFAULT_HIGH_RISK_AHI_THRESHOLD:g}")
    if summary.suspected_apnea_count >= DEFAULT_HIGH_RISK_SUSPECTED_APNEA_THRESHOLD:
        reasons.append(
            f"suspected_apnea_count>={DEFAULT_HIGH_RISK_SUSPECTED_APNEA_THRESHOLD}"
        )
    return reasons


def _build_alert_message(
    analysis_record: StoredAnalysisRecord,
    trigger_reasons: list[str],
) -> str:
    summary = analysis_record.analysis.respiratory_summary
    return (
        f"本地高风险睡眠告警：受试者 {analysis_record.subject_id} 的记录 "
        f"{analysis_record.record_id} 风险等级为 high，AHI约{summary.ahi:.1f}，"
        f"疑似呼吸暂停{summary.suspected_apnea_count}次，"
        f"低通气{summary.hypopnea_count}次。触发原因："
        f"{'；'.join(trigger_reasons)}。该事件仅记录在本地，"
        "未发送短信、邮件或应用推送。"
    )


def _safe_identifier(value: str) -> str:
    normalized = _SAFE_IDENTIFIER_PATTERN.sub("-", value.strip()).strip("-")
    return normalized or "unknown"
