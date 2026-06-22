from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sleepagent.schemas.sleep import RiskLevel

NonEmptyStr = Annotated[str, Field(min_length=1)]


class AlertSeverity(str, Enum):
    HIGH = "high"


class AlertStatus(str, Enum):
    LOCAL_RECORDED = "local_recorded"


class AlertTriggerType(str, Enum):
    HIGH_RISK_SLEEP_FINDING = "high_risk_sleep_finding"


class StrictAlertSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AlertEvent(StrictAlertSchema):
    schema_version: Literal["stage9.alert_event.v1"] = "stage9.alert_event.v1"
    alert_id: NonEmptyStr
    trigger_type: AlertTriggerType = AlertTriggerType.HIGH_RISK_SLEEP_FINDING
    severity: AlertSeverity = AlertSeverity.HIGH
    status: AlertStatus = AlertStatus.LOCAL_RECORDED
    subject_id: NonEmptyStr
    record_id: NonEmptyStr
    source_analysis_id: str | None = Field(default=None, min_length=1)
    risk_level: RiskLevel
    ahi: float = Field(..., ge=0)
    hypopnea_count: int = Field(..., ge=0)
    suspected_apnea_count: int = Field(..., ge=0)
    trigger_reasons: list[NonEmptyStr] = Field(default_factory=list)
    message: NonEmptyStr
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    local_recorded_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    push_channels_attempted: list[NonEmptyStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_local_alert_event(self) -> "AlertEvent":
        if self.risk_level != RiskLevel.HIGH:
            raise ValueError("AlertEvent is only emitted for high risk records.")
        if not self.trigger_reasons:
            raise ValueError("trigger_reasons must contain at least one reason.")
        if self.push_channels_attempted:
            raise ValueError(
                "Stage 9 local alert events must not attempt push channels."
            )
        if self.local_recorded_at < self.created_at:
            raise ValueError("local_recorded_at must not be earlier than created_at.")
        return self
