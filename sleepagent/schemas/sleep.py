from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Sex(str, Enum):
    MALE = "male"
    FEMALE = "female"
    UNKNOWN = "unknown"


class SleepStage(str, Enum):
    WAKE = "Wake"
    REM = "REM"
    NREM = "NREM"


class RespiratoryEventType(str, Enum):
    NORMAL_BREATHING = "normal_breathing"
    HYPOPNEA = "hypopnea"
    SUSPECTED_APNEA = "suspected_apnea"


class RiskLevel(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PatientProfile(StrictSchema):
    subject_id: str = Field(..., min_length=1)
    age_years: int | None = Field(default=None, ge=0, le=120)
    sex: Sex = Sex.UNKNOWN
    notes: list[str] = Field(default_factory=list)


class SignalChannel(StrictSchema):
    name: str = Field(..., min_length=1)
    sampling_rate_hz: float = Field(..., gt=0)
    unit: str | None = None
    source: str | None = None


class SleepRecordMetadata(StrictSchema):
    record_id: str = Field(..., min_length=1)
    source_dataset: str = Field(default="mock", min_length=1)
    patient: PatientProfile
    recording_start: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_seconds: float = Field(..., gt=0)
    channels: list[SignalChannel] = Field(default_factory=list)


class SleepEpoch(StrictSchema):
    start_second: float = Field(..., ge=0)
    duration_seconds: float = Field(default=30.0, gt=0)
    stage: SleepStage
    confidence: float = Field(..., ge=0, le=1)

    @property
    def end_second(self) -> float:
        return self.start_second + self.duration_seconds


class RespiratoryEvent(StrictSchema):
    start_second: float = Field(..., ge=0)
    duration_seconds: float = Field(..., gt=0)
    event_type: RespiratoryEventType
    confidence: float = Field(..., ge=0, le=1)
    oxygen_desaturation_percent: float | None = Field(default=None, ge=0)

    @property
    def end_second(self) -> float:
        return self.start_second + self.duration_seconds


class RespiratoryTrendPoint(StrictSchema):
    second: float = Field(..., ge=0)
    breaths_per_minute: float = Field(..., ge=0, le=80)
    spo2_percent: float | None = Field(default=None, ge=0, le=100)


class SleepStageSummary(StrictSchema):
    total_recording_minutes: float = Field(..., ge=0)
    total_sleep_time_minutes: float = Field(..., ge=0)
    wake_minutes: float = Field(..., ge=0)
    rem_minutes: float = Field(..., ge=0)
    nrem_minutes: float = Field(..., ge=0)
    sleep_efficiency_percent: float = Field(..., ge=0, le=100)


class RespiratorySummary(StrictSchema):
    ahi: float = Field(..., ge=0)
    normal_breathing_count: int = Field(..., ge=0)
    hypopnea_count: int = Field(..., ge=0)
    suspected_apnea_count: int = Field(..., ge=0)
    mean_respiratory_rate_bpm: float | None = Field(default=None, ge=0, le=80)


class SleepStagingMetrics(StrictSchema):
    accuracy: float | None = Field(default=None, ge=0, le=1)
    cohen_kappa: float | None = Field(default=None, ge=-1, le=1)
    macro_f1: float | None = Field(default=None, ge=0, le=1)
    weighted_f1: float | None = Field(default=None, ge=0, le=1)
    per_class_f1: dict[SleepStage, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_per_class_f1(self) -> "SleepStagingMetrics":
        for label, score in self.per_class_f1.items():
            if score < 0 or score > 1:
                raise ValueError(f"F1 for {label.value} must be between 0 and 1.")
        return self


class RespiratoryDetectionMetrics(StrictSchema):
    recall: float | None = Field(default=None, ge=0, le=1)
    auc: float | None = Field(default=None, ge=0, le=1)
    f1: float | None = Field(default=None, ge=0, le=1)
    per_class_recall: dict[RespiratoryEventType, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_per_class_recall(self) -> "RespiratoryDetectionMetrics":
        for label, score in self.per_class_recall.items():
            if score < 0 or score > 1:
                raise ValueError(f"Recall for {label.value} must be between 0 and 1.")
        return self


class SleepAnalysisResult(StrictSchema):
    metadata: SleepRecordMetadata
    epochs: list[SleepEpoch]
    respiratory_events: list[RespiratoryEvent]
    respiratory_trend: list[RespiratoryTrendPoint]
    sleep_summary: SleepStageSummary
    respiratory_summary: RespiratorySummary
    sleep_staging_metrics: SleepStagingMetrics | None = None
    respiratory_detection_metrics: RespiratoryDetectionMetrics | None = None
    risk_level: RiskLevel
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    notes: list[str] = Field(default_factory=list)
