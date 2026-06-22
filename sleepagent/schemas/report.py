from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from sleepagent.schemas.sleep import RiskLevel

NonEmptyStr = Annotated[str, Field(min_length=1)]


class StrictReportSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportKnowledgeSourceType(str, Enum):
    INTERNAL_SEED = "internal_seed"
    CLINICAL_GUIDELINE = "clinical_guideline"
    PAPER = "paper"
    LOCAL_POLICY = "local_policy"


class ReportKnowledgeReviewStatus(str, Enum):
    DEV_ONLY = "dev_only"
    REVIEWED = "reviewed"


class ReportSummary(StrictReportSchema):
    record_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    risk_level: RiskLevel
    total_recording_minutes: float = Field(..., ge=0)
    total_sleep_minutes: float = Field(..., ge=0)
    sleep_efficiency_percent: float = Field(..., ge=0, le=100)
    ahi: float = Field(..., ge=0)
    hypopnea_count: int = Field(..., ge=0)
    suspected_apnea_count: int = Field(..., ge=0)
    mean_respiratory_rate_bpm: float | None = Field(default=None, ge=0, le=80)


class MockSleepReport(StrictReportSchema):
    summary: ReportSummary
    elder_report: str = Field(..., min_length=1)
    professional_report: str = Field(..., min_length=1)
    care_suggestions: list[str] = Field(default_factory=list)
    medical_disclaimer: str = Field(..., min_length=1)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LLMReportDraft(StrictReportSchema):
    schema_version: Literal["stage7.llm_report_draft.v1"] = (
        "stage7.llm_report_draft.v1"
    )
    elder_report: str = Field(..., min_length=1)
    professional_report: str = Field(..., min_length=1)
    care_suggestions: list[NonEmptyStr] = Field(default_factory=list)
    safety_warnings: list[NonEmptyStr] = Field(default_factory=list)


class ReportKnowledgeChunk(StrictReportSchema):
    schema_version: str = Field(
        default="stage7.report_knowledge_chunk.v1",
        min_length=1,
    )
    chunk_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    source: str = Field(..., min_length=1)
    source_type: ReportKnowledgeSourceType = ReportKnowledgeSourceType.INTERNAL_SEED
    review_status: ReportKnowledgeReviewStatus = ReportKnowledgeReviewStatus.REVIEWED
    last_reviewed_at: datetime | None = Field(
        default_factory=lambda: datetime(2026, 6, 9, tzinfo=timezone.utc)
    )
    topic_tags: list[NonEmptyStr] = Field(default_factory=list)
    audience_tags: list[NonEmptyStr] = Field(default_factory=list)
    safety_notes: list[NonEmptyStr] = Field(default_factory=list)


class RetrievedReportKnowledgeChunk(StrictReportSchema):
    chunk: ReportKnowledgeChunk
    score: float = Field(..., ge=0)
    matched_terms: list[NonEmptyStr] = Field(default_factory=list)
