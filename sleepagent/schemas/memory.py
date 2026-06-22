from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sleepagent.schemas.sleep import RiskLevel

NonEmptyStr = Annotated[str, Field(min_length=1)]


class StrictMemorySchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LongTermMemorySummary(StrictMemorySchema):
    schema_version: Literal["stage9.long_term_memory_summary.v1"] = (
        "stage9.long_term_memory_summary.v1"
    )
    subject_id: NonEmptyStr
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_analysis_ids: list[NonEmptyStr] = Field(default_factory=list)
    source_report_ids: list[NonEmptyStr] = Field(default_factory=list)
    record_ids: list[NonEmptyStr] = Field(default_factory=list)
    record_count: int = Field(..., ge=1)
    first_record_generated_at: datetime
    latest_record_generated_at: datetime
    latest_risk_level: RiskLevel
    risk_level_counts: dict[RiskLevel, int] = Field(default_factory=dict)
    average_sleep_efficiency_percent: float = Field(..., ge=0, le=100)
    average_ahi: float = Field(..., ge=0)
    max_ahi: float = Field(..., ge=0)
    latest_ahi: float = Field(..., ge=0)
    latest_sleep_efficiency_percent: float = Field(..., ge=0, le=100)
    history_summary: NonEmptyStr

    @model_validator(mode="after")
    def validate_memory_summary(self) -> "LongTermMemorySummary":
        if self.record_count != len(self.record_ids):
            raise ValueError("record_count must match record_ids length.")
        if self.record_count != len(self.source_analysis_ids):
            raise ValueError("record_count must match source_analysis_ids length.")
        if self.first_record_generated_at > self.latest_record_generated_at:
            raise ValueError(
                "first_record_generated_at must not be later than "
                "latest_record_generated_at."
            )
        for risk_level, count in self.risk_level_counts.items():
            if count < 0:
                raise ValueError(
                    f"risk count for {risk_level.value} must be non-negative."
                )
        return self
