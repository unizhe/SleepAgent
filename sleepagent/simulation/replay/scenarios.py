from __future__ import annotations

import json
from datetime import date, datetime, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field



SCENARIO_CATALOG_PATH = Path(__file__).with_name("scenario_catalog.json")


class ReplayScenarioSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RadarBedPresence(str, Enum):
    IN_BED = "in_bed"
    OUT_OF_BED = "out_of_bed"
    UNKNOWN = "unknown"


class RiskLevel(str, Enum):
    INFO = "info"
    WATCH = "watch"
    ESCALATE = "escalate"
    URGENT_BOUNDARY = "urgent_boundary"
    UNCERTAIN = "uncertain"


class ReplayScenarioSnapshotInput(ReplayScenarioSchema):
    snapshot_id: str = Field(..., min_length=1)
    measured_at: datetime
    received_at: datetime
    heart_rate_bpm: int | None = Field(default=None, ge=0, le=240)
    breath_rate_bpm: int | None = Field(default=None, ge=0, le=80)
    body_movement: float | None = Field(default=None, ge=0)
    bed_presence: RadarBedPresence = RadarBedPresence.UNKNOWN
    data_quality_flags: list[str] = Field(default_factory=list)
    invalid_reading_flags: list[str] = Field(default_factory=list)


class ReplayScenarioNightReportInput(ReplayScenarioSchema):
    night_of: date
    sleep_start_at: datetime | None = None
    sleep_end_at: datetime | None = None
    total_sleep_minutes: float | None = Field(default=None, ge=0)
    sleep_score: float | None = Field(default=None, ge=0, le=100)
    deep_sleep_minutes: float | None = Field(default=None, ge=0)
    light_sleep_minutes: float | None = Field(default=None, ge=0)
    rem_sleep_minutes: float | None = Field(default=None, ge=0)
    awake_minutes: float | None = Field(default=None, ge=0)
    out_of_bed_count: int = Field(default=0, ge=0)
    movement_count: int = Field(default=0, ge=0)
    data_coverage_ratio: float = Field(default=0, ge=0, le=1)
    invalid_reading_count: int = Field(default=0, ge=0)
    missing_intervals: list[str] = Field(default_factory=list)


class ReplayScenarioAlertInput(ReplayScenarioSchema):
    alert_id: str = Field(..., min_length=1)
    alert_type: str = Field(..., min_length=1)
    severity: Literal["info", "warning", "critical", "unknown"] = "info"
    occurred_at: datetime
    resolved_at: datetime | None = None
    title: str | None = None
    message: str | None = None


class ReplayScenarioTrendPoint(ReplayScenarioSchema):
    date: str = Field(..., min_length=1)
    sleep_score: float = Field(..., ge=0, le=100)
    total_sleep_minutes: float = Field(..., ge=0)
    getup_count: int = Field(..., ge=0)


class ReplayScenarioInput(ReplayScenarioSchema):
    subject_id: str = Field(..., min_length=1)
    radar_device_id: str = Field(..., min_length=1)
    timezone_name: str = "UTC"
    now: datetime
    device_status: Literal["online", "offline", "unknown"] = "unknown"
    snapshots: list[ReplayScenarioSnapshotInput] = Field(default_factory=list)
    night_report: ReplayScenarioNightReportInput
    alerts: list[ReplayScenarioAlertInput] = Field(default_factory=list)
    trend_summary: list[ReplayScenarioTrendPoint] = Field(default_factory=list)
    text_input: str | None = None
    anomalies: list[str] = Field(default_factory=list)


class ReplayExpectedDataQuality(ReplayScenarioSchema):
    status: Literal["good", "partial", "unusable", "urgent_text_override"]
    coverage_ratio: float = Field(..., ge=0, le=1)
    device_offline: bool = False
    user_out_of_bed: bool = False
    missing_intervals: int = Field(default=0, ge=0)
    invalid_reading_count: int = Field(default=0, ge=0)
    blocked_reasons: list[str] = Field(default_factory=list)


class ReplayReportExpectations(ReplayScenarioSchema):
    elder: list[str] = Field(default_factory=list)
    family: list[str] = Field(default_factory=list)
    doctor: list[str] = Field(default_factory=list)
    must_include_caveats: list[str] = Field(default_factory=list)


class ReplayScenarioExpected(ReplayScenarioSchema):
    data_quality: ReplayExpectedDataQuality
    risk_level: RiskLevel
    questionnaire_candidates: list[str] = Field(default_factory=list)
    confirmation_candidates: list[str] = Field(default_factory=list)
    report_expectations: ReplayReportExpectations


class ReplayScenario(ReplayScenarioSchema):
    scenario_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    deterministic_input: ReplayScenarioInput
    expected: ReplayScenarioExpected


class ReplayScenarioSummary(ReplayScenarioSchema):
    scenario_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    risk_level: RiskLevel
    data_quality_status: str = Field(..., min_length=1)
    questionnaire_candidate_count: int = Field(default=0, ge=0)
    confirmation_candidate_count: int = Field(default=0, ge=0)


class ReplayScenarioCatalog(ReplayScenarioSchema):
    version: str = Field(..., min_length=1)
    default_scenario_id: str = Field(..., min_length=1)
    scenarios: list[ReplayScenario] = Field(..., min_length=1)


@lru_cache(maxsize=1)
def load_replay_scenario_catalog() -> ReplayScenarioCatalog:
    payload = json.loads(SCENARIO_CATALOG_PATH.read_text(encoding="utf-8"))
    return ReplayScenarioCatalog.model_validate(payload)


def replay_scenario_ids() -> tuple[str, ...]:
    return tuple(item.scenario_id for item in load_replay_scenario_catalog().scenarios)


def default_replay_scenario_id() -> str:
    return load_replay_scenario_catalog().default_scenario_id


def list_replay_scenarios() -> list[ReplayScenarioSummary]:
    return [
        ReplayScenarioSummary(
            scenario_id=item.scenario_id,
            title=item.title,
            description=item.description,
            risk_level=item.expected.risk_level,
            data_quality_status=item.expected.data_quality.status,
            questionnaire_candidate_count=len(item.expected.questionnaire_candidates),
            confirmation_candidate_count=len(item.expected.confirmation_candidates),
        )
        for item in load_replay_scenario_catalog().scenarios
    ]


def get_replay_scenario(scenario_id: str | None = None) -> ReplayScenario:
    resolved = scenario_id or default_replay_scenario_id()
    for item in load_replay_scenario_catalog().scenarios:
        if item.scenario_id == resolved:
            return item
    raise KeyError(f"unknown radar replay scenario: {resolved}")


def scenario_now(scenario: ReplayScenario) -> datetime:
    value = scenario.deterministic_input.now
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "ReplayExpectedDataQuality",
    "ReplayReportExpectations",
    "ReplayScenario",
    "ReplayScenarioAlertInput",
    "ReplayScenarioCatalog",
    "ReplayScenarioExpected",
    "ReplayScenarioInput",
    "ReplayScenarioNightReportInput",
    "ReplayScenarioSnapshotInput",
    "ReplayScenarioSummary",
    "ReplayScenarioTrendPoint",
    "SCENARIO_CATALOG_PATH",
    "default_replay_scenario_id",
    "get_replay_scenario",
    "list_replay_scenarios",
    "load_replay_scenario_catalog",
    "replay_scenario_ids",
    "scenario_now",
]
