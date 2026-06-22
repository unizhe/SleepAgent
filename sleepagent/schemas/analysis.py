from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sleepagent.schemas.sleep import SleepAnalysisResult


class StrictAnalysisSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalysisMode(str, Enum):
    MOCK = "mock"
    REAL = "real"


class AnalysisNodeStatus(str, Enum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    SKIPPED_WITH_WARNING = "skipped_with_warning"
    FAILED = "failed"


class AnalysisRequest(StrictAnalysisSchema):
    record_id: str = Field(default="shhs1-200001", min_length=1)
    subject_id: str | None = Field(default=None, min_length=1)
    shhs_root: str | None = Field(default=None, min_length=1)
    edf_path: str | None = Field(default=None, min_length=1)
    nsrr_xml_path: str | None = Field(default=None, min_length=1)
    profusion_xml_path: str | None = Field(default=None, min_length=1)
    eeg_channel: str = Field(default="EEG", min_length=1)
    eog_channel: str | None = Field(default="EOG(L)", min_length=1)
    emg_channel: str | None = Field(default="EMG", min_length=1)
    respiratory_channels: list[str] = Field(default_factory=list)
    use_respiratory_model: bool = False
    respiratory_checkpoint_path: str | None = Field(default=None, min_length=1)
    allow_demo_respiratory_model: bool = False


class AnalysisNodeResult(StrictAnalysisSchema):
    name: str = Field(..., min_length=1)
    status: AnalysisNodeStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    source_paths: dict[str, str] = Field(default_factory=dict)
    source_artifacts: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None


class AnalysisRunResult(StrictAnalysisSchema):
    record_status: AnalysisNodeStatus
    record_result: AnalysisNodeResult
    quality_result: AnalysisNodeResult
    sleep_staging_result: AnalysisNodeResult
    respiratory_result: AnalysisNodeResult
    risk_result: AnalysisNodeResult
    sleep_analysis_result: SleepAnalysisResult | None = None
    caveats: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
