from datetime import date, datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from sleepagent.schemas.alert import AlertEvent
from sleepagent.schemas.data_management import StoredAnalysisRecord, StoredReportRecord
from sleepagent.schemas.external_tools import ExternalToolContext
from sleepagent.schemas.memory import LongTermMemorySummary


class StrictStage9Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Stage9MockContextRequest(StrictStage9Schema):
    record_id: str = Field(default="stage9-mock-record", min_length=1)
    subject_id: str = Field(default="stage9-mock-subject", min_length=1)
    duration_hours: float = Field(default=0.5, ge=0.5, le=12.0)
    seed: int = 42
    abnormal_event_rate_per_hour: float = Field(default=6.0, ge=0.0, le=60.0)
    location: str = Field(default="mock-city", min_length=1)
    context_date: date | None = None
    external_context_seed: int = 42
    max_memory_records: int = Field(default=5, ge=1, le=30)


class Stage9MockContextResult(StrictStage9Schema):
    analysis_record: StoredAnalysisRecord
    report_record: StoredReportRecord
    memory_summary: LongTermMemorySummary | None = None
    alert_event: AlertEvent | None = None
    external_context: ExternalToolContext
    local_store_dir: str = Field(..., min_length=1)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
