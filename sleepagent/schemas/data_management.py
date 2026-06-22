from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sleepagent.schemas.report import MockSleepReport
from sleepagent.schemas.sleep import RiskLevel, SleepAnalysisResult

NonEmptyStr = Annotated[str, Field(min_length=1)]


class StrictDataManagementSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StoredAnalysisRecord(StrictDataManagementSchema):
    schema_version: Literal["stage9.analysis_record.v1"] = (
        "stage9.analysis_record.v1"
    )
    analysis_id: NonEmptyStr
    record_id: NonEmptyStr
    subject_id: NonEmptyStr
    source_dataset: NonEmptyStr
    risk_level: RiskLevel
    generated_at: datetime
    stored_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    analysis: SleepAnalysisResult

    @model_validator(mode="after")
    def validate_analysis_snapshot(self) -> "StoredAnalysisRecord":
        if self.record_id != self.analysis.metadata.record_id:
            raise ValueError("record_id must match analysis.metadata.record_id.")
        if self.subject_id != self.analysis.metadata.patient.subject_id:
            raise ValueError(
                "subject_id must match analysis.metadata.patient.subject_id."
            )
        if self.source_dataset != self.analysis.metadata.source_dataset:
            raise ValueError(
                "source_dataset must match analysis.metadata.source_dataset."
            )
        if self.risk_level != self.analysis.risk_level:
            raise ValueError("risk_level must match analysis.risk_level.")
        if self.generated_at != self.analysis.generated_at:
            raise ValueError("generated_at must match analysis.generated_at.")
        return self


class StoredReportRecord(StrictDataManagementSchema):
    schema_version: Literal["stage9.report_record.v1"] = "stage9.report_record.v1"
    report_id: NonEmptyStr
    analysis_id: str | None = Field(default=None, min_length=1)
    record_id: NonEmptyStr
    subject_id: NonEmptyStr
    risk_level: RiskLevel
    generated_at: datetime
    stored_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    report: MockSleepReport

    @model_validator(mode="after")
    def validate_report_snapshot(self) -> "StoredReportRecord":
        if self.record_id != self.report.summary.record_id:
            raise ValueError("record_id must match report.summary.record_id.")
        if self.subject_id != self.report.summary.subject_id:
            raise ValueError("subject_id must match report.summary.subject_id.")
        if self.risk_level != self.report.summary.risk_level:
            raise ValueError("risk_level must match report.summary.risk_level.")
        if self.generated_at != self.report.generated_at:
            raise ValueError("generated_at must match report.generated_at.")
        return self
