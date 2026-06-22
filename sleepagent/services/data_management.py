from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

from sleepagent.schemas.data_management import StoredAnalysisRecord, StoredReportRecord
from sleepagent.schemas.report import MockSleepReport
from sleepagent.schemas.sleep import SleepAnalysisResult


ANALYSIS_RECORDS_FILENAME = "analysis_records.jsonl"
REPORT_RECORDS_FILENAME = "report_records.jsonl"
SLEEPAGENT_DATA_STORE_DIR_ENV = "SLEEPAGENT_DATA_STORE_DIR"

_SAFE_IDENTIFIER_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")
_RecordT = TypeVar("_RecordT", bound=BaseModel)


class SleepDataRepository(Protocol):
    """Persistence boundary for Stage 9 data management.

    The first implementation is local JSONL for deterministic tests and MVP
    demos. A PostgreSQL adapter can implement the same boundary later.
    """

    def save_analysis(
        self,
        analysis: SleepAnalysisResult,
        *,
        analysis_id: str | None = None,
        stored_at: datetime | None = None,
    ) -> StoredAnalysisRecord:
        ...

    def save_report(
        self,
        report: MockSleepReport,
        *,
        report_id: str | None = None,
        analysis_id: str | None = None,
        stored_at: datetime | None = None,
    ) -> StoredReportRecord:
        ...

    def list_analysis_records(
        self,
        *,
        subject_id: str | None = None,
        record_id: str | None = None,
    ) -> list[StoredAnalysisRecord]:
        ...

    def list_report_records(
        self,
        *,
        subject_id: str | None = None,
        record_id: str | None = None,
    ) -> list[StoredReportRecord]:
        ...

    def get_latest_analysis_record(
        self,
        *,
        subject_id: str,
        record_id: str | None = None,
    ) -> StoredAnalysisRecord | None:
        ...

    def get_latest_report_record(
        self,
        *,
        subject_id: str,
        record_id: str | None = None,
    ) -> StoredReportRecord | None:
        ...


class LocalJsonlSleepDataRepository:
    """Append-only local data repository for Stage 9 MVP work."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir)
        self.analysis_records_path = self.root_dir / ANALYSIS_RECORDS_FILENAME
        self.report_records_path = self.root_dir / REPORT_RECORDS_FILENAME

    def save_analysis(
        self,
        analysis: SleepAnalysisResult,
        *,
        analysis_id: str | None = None,
        stored_at: datetime | None = None,
    ) -> StoredAnalysisRecord:
        record = build_stored_analysis_record(
            analysis,
            analysis_id=analysis_id,
            stored_at=stored_at,
        )
        self._append_jsonl_record(self.analysis_records_path, record)
        return record

    def save_report(
        self,
        report: MockSleepReport,
        *,
        report_id: str | None = None,
        analysis_id: str | None = None,
        stored_at: datetime | None = None,
    ) -> StoredReportRecord:
        record = build_stored_report_record(
            report,
            report_id=report_id,
            analysis_id=analysis_id,
            stored_at=stored_at,
        )
        self._append_jsonl_record(self.report_records_path, record)
        return record

    def list_analysis_records(
        self,
        *,
        subject_id: str | None = None,
        record_id: str | None = None,
    ) -> list[StoredAnalysisRecord]:
        records = _read_jsonl_records(
            self.analysis_records_path,
            StoredAnalysisRecord,
        )
        return [
            record
            for record in records
            if _matches_record_filters(
                record,
                subject_id=subject_id,
                record_id=record_id,
            )
        ]

    def list_report_records(
        self,
        *,
        subject_id: str | None = None,
        record_id: str | None = None,
    ) -> list[StoredReportRecord]:
        records = _read_jsonl_records(
            self.report_records_path,
            StoredReportRecord,
        )
        return [
            record
            for record in records
            if _matches_record_filters(
                record,
                subject_id=subject_id,
                record_id=record_id,
            )
        ]

    def get_latest_analysis_record(
        self,
        *,
        subject_id: str,
        record_id: str | None = None,
    ) -> StoredAnalysisRecord | None:
        records = self.list_analysis_records(subject_id=subject_id, record_id=record_id)
        if not records:
            return None
        return max(records, key=lambda record: (record.generated_at, record.stored_at))

    def get_latest_report_record(
        self,
        *,
        subject_id: str,
        record_id: str | None = None,
    ) -> StoredReportRecord | None:
        records = self.list_report_records(subject_id=subject_id, record_id=record_id)
        if not records:
            return None
        return max(records, key=lambda record: (record.generated_at, record.stored_at))

    def _append_jsonl_record(self, path: Path, record: BaseModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(record.model_dump_json())
            file.write("\n")


def build_stored_analysis_record(
    analysis: SleepAnalysisResult,
    *,
    analysis_id: str | None = None,
    stored_at: datetime | None = None,
) -> StoredAnalysisRecord:
    return StoredAnalysisRecord(
        analysis_id=analysis_id or build_analysis_snapshot_id(analysis),
        record_id=analysis.metadata.record_id,
        subject_id=analysis.metadata.patient.subject_id,
        source_dataset=analysis.metadata.source_dataset,
        risk_level=analysis.risk_level,
        generated_at=analysis.generated_at,
        stored_at=stored_at or datetime.now(timezone.utc),
        analysis=analysis,
    )


def build_stored_report_record(
    report: MockSleepReport,
    *,
    report_id: str | None = None,
    analysis_id: str | None = None,
    stored_at: datetime | None = None,
) -> StoredReportRecord:
    return StoredReportRecord(
        report_id=report_id or build_report_snapshot_id(report),
        analysis_id=analysis_id,
        record_id=report.summary.record_id,
        subject_id=report.summary.subject_id,
        risk_level=report.summary.risk_level,
        generated_at=report.generated_at,
        stored_at=stored_at or datetime.now(timezone.utc),
        report=report,
    )


def build_analysis_snapshot_id(analysis: SleepAnalysisResult) -> str:
    return _build_snapshot_id(
        "analysis",
        analysis.metadata.record_id,
        analysis.generated_at,
    )


def build_report_snapshot_id(report: MockSleepReport) -> str:
    return _build_snapshot_id("report", report.summary.record_id, report.generated_at)


def _build_snapshot_id(prefix: str, record_id: str, generated_at: datetime) -> str:
    timestamp = generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{_safe_identifier(record_id)}-{timestamp}"


def _safe_identifier(value: str) -> str:
    normalized = _SAFE_IDENTIFIER_PATTERN.sub("-", value.strip()).strip("-")
    return normalized or "unknown"


def _read_jsonl_records(path: Path, model: type[_RecordT]) -> list[_RecordT]:
    if not path.exists():
        return []

    records: list[_RecordT] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped_line = line.strip()
        if stripped_line:
            records.append(model.model_validate_json(stripped_line))
    return records


def _matches_record_filters(
    record: StoredAnalysisRecord | StoredReportRecord,
    *,
    subject_id: str | None,
    record_id: str | None,
) -> bool:
    if subject_id is not None and record.subject_id != subject_id:
        return False
    if record_id is not None and record.record_id != record_id:
        return False
    return True
