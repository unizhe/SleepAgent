from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from sleepagent.schemas import DialogueContext
from sleepagent.schemas.data_management import StoredAnalysisRecord, StoredReportRecord
from sleepagent.schemas.memory import LongTermMemorySummary
from sleepagent.schemas.sleep import RiskLevel
from sleepagent.services.data_management import SleepDataRepository


DEFAULT_MEMORY_MAX_RECORDS = 5


def compress_long_term_memory(
    analysis_records: list[StoredAnalysisRecord],
    *,
    report_records: list[StoredReportRecord] | None = None,
    max_records: int = DEFAULT_MEMORY_MAX_RECORDS,
    generated_at: datetime | None = None,
) -> LongTermMemorySummary:
    if max_records <= 0:
        raise ValueError("max_records must be greater than 0.")
    if not analysis_records:
        raise ValueError("analysis_records must contain at least one record.")

    recent_analysis_records = _select_recent_analysis_records(
        analysis_records,
        max_records=max_records,
    )
    subject_id = recent_analysis_records[0].subject_id
    if any(record.subject_id != subject_id for record in recent_analysis_records):
        raise ValueError("analysis_records must belong to one subject.")

    report_records_by_analysis_id = _index_report_records_by_analysis_id(
        report_records or [],
        subject_id=subject_id,
    )
    source_report_ids = [
        report_records_by_analysis_id[record.analysis_id].report_id
        for record in recent_analysis_records
        if record.analysis_id in report_records_by_analysis_id
    ]

    ahi_values = [
        record.analysis.respiratory_summary.ahi for record in recent_analysis_records
    ]
    sleep_efficiency_values = [
        record.analysis.sleep_summary.sleep_efficiency_percent
        for record in recent_analysis_records
    ]
    risk_counts = Counter(record.risk_level for record in recent_analysis_records)
    sorted_records = sorted(
        recent_analysis_records,
        key=lambda record: (record.generated_at, record.stored_at),
    )
    latest_record = sorted_records[-1]
    first_record = sorted_records[0]

    average_ahi = _mean(ahi_values)
    average_sleep_efficiency = _mean(sleep_efficiency_values)
    history_summary = _build_history_summary(
        subject_id=subject_id,
        record_count=len(recent_analysis_records),
        latest_record=latest_record,
        average_ahi=average_ahi,
        average_sleep_efficiency=average_sleep_efficiency,
        max_ahi=max(ahi_values),
        risk_counts=risk_counts,
        source_report_count=len(source_report_ids),
    )

    return LongTermMemorySummary(
        subject_id=subject_id,
        generated_at=generated_at or datetime.now(timezone.utc),
        source_analysis_ids=[record.analysis_id for record in recent_analysis_records],
        source_report_ids=source_report_ids,
        record_ids=[record.record_id for record in recent_analysis_records],
        record_count=len(recent_analysis_records),
        first_record_generated_at=first_record.generated_at,
        latest_record_generated_at=latest_record.generated_at,
        latest_risk_level=latest_record.risk_level,
        risk_level_counts=dict(risk_counts),
        average_sleep_efficiency_percent=round(average_sleep_efficiency, 2),
        average_ahi=round(average_ahi, 2),
        max_ahi=round(max(ahi_values), 2),
        latest_ahi=round(latest_record.analysis.respiratory_summary.ahi, 2),
        latest_sleep_efficiency_percent=round(
            latest_record.analysis.sleep_summary.sleep_efficiency_percent,
            2,
        ),
        history_summary=history_summary,
    )


def compress_memory_from_repository(
    repository: SleepDataRepository,
    *,
    subject_id: str,
    max_records: int = DEFAULT_MEMORY_MAX_RECORDS,
    generated_at: datetime | None = None,
) -> LongTermMemorySummary | None:
    analysis_records = repository.list_analysis_records(subject_id=subject_id)
    if not analysis_records:
        return None

    report_records = repository.list_report_records(subject_id=subject_id)
    return compress_long_term_memory(
        analysis_records,
        report_records=report_records,
        max_records=max_records,
        generated_at=generated_at,
    )


def build_dialogue_context_from_memory(
    memory_summary: LongTermMemorySummary,
    *,
    user_preferences: list[str] | None = None,
    recent_questions: list[str] | None = None,
) -> DialogueContext:
    return DialogueContext(
        history_summary=memory_summary.history_summary,
        user_preferences=user_preferences or [],
        recent_questions=recent_questions or [],
    )


def _select_recent_analysis_records(
    analysis_records: list[StoredAnalysisRecord],
    *,
    max_records: int,
) -> list[StoredAnalysisRecord]:
    return sorted(
        analysis_records,
        key=lambda record: (record.generated_at, record.stored_at),
        reverse=True,
    )[:max_records]


def _index_report_records_by_analysis_id(
    report_records: list[StoredReportRecord],
    *,
    subject_id: str,
) -> dict[str, StoredReportRecord]:
    indexed: dict[str, StoredReportRecord] = {}
    for record in sorted(
        report_records,
        key=lambda item: (item.generated_at, item.stored_at),
    ):
        if record.subject_id != subject_id or record.analysis_id is None:
            continue
        indexed[record.analysis_id] = record
    return indexed


def _build_history_summary(
    *,
    subject_id: str,
    record_count: int,
    latest_record: StoredAnalysisRecord,
    average_ahi: float,
    average_sleep_efficiency: float,
    max_ahi: float,
    risk_counts: Counter[RiskLevel],
    source_report_count: int,
) -> str:
    risk_text = "，".join(
        f"{_risk_text(risk_level)}{count}次"
        for risk_level, count in sorted(
            risk_counts.items(),
            key=lambda item: item[0].value,
        )
    )
    report_text = (
        f"已关联{source_report_count}份报告摘要"
        if source_report_count
        else "暂无已关联报告摘要"
    )
    return (
        f"受试者 {subject_id} 最近{record_count}次睡眠记录："
        f"平均AHI约{average_ahi:.1f}，最高AHI约{max_ahi:.1f}，"
        f"平均睡眠效率约{average_sleep_efficiency:.1f}%。"
        f"最近一次记录为{_risk_text(latest_record.risk_level)}，"
        f"AHI约{latest_record.analysis.respiratory_summary.ahi:.1f}，"
        f"睡眠效率约"
        f"{latest_record.analysis.sleep_summary.sleep_efficiency_percent:.1f}%。"
        f"风险分布：{risk_text}。{report_text}。"
        "该长期记忆仅用于对话上下文，不替代医生诊断。"
    )


def _risk_text(risk_level: RiskLevel) -> str:
    if risk_level == RiskLevel.LOW:
        return "低风险"
    if risk_level == RiskLevel.MODERATE:
        return "中等风险"
    return "较高风险"


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)
