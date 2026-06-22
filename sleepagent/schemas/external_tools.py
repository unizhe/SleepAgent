from datetime import date, datetime, timezone
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

NonEmptyStr = Annotated[str, Field(min_length=1)]


class WeatherCondition(str, Enum):
    CLEAR = "clear"
    CLOUDY = "cloudy"
    RAINY = "rainy"
    COLD = "cold"
    HOT = "hot"


class MealTiming(str, Enum):
    EARLY = "early"
    NORMAL = "normal"
    LATE = "late"
    UNKNOWN = "unknown"


class ActivityLevel(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class StrictExternalToolSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WeatherContext(StrictExternalToolSchema):
    condition: WeatherCondition
    outdoor_temperature_celsius: float = Field(..., ge=-50, le=60)
    indoor_temperature_celsius: float | None = Field(default=None, ge=0, le=40)
    humidity_percent: float = Field(..., ge=0, le=100)
    note: NonEmptyStr


class DietContext(StrictExternalToolSchema):
    last_meal_timing: MealTiming = MealTiming.UNKNOWN
    caffeine_after_noon: bool = False
    alcohol_near_bedtime: bool = False
    heavy_meal_near_bedtime: bool = False
    note: NonEmptyStr


class LifestyleContext(StrictExternalToolSchema):
    activity_level: ActivityLevel
    screen_time_before_bed_minutes: int = Field(..., ge=0, le=600)
    nap_minutes: int = Field(..., ge=0, le=360)
    stress_level_0_10: int = Field(..., ge=0, le=10)
    note: NonEmptyStr


class ExternalToolContext(StrictExternalToolSchema):
    schema_version: Literal["stage9.external_tool_context.v1"] = (
        "stage9.external_tool_context.v1"
    )
    subject_id: NonEmptyStr
    location: NonEmptyStr
    context_date: date
    source: Literal["mock"] = "mock"
    weather: WeatherContext
    diet: DietContext
    lifestyle: LifestyleContext
    summary: NonEmptyStr
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
