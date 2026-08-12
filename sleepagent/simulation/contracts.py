"""Strict contracts for provider-neutral replay world fixtures.

Only external-world facts belong here.  Derived quality/risk decisions, Agent
work products, readiness decisions, and verifier verdicts are intentionally not
represented by these contracts.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated, Literal, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sleepagent.sleep_domain.contracts import (
    AlertLifecycleState,
    AlertSeverity,
    ObservationType,
    SleepObservation,
)


NonEmptyStr = Annotated[str, Field(min_length=1)]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SimulationContract(BaseModel):
    """Immutable, fail-closed base for replay contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def require_aware_datetimes(self) -> "SimulationContract":
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            if isinstance(value, datetime) and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError(f"{field_name} must be timezone-aware")
        return self


class ComponentVersionPin(SimulationContract):
    schema_version: Literal["simulation_component_pin.v1"] = (
        "simulation_component_pin.v1"
    )
    component: Literal["adapter", "schema", "producer", "algorithm", "policy"]
    component_id: NonEmptyStr
    version: NonEmptyStr
    artifact_sha256: Sha256Hex


class ReplayEnvironment(SimulationContract):
    schema_version: Literal["replay_environment.v1"] = "replay_environment.v1"
    seed: int = Field(ge=0, le=2**63 - 1)
    scenario_clock_start: datetime
    timezone_name: NonEmptyStr
    sleep_day_boundary_hour: int = Field(default=12, ge=0, le=23)
    namespace_id: str = Field(pattern=r"^replay:[a-z0-9][a-z0-9._:-]{0,127}$")
    cohort_id: NonEmptyStr
    cohort_configuration_sha256: Sha256Hex
    data_mode: Literal["replay"] = "replay"
    generator_version: Literal["canonical-replay-generator.v1"] = (
        "canonical-replay-generator.v1"
    )
    component_pins: tuple[ComponentVersionPin, ...]

    @model_validator(mode="after")
    def validate_environment(self) -> "ReplayEnvironment":
        try:
            zone = ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone_name must be a valid IANA timezone") from exc
        if self.scenario_clock_start.tzinfo is None:
            raise ValueError("scenario_clock_start must be timezone-aware")
        if self.scenario_clock_start.astimezone(zone).utcoffset() is None:
            raise ValueError("scenario_clock_start cannot be normalized to timezone")
        components = [pin.component for pin in self.component_pins]
        required = {"adapter", "schema", "producer", "algorithm", "policy"}
        if set(components) != required or len(components) != len(required):
            raise ValueError(
                "component_pins requires exactly one adapter/schema/producer/"
                "algorithm/policy pin"
            )
        return self


class SyntheticAuthorization(SimulationContract):
    schema_version: Literal["synthetic_authorization.v1"] = (
        "synthetic_authorization.v1"
    )
    authorization_id: NonEmptyStr
    scopes: tuple[NonEmptyStr, ...]
    authorization_epoch: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def require_scopes(self) -> "SyntheticAuthorization":
        if not self.scopes:
            raise ValueError("synthetic authorization requires at least one scope")
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("synthetic authorization scopes must be unique")
        return self


class SyntheticActor(SimulationContract):
    schema_version: Literal["synthetic_actor.v1"] = "synthetic_actor.v1"
    actor_id: NonEmptyStr
    role: Literal["elder", "family", "doctor"]
    display_name: NonEmptyStr
    synthetic: Literal[True] = True
    authorization: SyntheticAuthorization

    @model_validator(mode="after")
    def require_obvious_synthetic_name(self) -> "SyntheticActor":
        if "synthetic" not in self.display_name.casefold():
            raise ValueError("synthetic actor display_name must say synthetic")
        return self


class SyntheticIdentity(SimulationContract):
    schema_version: Literal["synthetic_identity.v1"] = "synthetic_identity.v1"
    subject_id: NonEmptyStr
    subject_display_name: NonEmptyStr
    synthetic: Literal[True] = True
    actors: tuple[SyntheticActor, ...]
    provider_id: NonEmptyStr
    provider_account_id: NonEmptyStr
    provider_device_id: NonEmptyStr
    device_id: NonEmptyStr
    device_binding_id: NonEmptyStr
    binding_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_identity(self) -> "SyntheticIdentity":
        if "synthetic" not in self.subject_display_name.casefold():
            raise ValueError("synthetic subject_display_name must say synthetic")
        roles = [actor.role for actor in self.actors]
        if sorted(roles) != ["doctor", "elder", "family"]:
            raise ValueError("identity requires exactly elder/family/doctor actors")
        if len({actor.actor_id for actor in self.actors}) != len(self.actors):
            raise ValueError("synthetic actor IDs must be unique")
        return self


class NightRecipe(SimulationContract):
    schema_version: Literal["night_recipe.v1"] = "night_recipe.v1"
    night_index: int = Field(ge=1, le=366)
    sleep_day: date
    bedtime_local: time
    wake_time_local: time
    cadence_minutes: Literal[3] = 3
    heart_rate_center: float = Field(default=62.0, gt=30, lt=180)
    respiratory_rate_center: float = Field(default=14.0, gt=5, lt=60)
    movement_center: float = Field(default=0.18, ge=0, le=10)
    bed_exit_offsets_minutes: tuple[int, ...] = ()
    stage_template: Literal["balanced-adult-v1"] = "balanced-adult-v1"
    include_vendor_profile: bool = True

    @model_validator(mode="after")
    def validate_bed_exit_offsets(self) -> "NightRecipe":
        if len(set(self.bed_exit_offsets_minutes)) != len(
            self.bed_exit_offsets_minutes
        ):
            raise ValueError("bed_exit_offsets_minutes must be unique")
        if any(offset <= 0 for offset in self.bed_exit_offsets_minutes):
            raise ValueError("bed exit offsets must be positive")
        return self


class LowCoverageOverlay(SimulationContract):
    overlay_type: Literal["low_coverage"] = "low_coverage"
    night_index: int = Field(ge=1)
    coverage_fraction: float = Field(gt=0, lt=1)
    target_types: tuple[
        Literal[
            ObservationType.HEART_RATE,
            ObservationType.RESPIRATORY_RATE,
            ObservationType.MOVEMENT,
        ],
        ...,
    ]

    @model_validator(mode="after")
    def require_targets(self) -> "LowCoverageOverlay":
        if not self.target_types or len(self.target_types) != len(set(self.target_types)):
            raise ValueError("low coverage target_types must be non-empty and unique")
        return self


class MissingMetricOverlay(SimulationContract):
    overlay_type: Literal["missing_metric"] = "missing_metric"
    night_index: int = Field(ge=1)
    target_type: Literal[
        ObservationType.HEART_RATE,
        ObservationType.RESPIRATORY_RATE,
        ObservationType.MOVEMENT,
        ObservationType.SLEEP_STAGE_INTERVAL,
    ]
    reason_code: NonEmptyStr = "synthetic_source_missing"


class OfflineOverlay(SimulationContract):
    overlay_type: Literal["offline"] = "offline"
    night_index: int = Field(ge=1)
    start_offset_minutes: int = Field(ge=0)
    duration_minutes: int = Field(gt=0)


class LateReportOverlay(SimulationContract):
    overlay_type: Literal["late_report"] = "late_report"
    night_index: int = Field(ge=1)
    delay_minutes: int = Field(gt=0, le=60 * 24 * 30)
    target_types: tuple[
        Literal[
            ObservationType.SLEEP_STAGE_INTERVAL,
            ObservationType.VENDOR_SLEEP_PROFILE_METRIC,
        ],
        ...,
    ]

    @model_validator(mode="after")
    def require_targets(self) -> "LateReportOverlay":
        if not self.target_types or len(self.target_types) != len(set(self.target_types)):
            raise ValueError("late report target_types must be non-empty and unique")
        return self


class CorrectionOverlay(SimulationContract):
    overlay_type: Literal["correction"] = "correction"
    correction_id: NonEmptyStr
    night_index: int = Field(ge=1)
    metric_name: NonEmptyStr
    corrected_value: float = Field(gt=0)
    unit: NonEmptyStr
    parent_source_revision: NonEmptyStr
    source_revision: NonEmptyStr
    delay_minutes: int = Field(gt=0, le=60 * 24 * 30)

    @model_validator(mode="after")
    def revision_advances(self) -> "CorrectionOverlay":
        if self.parent_source_revision == self.source_revision:
            raise ValueError("correction source revision must advance its parent")
        return self


class VendorAlertOverlay(SimulationContract):
    """One external vendor alert fact; it contains no risk verdict."""

    overlay_type: Literal["vendor_alert"] = "vendor_alert"
    night_index: int = Field(ge=1)
    offset_minutes: int = Field(ge=0)
    alert_code: NonEmptyStr
    severity: AlertSeverity
    lifecycle_state: AlertLifecycleState = AlertLifecycleState.ACTIVE
    vendor_alert_instance_id: NonEmptyStr


ScenarioOverlay: TypeAlias = Annotated[
    LowCoverageOverlay
    | MissingMetricOverlay
    | OfflineOverlay
    | LateReportOverlay
    | CorrectionOverlay
    | VendorAlertOverlay,
    Field(discriminator="overlay_type"),
]


class ReplayScenario(SimulationContract):
    schema_version: Literal["canonical_replay_scenario.v1"] = (
        "canonical_replay_scenario.v1"
    )
    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    description: NonEmptyStr
    environment: ReplayEnvironment
    identity: SyntheticIdentity
    nights: tuple[NightRecipe, ...]
    overlays: tuple[ScenarioOverlay, ...] = ()
    tags: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_scenario(self) -> "ReplayScenario":
        if not self.nights:
            raise ValueError("scenario requires at least one night recipe")
        expected_indices = list(range(1, len(self.nights) + 1))
        actual_indices = [night.night_index for night in self.nights]
        if actual_indices != expected_indices:
            raise ValueError("night indices must be consecutive and ordered from 1")
        dates = [night.sleep_day for night in self.nights]
        if dates != sorted(dates) or len(set(dates)) != len(dates):
            raise ValueError("sleep_day values must be unique and increasing")
        zone = ZoneInfo(self.environment.timezone_name)
        first_bedtime = datetime.combine(
            self.nights[0].sleep_day,
            self.nights[0].bedtime_local,
            zone,
        )
        if first_bedtime < self.environment.scenario_clock_start.astimezone(zone):
            raise ValueError("first night cannot precede scenario_clock_start")
        valid_indices = set(actual_indices)
        if any(overlay.night_index not in valid_indices for overlay in self.overlays):
            raise ValueError("overlay references an unknown night_index")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("scenario tags must be unique")
        corrections = tuple(
            overlay
            for overlay in self.overlays
            if isinstance(overlay, CorrectionOverlay)
        )
        correction_ids = [item.correction_id for item in corrections]
        if len(correction_ids) != len(set(correction_ids)):
            raise ValueError("correction_id must be unique within a scenario")
        revision_keys = [
            (item.night_index, item.metric_name, item.source_revision)
            for item in corrections
        ]
        if len(revision_keys) != len(set(revision_keys)):
            raise ValueError(
                "correction source revision must be unique per night and metric"
            )
        known_revisions: dict[tuple[int, str], set[str]] = {}
        night_by_index = {night.night_index: night for night in self.nights}
        for correction in corrections:
            if (
                correction.metric_name != "vendor_time_in_bed_minutes"
                or not night_by_index[correction.night_index].include_vendor_profile
            ):
                raise ValueError(
                    "correction requires the generated vendor_time_in_bed_minutes "
                    "v1 source"
                )
            key = (correction.night_index, correction.metric_name)
            known = known_revisions.setdefault(key, {"v1"})
            if correction.parent_source_revision not in known:
                raise ValueError(
                    "correction parent_source_revision must reference an earlier "
                    "revision for the same night and metric"
                )
            known.add(correction.source_revision)
        return self


class ScenarioSeedRequest(SimulationContract):
    """Sanitized world input suitable for a future demo control endpoint."""

    schema_version: Literal["scenario_seed_request.v1"] = (
        "scenario_seed_request.v1"
    )
    request_id: NonEmptyStr
    workspace_generation: int = Field(ge=1)
    scenario: ReplayScenario


class GeneratedReplayManifest(SimulationContract):
    schema_version: Literal["generated_replay_manifest.v1"] = (
        "generated_replay_manifest.v1"
    )
    scenario_id: NonEmptyStr
    generator_version: NonEmptyStr
    scenario_sha256: Sha256Hex
    observation_sequence_sha256: Sha256Hex
    observation_count: int = Field(ge=1)
    night_count: int = Field(ge=1)
    counts_by_type: dict[ObservationType, int]
    first_event_at: datetime
    last_event_at: datetime
    data_mode: Literal["replay"] = "replay"
    synthetic_non_release: Literal[True] = True
    correction_lineage: tuple["GeneratedCorrectionLineage", ...] = ()


class GeneratedCorrectionLineage(SimulationContract):
    schema_version: Literal["generated_correction_lineage.v1"] = (
        "generated_correction_lineage.v1"
    )
    correction_id: NonEmptyStr
    observation_id: NonEmptyStr
    parent_observation_id: NonEmptyStr
    parent_source_revision: NonEmptyStr
    source_revision: NonEmptyStr


class GeneratedReplay(SimulationContract):
    schema_version: Literal["generated_replay.v1"] = "generated_replay.v1"
    manifest: GeneratedReplayManifest
    observations: tuple[SleepObservation, ...]

    @model_validator(mode="after")
    def manifest_matches_observations(self) -> "GeneratedReplay":
        if len(self.observations) != self.manifest.observation_count:
            raise ValueError("manifest observation_count does not match observations")
        if any(observation.data_mode.value != "replay" for observation in self.observations):
            raise ValueError("generated replay cannot contain live observations")
        observation_ids = [item.observation_id for item in self.observations]
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("generated replay observation IDs must be unique")
        actual_counts: dict[ObservationType, int] = {}
        for observation in self.observations:
            actual_counts[observation.observation_type] = (
                actual_counts.get(observation.observation_type, 0) + 1
            )
        if actual_counts != self.manifest.counts_by_type:
            raise ValueError("manifest counts_by_type does not match observations")
        known_ids = set(observation_ids)
        lineage_ids = [item.correction_id for item in self.manifest.correction_lineage]
        if len(lineage_ids) != len(set(lineage_ids)):
            raise ValueError("generated correction lineage IDs must be unique")
        if any(
            item.observation_id not in known_ids
            or item.parent_observation_id not in known_ids
            for item in self.manifest.correction_lineage
        ):
            raise ValueError("correction lineage must reference generated observations")
        return self


__all__ = [
    "ComponentVersionPin",
    "CorrectionOverlay",
    "GeneratedCorrectionLineage",
    "GeneratedReplay",
    "GeneratedReplayManifest",
    "LateReportOverlay",
    "LowCoverageOverlay",
    "MissingMetricOverlay",
    "NightRecipe",
    "OfflineOverlay",
    "ReplayEnvironment",
    "ReplayScenario",
    "ScenarioOverlay",
    "ScenarioSeedRequest",
    "SyntheticActor",
    "SyntheticAuthorization",
    "SyntheticIdentity",
    "VendorAlertOverlay",
]
