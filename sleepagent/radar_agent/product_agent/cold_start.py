"""Deterministic, request-scoped cold-start policy.

This module deliberately owns no database, cache, worker, Agent, or release
workflow.  It turns canonical metric-night inputs plus registry-issued
capability receipts into immutable, auditable claim ceilings.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import Enum
from typing import Iterable, Literal, Mapping, Sequence

from pydantic import Field, model_validator

from sleepagent.radar_agent.product_agent.contracts import (
    FrozenContract,
    StrictContract,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.habit_profile import (
    BaselineMaturity,
    ObjectiveBaselineArtifact,
)


COLD_START_POLICY_VERSION = "sleepagent-cold-start.v1"
FIXTURE_BASELINE_POLICY_VERSION = "sleepagent-baseline-fixture-4-15.v1"
PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})

MetricId = Literal[
    "sleep_minutes",
    "in_bed_minutes",
    "out_of_bed_count",
    "movement_count",
    "breath_rate_bpm",
    "heart_rate_bpm",
    "data_coverage_ratio",
]
QualityStatus = Literal["usable", "limited", "unusable"]

REVIEWED_METRICS: tuple[MetricId, ...] = (
    "sleep_minutes",
    "in_bed_minutes",
    "out_of_bed_count",
    "movement_count",
    "breath_rate_bpm",
    "heart_rate_bpm",
    "data_coverage_ratio",
)
TIME_SENSITIVE_METRICS = frozenset({"sleep_minutes", "in_bed_minutes"})
_METRIC_CAPABILITIES: Mapping[MetricId, tuple[str, ...]] = {
    "sleep_minutes": ("adapter.sleep_report",),
    "in_bed_minutes": ("adapter.sleep_report",),
    "out_of_bed_count": ("adapter.sleep_report",),
    "movement_count": ("adapter.sleep_report",),
    "breath_rate_bpm": ("adapter.realtime_vitals",),
    "heart_rate_bpm": ("adapter.realtime_vitals",),
    "data_coverage_ratio": ("adapter.sleep_report",),
}


class ClaimKind(str, Enum):
    GENERAL_KNOWLEDGE = "general_knowledge"
    DESCRIBE_CURRENT_NIGHT = "describe_current_night"
    COMPARE_EXPLICIT_NIGHTS = "compare_explicit_nights"
    CURRENT_NIGHT_VS_ESTABLISHED_BASELINE = (
        "current_night_vs_established_baseline"
    )
    SHORT_WINDOW_PATTERN = "short_window_pattern"
    LONGITUDINAL_TREND = "longitudinal_trend"


class ResponseMode(str, Enum):
    SUPPORTED = "supported"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ClaimCeiling(str, Enum):
    GENERAL_KNOWLEDGE = "general_knowledge"
    SINGLE_NIGHT_DESCRIPTION = "single_night_description"
    SHORT_SERIES_DIFFERENCE = "short_series_difference"
    PROVISIONAL_PATTERN = "provisional_pattern"
    ESTABLISHED_BASELINE = "established_baseline"


CLAIM_CEILING_RANK: Mapping[ClaimCeiling, int] = {
    ClaimCeiling.GENERAL_KNOWLEDGE: 0,
    ClaimCeiling.SINGLE_NIGHT_DESCRIPTION: 1,
    ClaimCeiling.SHORT_SERIES_DIFFERENCE: 2,
    ClaimCeiling.PROVISIONAL_PATTERN: 3,
    ClaimCeiling.ESTABLISHED_BASELINE: 4,
}


class ColdStartReason(str, Enum):
    NO_VALID_NIGHT = "no_valid_night"
    METRIC_MISSING = "metric_missing"
    INSUFFICIENT_METRIC_NIGHTS = "insufficient_metric_nights"
    LIMITED_QUALITY = "limited_quality"
    UNUSABLE_QUALITY = "unusable_quality"
    STALE_EVIDENCE = "stale_evidence"
    INCOMPATIBLE_MEASUREMENT_COHORT = "incompatible_measurement_cohort"
    CAPABILITY_NOT_PRODUCTION_ELIGIBLE = (
        "capability_not_production_eligible"
    )
    AUTHORIZATION_MISSING = "authorization_missing"
    BASELINE_POLICY_UNAVAILABLE = "baseline_policy_unavailable"
    BASELINE_UNAVAILABLE = "baseline_unavailable"
    BASELINE_STALE = "baseline_stale"
    BASELINE_SOURCE_INVALID = "baseline_source_invalid"
    BASELINE_POLICY_INCOMPATIBLE = "baseline_policy_incompatible"
    INVALID_SCOPE = "invalid_scope"
    PROFILE_UNKNOWN = "profile_unknown"
    PROFILE_STALE = "profile_stale"
    PROFILE_DISPUTED = "profile_disputed"


class ProfileFieldState(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    STALE = "stale"
    DISPUTED = "disputed"


class ProfileFieldReadiness(FrozenContract):
    concept_id: str = Field(..., min_length=1)
    state: ProfileFieldState
    source_ref: str | None = None

    @model_validator(mode="after")
    def known_field_requires_source(self) -> "ProfileFieldReadiness":
        if self.state == ProfileFieldState.KNOWN and not self.source_ref:
            raise ValueError("known Profile field requires a confirmed source")
        if self.state == ProfileFieldState.UNKNOWN and self.source_ref:
            raise ValueError("unknown Profile field cannot claim a source")
        return self


class MeasurementCohort(FrozenContract):
    """Exact measurement generation; equality is intentionally conservative."""

    subject_id: str = Field(..., min_length=1)
    metric_id: MetricId
    data_mode: Literal["live", "replay"]
    device_binding_version: str = Field(..., min_length=1)
    device_measurement_domain: str = Field(..., min_length=1)
    adapter_id: str = Field(..., min_length=1)
    adapter_version: str = Field(..., min_length=1)
    adapter_configuration_fingerprint: str = Field(
        ..., min_length=64, max_length=64
    )
    observation_schema_version: str = Field(..., min_length=1)
    canonical_data_version: str = Field(..., min_length=1)
    producer_id: str = Field(..., min_length=1)
    producer_version: str = Field(..., min_length=1)
    firmware_version: str | None = None
    calibration_state: Literal[
        "known_calibrated",
        "known_uncalibrated",
        "unknown",
        "not_provided",
    ] = "not_provided"
    calibration_version: str | None = None
    timezone_name: str | None = None
    sleep_day_policy_version: str | None = None
    boundary_policy_version: str | None = None

    @model_validator(mode="after")
    def require_time_and_calibration_semantics(self) -> "MeasurementCohort":
        if self.metric_id in TIME_SENSITIVE_METRICS and not all(
            (
                self.timezone_name,
                self.sleep_day_policy_version,
                self.boundary_policy_version,
            )
        ):
            raise ValueError(
                "time-sensitive metric cohort requires timezone and sleep-day "
                "boundary policy versions"
            )
        if (
            self.calibration_state == "known_calibrated"
            and not self.calibration_version
        ):
            raise ValueError("calibrated cohort requires calibration_version")
        if (
            self.calibration_state
            in {"unknown", "not_provided", "known_uncalibrated"}
            and self.calibration_version
        ):
            raise ValueError(
                "unknown or uncalibrated cohort cannot claim a calibration version"
            )
        return self

    @property
    def cohort_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json"))

    @property
    def cohort_ref(self) -> str:
        return f"measurement-cohort:{self.cohort_hash}"


class MeasurementCompatibilityReceipt(FrozenContract):
    receipt_id: str = Field(..., min_length=1)
    left_cohort_hash: str = Field(..., min_length=64, max_length=64)
    right_cohort_hash: str = Field(..., min_length=64, max_length=64)
    subject_id: str = Field(..., min_length=1)
    metric_id: MetricId
    data_mode: Literal["live", "replay"]
    reviewed_by_actor_id: str = Field(..., min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    reviewed_at: datetime


def cohorts_compatible(
    left: MeasurementCohort,
    right: MeasurementCohort,
    *,
    compatibility: MeasurementCompatibilityReceipt | None = None,
) -> bool:
    if left.cohort_hash == right.cohort_hash:
        return True
    if (
        left.subject_id != right.subject_id
        or left.metric_id != right.metric_id
        or left.data_mode != right.data_mode
    ):
        return False
    if compatibility is None:
        return False
    if (
        compatibility.subject_id != left.subject_id
        or compatibility.metric_id != left.metric_id
        or compatibility.data_mode != left.data_mode
    ):
        return False
    if (
        left.calibration_state in {"unknown", "not_provided"}
        or right.calibration_state in {"unknown", "not_provided"}
    ):
        return False
    return {
        compatibility.left_cohort_hash,
        compatibility.right_cohort_hash,
    } == {left.cohort_hash, right.cohort_hash}


class CapabilityEligibilityReceipt(FrozenContract):
    """Eligibility verdict issued by an owning registry, not by a model."""

    receipt_id: str = Field(..., min_length=1)
    registry_kind: Literal["adapter", "tool", "skill"]
    registry_snapshot_ref: str = Field(..., min_length=1)
    registry_snapshot_hash: str = Field(..., min_length=64, max_length=64)
    capability_id: str = Field(..., min_length=1)
    capability_version: str = Field(..., min_length=1)
    environment: str = Field(..., min_length=1)
    configuration_or_content_hash: str = Field(
        ..., min_length=64, max_length=64
    )
    eligible: bool
    reason_code: str = Field(..., min_length=1)
    authoritative_refs: tuple[str, ...] = Field(min_length=1)
    resolved_at: datetime
    receipt_hash: str = Field(..., min_length=64, max_length=64)

    @classmethod
    def create(cls, **values: object) -> "CapabilityEligibilityReceipt":
        material = dict(values)
        material.pop("receipt_hash", None)
        unsigned = cls(receipt_hash="0" * 64, **material)
        return unsigned.model_copy(
            update={
                "receipt_hash": stable_hash(
                    unsigned.model_dump(
                        mode="json",
                        exclude={"receipt_hash"},
                    )
                )
            }
        )

    @model_validator(mode="after")
    def hash_is_authentic(self) -> "CapabilityEligibilityReceipt":
        if self.receipt_hash != "0" * 64:
            material = self.model_dump(mode="json", exclude={"receipt_hash"})
            if self.receipt_hash != stable_hash(material):
                raise ValueError("capability eligibility receipt hash mismatch")
        if self.eligible and self.registry_kind == "adapter":
            refs = " ".join(self.authoritative_refs).lower()
            if not all(
                marker in refs
                for marker in ("resolution", "verification", "deployment")
            ):
                raise ValueError(
                    "eligible Adapter receipt requires resolution, verification, "
                    "and deployment authority refs"
                )
        if (
            self.eligible
            and self.registry_kind == "skill"
            and not any(
                "release" in ref.lower()
                for ref in self.authoritative_refs
            )
        ):
            raise ValueError(
                "eligible Skill receipt requires an authoritative release ref"
            )
        return self

    @property
    def receipt_ref(self) -> str:
        return f"capability-eligibility:{self.receipt_id}:{self.receipt_hash}"


class ClaimRequirement(FrozenContract):
    requirement_id: str = Field(..., min_length=1)
    catalog_version: Literal["sleepagent-claim-catalog.v1"] = (
        "sleepagent-claim-catalog.v1"
    )
    claim_kind: ClaimKind
    metric_id: MetricId | None = None
    required_profile_concepts: tuple[str, ...] = ()
    required_capability_ids: tuple[str, ...] = ()
    fixed_dates: tuple[date, ...] = ()
    window_start: date | None = None
    window_end: date | None = None

    @model_validator(mode="after")
    def scope_is_registered_and_complete(self) -> "ClaimRequirement":
        if self.claim_kind == ClaimKind.GENERAL_KNOWLEDGE:
            if self.metric_id is not None:
                raise ValueError("general knowledge cannot bind a personal metric")
            return self
        if self.metric_id is None:
            raise ValueError("personal requirement requires a reviewed metric")
        if bool(self.window_start) != bool(self.window_end):
            raise ValueError("fixed window requires both boundaries")
        if self.window_start and self.window_start > self.window_end:
            raise ValueError("fixed window is reversed")
        if self.fixed_dates and (self.window_start or self.window_end):
            raise ValueError("use explicit dates or one fixed window, not both")
        if (
            self.claim_kind == ClaimKind.COMPARE_EXPLICIT_NIGHTS
            and len(self.fixed_dates) < 2
            and not (self.window_start and self.window_end)
        ):
            raise ValueError("explicit-night comparison requires at least two dates")
        return self


def claim_requirement_id(claim_kind: ClaimKind | str, metric_id: MetricId) -> str:
    kind = ClaimKind(claim_kind)
    if kind == ClaimKind.GENERAL_KNOWLEDGE:
        raise ValueError("general knowledge does not use a metric requirement")
    if metric_id not in REVIEWED_METRICS:
        raise ValueError("metric is not in the reviewed allowlist")
    return f"{kind.value}:{metric_id}"


def resolve_claim_requirement(
    requirement_id: str,
    *,
    explicit_dates: Sequence[date] = (),
    fixed_window: tuple[date, date] | None = None,
) -> ClaimRequirement:
    """Resolve a finite catalog ID; values never select dates or thresholds."""

    if requirement_id == "general_knowledge":
        if explicit_dates or fixed_window:
            raise ValueError("general knowledge cannot claim a personal scope")
        return ClaimRequirement(
            requirement_id=requirement_id,
            claim_kind=ClaimKind.GENERAL_KNOWLEDGE,
        )
    try:
        raw_kind, raw_metric = requirement_id.split(":", 1)
        kind = ClaimKind(raw_kind)
    except (ValueError, KeyError) as exc:
        raise ValueError("unknown ClaimRequirement") from exc
    if kind == ClaimKind.GENERAL_KNOWLEDGE or raw_metric not in REVIEWED_METRICS:
        raise ValueError("unknown ClaimRequirement")
    metric_id = raw_metric
    dates = tuple(sorted(set(explicit_dates)))
    window_start, window_end = fixed_window or (None, None)
    if kind == ClaimKind.COMPARE_EXPLICIT_NIGHTS and not (
        len(dates) >= 2 or fixed_window is not None
    ):
        raise ValueError(
            "compare_explicit_nights requires user dates or a registered full window"
        )
    return ClaimRequirement(
        requirement_id=requirement_id,
        claim_kind=kind,
        metric_id=metric_id,
        required_capability_ids=_METRIC_CAPABILITIES[metric_id],
        fixed_dates=dates,
        window_start=window_start,
        window_end=window_end,
    )


class BaselinePolicy(FrozenContract):
    metric_id: MetricId
    environment: str = Field(..., min_length=1)
    policy_version: str = Field(..., min_length=1)
    reviewed: bool
    fixture_only: bool = False
    scope_samples: Mapping[str, int]
    provisional_nights: int = Field(..., ge=4)
    established_nights: int = Field(..., ge=4)
    rolling_window_days: int = Field(..., ge=1)
    minimum_coverage_ratio: float = Field(..., ge=0, le=1)
    allowed_quality: tuple[QualityStatus, ...]
    freshness_days: int = Field(..., ge=1)
    timezone_rule: str = Field(..., min_length=1)
    sleep_day_rule: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def thresholds_are_ordered(self) -> "BaselinePolicy":
        if self.established_nights < self.provisional_nights:
            raise ValueError("established threshold cannot be below provisional")
        if not self.allowed_quality:
            raise ValueError("BaselinePolicy requires allowed quality states")
        expected = {
            item.value
            for item in ClaimKind
            if item != ClaimKind.GENERAL_KNOWLEDGE
        }
        if set(self.scope_samples) != expected:
            raise ValueError(
                "BaselinePolicy scope_samples must use the finite claim catalog"
            )
        if any(value < 1 for value in self.scope_samples.values()):
            raise ValueError("BaselinePolicy scope sample counts must be positive")
        return self


_FIXTURE_POLICIES: Mapping[MetricId, BaselinePolicy] = {
    metric: BaselinePolicy(
        metric_id=metric,
        environment="test",
        policy_version=FIXTURE_BASELINE_POLICY_VERSION,
        reviewed=True,
        fixture_only=True,
        scope_samples={
            ClaimKind.DESCRIBE_CURRENT_NIGHT.value: 1,
            ClaimKind.COMPARE_EXPLICIT_NIGHTS.value: 2,
            ClaimKind.CURRENT_NIGHT_VS_ESTABLISHED_BASELINE.value: 1,
            ClaimKind.SHORT_WINDOW_PATTERN.value: 4,
            ClaimKind.LONGITUDINAL_TREND.value: 15,
        },
        provisional_nights=4,
        established_nights=15,
        rolling_window_days=30,
        minimum_coverage_ratio=0.70,
        allowed_quality=("usable", "limited"),
        freshness_days=14,
        timezone_rule="iana-exact",
        sleep_day_rule="wake-date.v1",
    )
    for metric in REVIEWED_METRICS
}


def load_baseline_policy(
    metric_id: str,
    *,
    environment: str,
) -> BaselinePolicy | None:
    if metric_id not in REVIEWED_METRICS:
        return None
    if environment.lower() in PRODUCTION_ENVIRONMENTS:
        return None
    policy = _FIXTURE_POLICIES[metric_id]
    return policy.model_copy(update={"environment": environment})


class CanonicalMetricNight(FrozenContract):
    """Privacy-minimized canonical input for one metric on one sleep day."""

    night_episode_id: str = Field(..., min_length=1)
    night_episode_revision_id: str = Field(..., min_length=1)
    revision_number: int = Field(..., ge=1)
    subject_id: str = Field(..., min_length=1)
    metric_id: MetricId
    local_sleep_date: date
    cohort: MeasurementCohort
    night_eligible: bool
    metric_present: bool
    coverage_ratio: float = Field(..., ge=0, le=1)
    quality_status: QualityStatus
    blocking_flags: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = Field(min_length=1)
    observed_at: datetime

    @model_validator(mode="after")
    def identity_matches_cohort(self) -> "CanonicalMetricNight":
        if (
            self.subject_id != self.cohort.subject_id
            or self.metric_id != self.cohort.metric_id
        ):
            raise ValueError("metric night identity does not match cohort")
        return self


class MetricNightCount(FrozenContract):
    metric_id: MetricId
    cohort_ref: str
    measurement_cohort: MeasurementCohort
    valid_nights: tuple[CanonicalMetricNight, ...]
    source_refs: tuple[str, ...]
    source_hash: str = Field(..., min_length=64, max_length=64)
    compatibility_refs: tuple[str, ...] = ()
    reason_codes: tuple[ColdStartReason, ...] = ()

    @classmethod
    def create(cls, **values: object) -> "MetricNightCount":
        material = dict(values)
        material.pop("source_hash", None)
        unsigned = cls(source_hash="0" * 64, **material)
        return unsigned.model_copy(
            update={
                "source_hash": stable_hash(
                    unsigned.model_dump(
                        mode="json",
                        exclude={"source_hash"},
                    )
                )
            }
        )

    @model_validator(mode="after")
    def bind_server_derived_evidence(self) -> "MetricNightCount":
        if (
            self.cohort_ref != self.measurement_cohort.cohort_ref
            or self.metric_id != self.measurement_cohort.metric_id
        ):
            raise ValueError("metric count identity does not match cohort")
        dates = [item.local_sleep_date for item in self.valid_nights]
        if len(dates) != len(set(dates)):
            raise ValueError("metric count contains a duplicate sleep day")
        if any(
            item.subject_id != self.measurement_cohort.subject_id
            or item.metric_id != self.metric_id
            for item in self.valid_nights
        ):
            raise ValueError("metric count contains another subject or metric")
        expected_refs = tuple(
            dict.fromkeys(
                ref
                for item in self.valid_nights
                for ref in item.source_refs
            )
        )
        if self.source_refs != expected_refs:
            raise ValueError("metric count source refs do not match valid nights")
        if self.source_hash != "0" * 64:
            material = self.model_dump(mode="json", exclude={"source_hash"})
            if self.source_hash != stable_hash(material):
                raise ValueError("metric count source hash mismatch")
        return self

    @property
    def count(self) -> int:
        return len(self.valid_nights)


def derive_metric_valid_nights(
    nights: Iterable[CanonicalMetricNight],
    *,
    metric_id: MetricId,
    cohort: MeasurementCohort,
    policy: BaselinePolicy | None,
    date_start: date | None = None,
    date_end: date | None = None,
    selected_dates: Sequence[date] = (),
    compatibility_receipts: Sequence[MeasurementCompatibilityReceipt] = (),
    as_of: datetime | None = None,
) -> MetricNightCount:
    """Derive the authoritative count from canonical inputs.

    The newest revision wins before eligibility checks.  Selection uses only
    explicit dates or the complete requested window, never metric values.
    """

    selected = frozenset(selected_dates)
    if selected and (date_start is not None or date_end is not None):
        raise ValueError("use explicit dates or one complete window, not both")
    if bool(date_start) != bool(date_end):
        raise ValueError("complete window requires both date boundaries")
    if date_start and date_end and date_start > date_end:
        raise ValueError("complete window is reversed")
    if cohort.metric_id != metric_id:
        raise ValueError("requested metric does not match measurement cohort")
    if policy is not None and policy.metric_id != metric_id:
        raise ValueError("BaselinePolicy metric does not match requested metric")
    compatibility_by_pair = {
        frozenset(
            (item.left_cohort_hash, item.right_cohort_hash)
        ): item
        for item in compatibility_receipts
    }
    newest_by_sleep_day: dict[date, CanonicalMetricNight] = {}
    reasons: set[ColdStartReason] = set()
    used_compatibility_refs: list[str] = []
    for night in nights:
        if night.subject_id != cohort.subject_id or night.metric_id != metric_id:
            continue
        if selected and night.local_sleep_date not in selected:
            continue
        if date_start and night.local_sleep_date < date_start:
            continue
        if date_end and night.local_sleep_date > date_end:
            continue
        compatibility = compatibility_by_pair.get(
            frozenset((night.cohort.cohort_hash, cohort.cohort_hash))
        )
        if not cohorts_compatible(
            night.cohort,
            cohort,
            compatibility=compatibility,
        ):
            reasons.add(ColdStartReason.INCOMPATIBLE_MEASUREMENT_COHORT)
            continue
        if (
            night.cohort.cohort_hash != cohort.cohort_hash
            and compatibility is not None
        ):
            used_compatibility_refs.extend(compatibility.evidence_refs)
        prior = newest_by_sleep_day.get(night.local_sleep_date)
        if prior is None or (
            night.revision_number,
            night.night_episode_revision_id,
        ) > (
            prior.revision_number,
            prior.night_episode_revision_id,
        ):
            newest_by_sleep_day[night.local_sleep_date] = night

    valid: list[CanonicalMetricNight] = []
    source_refs: list[str] = []
    for night in sorted(
        newest_by_sleep_day.values(),
        key=lambda item: item.local_sleep_date,
    ):
        if not night.night_eligible:
            continue
        if not night.metric_present:
            reasons.add(ColdStartReason.METRIC_MISSING)
            continue
        if night.blocking_flags:
            reasons.add(ColdStartReason.UNUSABLE_QUALITY)
            continue
        if policy is None:
            minimum_coverage = 0.01
            allowed_quality: tuple[QualityStatus, ...] = ("usable", "limited")
        else:
            minimum_coverage = policy.minimum_coverage_ratio
            allowed_quality = policy.allowed_quality
        if night.coverage_ratio < minimum_coverage:
            reasons.add(ColdStartReason.LIMITED_QUALITY)
            continue
        if night.quality_status not in allowed_quality:
            reasons.add(
                ColdStartReason.UNUSABLE_QUALITY
                if night.quality_status == "unusable"
                else ColdStartReason.LIMITED_QUALITY
            )
            continue
        if (
            as_of is not None
            and policy is not None
            and night.local_sleep_date
            < as_of.date() - timedelta(days=policy.freshness_days)
        ):
            reasons.add(ColdStartReason.STALE_EVIDENCE)
            continue
        valid.append(night)
        source_refs.extend(night.source_refs)

    if not valid:
        reasons.add(ColdStartReason.NO_VALID_NIGHT)
    unique_refs = tuple(dict.fromkeys(source_refs))
    return MetricNightCount.create(
        metric_id=metric_id,
        cohort_ref=cohort.cohort_ref,
        measurement_cohort=cohort,
        valid_nights=tuple(valid),
        source_refs=unique_refs,
        compatibility_refs=tuple(dict.fromkeys(used_compatibility_refs)),
        reason_codes=tuple(sorted(reasons, key=lambda item: item.value)),
    )


class BaselineReadinessProjection(FrozenContract):
    artifact_ref: str | None = None
    artifact_hash: str | None = Field(default=None, min_length=64, max_length=64)
    artifact_maturity: BaselineMaturity = BaselineMaturity.UNAVAILABLE
    current_maturity: BaselineMaturity = BaselineMaturity.UNAVAILABLE
    use_eligible: bool = False
    valid_night_count: int = Field(default=0, ge=0)
    measurement_cohort_ref: str
    policy_version: str | None = None
    source_refs: tuple[str, ...] = ()
    source_hash: str = Field(..., min_length=64, max_length=64)
    reason_codes: tuple[ColdStartReason, ...] = ()

    @classmethod
    def create(cls, **values: object) -> "BaselineReadinessProjection":
        material = dict(values)
        material.pop("source_hash", None)
        unsigned = cls(source_hash="0" * 64, **material)
        return unsigned.model_copy(
            update={
                "source_hash": stable_hash(
                    unsigned.model_dump(
                        mode="json",
                        exclude={"source_hash"},
                    )
                )
            }
        )

    @model_validator(mode="after")
    def bind_projection(self) -> "BaselineReadinessProjection":
        if bool(self.artifact_ref) != bool(self.artifact_hash):
            raise ValueError("baseline artifact ref/hash must be paired")
        if self.use_eligible and (
            self.artifact_maturity != BaselineMaturity.ESTABLISHED
            or self.current_maturity != BaselineMaturity.ESTABLISHED
            or not self.artifact_ref
        ):
            raise ValueError(
                "eligible baseline requires established artifact and projection"
            )
        if self.source_hash != "0" * 64:
            material = self.model_dump(mode="json", exclude={"source_hash"})
            if self.source_hash != stable_hash(material):
                raise ValueError("baseline projection source hash mismatch")
        return self


def project_baseline_readiness(
    *,
    artifact: ObjectiveBaselineArtifact | None,
    metric_nights: MetricNightCount,
    cohort: MeasurementCohort,
    policy: BaselinePolicy | None,
    authorization_valid: bool = True,
    sources_valid: bool = True,
    as_of: datetime | None = None,
) -> BaselineReadinessProjection:
    metric_nights = MetricNightCount.model_validate(
        metric_nights.model_dump(mode="json")
    )
    if cohort.cohort_ref != metric_nights.cohort_ref:
        raise ValueError("baseline evidence does not match measurement cohort")
    if policy is not None and policy.metric_id != cohort.metric_id:
        raise ValueError("BaselinePolicy does not match measurement cohort")
    reasons: set[ColdStartReason] = set(metric_nights.reason_codes)
    bounded_nights = metric_nights.valid_nights
    if policy is not None and bounded_nights:
        anchor = (
            as_of.date()
            if as_of is not None
            else max(item.local_sleep_date for item in bounded_nights)
        )
        window_start = anchor - timedelta(
            days=policy.rolling_window_days - 1
        )
        bounded_nights = tuple(
            item
            for item in bounded_nights
            if window_start <= item.local_sleep_date <= anchor
        )
        if len(bounded_nights) < metric_nights.count:
            reasons.add(ColdStartReason.STALE_EVIDENCE)
    count = len(bounded_nights)
    current_source_refs = tuple(
        dict.fromkeys(
            ref
            for item in bounded_nights
            for ref in item.source_refs
        )
    )
    current = BaselineMaturity.UNAVAILABLE
    if policy is None or not policy.reviewed:
        reasons.add(ColdStartReason.BASELINE_POLICY_UNAVAILABLE)
    elif policy.fixture_only and policy.environment.lower() in PRODUCTION_ENVIRONMENTS:
        reasons.add(ColdStartReason.BASELINE_POLICY_UNAVAILABLE)
    elif count >= policy.established_nights:
        current = BaselineMaturity.ESTABLISHED
    elif count >= max(4, policy.provisional_nights):
        current = BaselineMaturity.PROVISIONAL
    else:
        reasons.add(ColdStartReason.INSUFFICIENT_METRIC_NIGHTS)

    if not authorization_valid or not sources_valid:
        current = BaselineMaturity.UNAVAILABLE
        reasons.add(ColdStartReason.BASELINE_SOURCE_INVALID)

    artifact_ref = None
    artifact_hash = None
    artifact_maturity = BaselineMaturity.UNAVAILABLE
    if artifact is not None:
        artifact_ref = f"objective-baseline:{artifact.artifact_id}"
        artifact_hash = stable_hash(artifact.model_dump(mode="json"))
        artifact_maturity = artifact.maturity
        if artifact.subject_id != cohort.subject_id or artifact.metric_id not in {
            cohort.metric_id,
            f"baseline.{cohort.metric_id}",
        }:
            current = BaselineMaturity.UNAVAILABLE
            reasons.add(ColdStartReason.INCOMPATIBLE_MEASUREMENT_COHORT)
        artifact_cohort = getattr(artifact, "measurement_cohort_ref", None)
        if artifact_cohort != cohort.cohort_ref:
            current = BaselineMaturity.UNAVAILABLE
            reasons.add(ColdStartReason.INCOMPATIBLE_MEASUREMENT_COHORT)
        artifact_policy = getattr(artifact, "policy_version", None)
        if policy and artifact_policy != policy.policy_version:
            current = BaselineMaturity.UNAVAILABLE
            reasons.add(ColdStartReason.BASELINE_POLICY_INCOMPATIBLE)

    use_eligible = (
        artifact is not None
        and artifact.maturity == BaselineMaturity.ESTABLISHED
        and current == BaselineMaturity.ESTABLISHED
        and authorization_valid
        and sources_valid
    )
    if not use_eligible:
        reasons.add(ColdStartReason.BASELINE_UNAVAILABLE)
    baseline_source_refs = tuple(
        dict.fromkeys(
            (
                *(artifact.source_refs if artifact is not None else ()),
                *current_source_refs,
            )
        )
    )
    return BaselineReadinessProjection.create(
        artifact_ref=artifact_ref,
        artifact_hash=artifact_hash,
        artifact_maturity=artifact_maturity,
        current_maturity=current,
        use_eligible=use_eligible,
        valid_night_count=count,
        measurement_cohort_ref=cohort.cohort_ref,
        policy_version=policy.policy_version if policy else None,
        source_refs=baseline_source_refs,
        reason_codes=tuple(sorted(reasons, key=lambda item: item.value)),
    )


class MetricReadinessDecision(FrozenContract):
    decision_id: str = Field(..., min_length=1)
    decision_hash: str = Field(..., min_length=64, max_length=64)
    policy_version: Literal["sleepagent-cold-start.v1"] = (
        COLD_START_POLICY_VERSION
    )
    requirement_id: str = Field(..., min_length=1)
    claim_kind: ClaimKind
    metric_id: MetricId | None = None
    measurement_cohort_ref: str | None = None
    scope_date_start: date | None = None
    scope_date_end: date | None = None
    scope_valid_night_count: int = Field(default=0, ge=0)
    scope_source_refs: tuple[str, ...] = ()
    scope_source_hash: str = Field(..., min_length=64, max_length=64)
    baseline_valid_night_count: int = Field(default=0, ge=0)
    baseline_artifact_ref: str | None = None
    baseline_maturity: BaselineMaturity = BaselineMaturity.UNAVAILABLE
    baseline_use_eligible: bool = False
    baseline_source_refs: tuple[str, ...] = ()
    baseline_source_hash: str = Field(..., min_length=64, max_length=64)
    claim_ceiling: ClaimCeiling
    response_mode: ResponseMode
    reason_codes: tuple[ColdStartReason, ...] = ()
    capability_receipt_refs: tuple[str, ...] = ()

    @classmethod
    def create(cls, **values: object) -> "MetricReadinessDecision":
        material = dict(values)
        material.pop("decision_hash", None)
        unsigned = cls(decision_hash="0" * 64, **material)
        return unsigned.model_copy(
            update={
                "decision_hash": stable_hash(
                    unsigned.model_dump(
                        mode="json",
                        exclude={"decision_hash"},
                    )
                )
            }
        )

    @model_validator(mode="after")
    def hash_and_identity_are_consistent(self) -> "MetricReadinessDecision":
        if self.decision_hash != "0" * 64:
            material = self.model_dump(mode="json", exclude={"decision_hash"})
            if self.decision_hash != stable_hash(material):
                raise ValueError("readiness decision hash mismatch")
        if self.claim_kind != ClaimKind.GENERAL_KNOWLEDGE:
            if self.metric_id is None:
                raise ValueError("personal decision requires a metric")
            if self.measurement_cohort_ref is None and not (
                self.scope_valid_night_count == 0
                and self.baseline_valid_night_count == 0
                and not self.scope_source_refs
                and not self.baseline_source_refs
                and self.baseline_artifact_ref is None
                and not self.baseline_use_eligible
                and self.claim_ceiling == ClaimCeiling.GENERAL_KNOWLEDGE
                and self.response_mode
                in {ResponseMode.DEGRADED, ResponseMode.BLOCKED}
                and (
                    ColdStartReason.NO_VALID_NIGHT in self.reason_codes
                    or ColdStartReason.INCOMPATIBLE_MEASUREMENT_COHORT
                    in self.reason_codes
                )
            ):
                raise ValueError(
                    "personal decision without an exact cohort must fail closed"
                )
        return self

    @property
    def decision_ref(self) -> str:
        return f"cold-start-decision:{self.decision_id}:{self.decision_hash}"


def evaluate_readiness(
    *,
    decision_id: str,
    requirement: ClaimRequirement,
    scope_nights: MetricNightCount | None,
    baseline: BaselineReadinessProjection | None,
    policy: BaselinePolicy | None,
    capability_receipts: Sequence[CapabilityEligibilityReceipt] = (),
    authorization_valid: bool = True,
    environment: str | None = None,
    profile_fields: Sequence[ProfileFieldReadiness] = (),
) -> MetricReadinessDecision:
    capability_receipts = tuple(
        CapabilityEligibilityReceipt.model_validate(
            item.model_dump(mode="json")
        )
        for item in capability_receipts
    )
    canonical_requirement = resolve_claim_requirement(
        requirement.requirement_id,
        explicit_dates=requirement.fixed_dates,
        fixed_window=(
            (requirement.window_start, requirement.window_end)
            if requirement.window_start and requirement.window_end
            else None
        ),
    )
    if requirement != canonical_requirement:
        raise ValueError("ClaimRequirement does not match the runtime catalog")
    if requirement.claim_kind == ClaimKind.GENERAL_KNOWLEDGE:
        empty_hash = stable_hash(())
        return MetricReadinessDecision.create(
            decision_id=decision_id,
            requirement_id=requirement.requirement_id,
            claim_kind=requirement.claim_kind,
            scope_source_hash=empty_hash,
            baseline_source_hash=empty_hash,
            claim_ceiling=ClaimCeiling.GENERAL_KNOWLEDGE,
            response_mode=ResponseMode.SUPPORTED,
        )
    if scope_nights is not None:
        scope_nights = MetricNightCount.model_validate(
            scope_nights.model_dump(mode="json")
        )
    if baseline is not None:
        baseline = BaselineReadinessProjection.model_validate(
            baseline.model_dump(mode="json")
        )
    if scope_nights is None or requirement.metric_id != scope_nights.metric_id:
        raise ValueError("requirement metric does not match scoped evidence")
    scoped_dates = {
        item.local_sleep_date for item in scope_nights.valid_nights
    }
    if requirement.fixed_dates and not scoped_dates.issubset(
        set(requirement.fixed_dates)
    ):
        raise ValueError("scoped evidence expands explicit comparison dates")
    if requirement.window_start and any(
        item < requirement.window_start or item > requirement.window_end
        for item in scoped_dates
    ):
        raise ValueError("scoped evidence expands the registered fixed window")

    reasons: set[ColdStartReason] = set(scope_nights.reason_codes)
    profile_by_concept = {
        item.concept_id: item for item in profile_fields
    }
    profile_ready = True
    for concept_id in requirement.required_profile_concepts:
        field = profile_by_concept.get(concept_id)
        state = field.state if field else ProfileFieldState.UNKNOWN
        if state == ProfileFieldState.KNOWN:
            continue
        profile_ready = False
        reasons.add(
            {
                ProfileFieldState.UNKNOWN: ColdStartReason.PROFILE_UNKNOWN,
                ProfileFieldState.STALE: ColdStartReason.PROFILE_STALE,
                ProfileFieldState.DISPUTED: ColdStartReason.PROFILE_DISPUTED,
            }[state]
        )
    capability_refs = tuple(item.receipt_ref for item in capability_receipts)
    expected_environment = environment or (
        policy.environment if policy is not None else None
    )
    eligible_capability_ids: set[str] = set()
    for item in capability_receipts:
        exact_environment = (
            expected_environment is not None
            and item.environment == expected_environment
        )
        exact_adapter_generation = (
            item.registry_kind != "adapter"
            or (
                item.capability_version
                == scope_nights.measurement_cohort.adapter_version
                and item.configuration_or_content_hash
                == scope_nights.measurement_cohort.adapter_configuration_fingerprint
            )
        )
        if item.eligible and exact_environment and exact_adapter_generation:
            eligible_capability_ids.add(item.capability_id)
    capability_ok = set(requirement.required_capability_ids).issubset(
        eligible_capability_ids
    ) and all(item.eligible for item in capability_receipts)
    if not capability_ok and (
        requirement.required_capability_ids or capability_receipts
    ):
        reasons.add(ColdStartReason.CAPABILITY_NOT_PRODUCTION_ELIGIBLE)
    if not authorization_valid:
        reasons.add(ColdStartReason.AUTHORIZATION_MISSING)

    count = scope_nights.count
    baseline_count = baseline.valid_night_count if baseline else 0
    baseline_maturity = (
        baseline.current_maturity
        if baseline
        else BaselineMaturity.UNAVAILABLE
    )
    baseline_usable = bool(baseline and baseline.use_eligible)
    if policy is None:
        reasons.add(ColdStartReason.BASELINE_POLICY_UNAVAILABLE)

    if count == 0:
        ceiling = ClaimCeiling.GENERAL_KNOWLEDGE
    elif count == 1:
        ceiling = ClaimCeiling.SINGLE_NIGHT_DESCRIPTION
    elif count < 4:
        ceiling = ClaimCeiling.SHORT_SERIES_DIFFERENCE
    elif baseline_maturity in {
        BaselineMaturity.PROVISIONAL,
        BaselineMaturity.ESTABLISHED,
    }:
        ceiling = ClaimCeiling.PROVISIONAL_PATTERN
    else:
        ceiling = ClaimCeiling.SHORT_SERIES_DIFFERENCE

    if (
        baseline_usable
        and requirement.claim_kind
        == ClaimKind.CURRENT_NIGHT_VS_ESTABLISHED_BASELINE
        and count >= 1
    ):
        ceiling = ClaimCeiling.ESTABLISHED_BASELINE
    elif (
        baseline_usable
        and requirement.claim_kind == ClaimKind.LONGITUDINAL_TREND
        and policy is not None
        and count
        >= policy.scope_samples[ClaimKind.LONGITUDINAL_TREND.value]
    ):
        ceiling = ClaimCeiling.ESTABLISHED_BASELINE

    required_ceiling = {
        ClaimKind.DESCRIBE_CURRENT_NIGHT: ClaimCeiling.SINGLE_NIGHT_DESCRIPTION,
        ClaimKind.COMPARE_EXPLICIT_NIGHTS: ClaimCeiling.SHORT_SERIES_DIFFERENCE,
        ClaimKind.CURRENT_NIGHT_VS_ESTABLISHED_BASELINE: (
            ClaimCeiling.ESTABLISHED_BASELINE
        ),
        ClaimKind.SHORT_WINDOW_PATTERN: ClaimCeiling.PROVISIONAL_PATTERN,
        ClaimKind.LONGITUDINAL_TREND: ClaimCeiling.ESTABLISHED_BASELINE,
    }[requirement.claim_kind]
    if not authorization_valid:
        mode = ResponseMode.BLOCKED
        ceiling = ClaimCeiling.GENERAL_KNOWLEDGE
    elif not profile_ready:
        mode = ResponseMode.DEGRADED
    elif not capability_ok and (
        requirement.required_capability_ids or capability_receipts
    ):
        mode = ResponseMode.DEGRADED
        ceiling = ClaimCeiling.GENERAL_KNOWLEDGE
    elif CLAIM_CEILING_RANK[ceiling] >= CLAIM_CEILING_RANK[required_ceiling]:
        mode = ResponseMode.SUPPORTED
    else:
        mode = ResponseMode.DEGRADED
        reasons.add(ColdStartReason.INSUFFICIENT_METRIC_NIGHTS)

    dates = [item.local_sleep_date for item in scope_nights.valid_nights]
    baseline_hash = baseline.source_hash if baseline else stable_hash(())
    return MetricReadinessDecision.create(
        decision_id=decision_id,
        requirement_id=requirement.requirement_id,
        claim_kind=requirement.claim_kind,
        metric_id=requirement.metric_id,
        measurement_cohort_ref=scope_nights.cohort_ref,
        scope_date_start=min(dates) if dates else None,
        scope_date_end=max(dates) if dates else None,
        scope_valid_night_count=count,
        scope_source_refs=scope_nights.source_refs,
        scope_source_hash=scope_nights.source_hash,
        baseline_valid_night_count=baseline_count,
        baseline_artifact_ref=baseline.artifact_ref if baseline else None,
        baseline_maturity=baseline_maturity,
        baseline_use_eligible=baseline_usable,
        baseline_source_refs=baseline.source_refs if baseline else (),
        baseline_source_hash=baseline_hash,
        claim_ceiling=ceiling,
        response_mode=mode,
        reason_codes=tuple(sorted(reasons, key=lambda item: item.value)),
        capability_receipt_refs=capability_refs,
    )


def build_unavailable_entry_decisions(
    *,
    decision_namespace: str,
    claim_kind: ClaimKind,
    metric_ids: Sequence[MetricId] = REVIEWED_METRICS,
    authorization_valid: bool = True,
) -> tuple[MetricReadinessDecision, ...]:
    """Fail closed when a legacy entry cannot prove an exact metric cohort.

    The helper deliberately does not synthesize a cohort or a capability
    receipt. It gives migrated entries typed decisions immediately; a
    canonical integration replaces them once exact source and owning-Registry
    receipts are available.
    """

    if claim_kind == ClaimKind.GENERAL_KNOWLEDGE:
        requirement = resolve_claim_requirement("general_knowledge")
        return (
            evaluate_readiness(
                decision_id=f"{decision_namespace}:general-knowledge",
                requirement=requirement,
                scope_nights=None,
                baseline=None,
                policy=None,
                authorization_valid=authorization_valid,
            ),
        )
    decisions: list[MetricReadinessDecision] = []
    for metric_id in tuple(metric_ids):
        requirement = resolve_claim_requirement(
            claim_requirement_id(claim_kind, metric_id)
        )
        reasons = {
            ColdStartReason.NO_VALID_NIGHT,
            ColdStartReason.INCOMPATIBLE_MEASUREMENT_COHORT,
            ColdStartReason.CAPABILITY_NOT_PRODUCTION_ELIGIBLE,
        }
        if claim_kind in {
            ClaimKind.CURRENT_NIGHT_VS_ESTABLISHED_BASELINE,
            ClaimKind.SHORT_WINDOW_PATTERN,
            ClaimKind.LONGITUDINAL_TREND,
        }:
            reasons.add(ColdStartReason.BASELINE_POLICY_UNAVAILABLE)
        if not authorization_valid:
            reasons.add(ColdStartReason.AUTHORIZATION_MISSING)
        decisions.append(
            MetricReadinessDecision.create(
                decision_id=f"{decision_namespace}:{metric_id}",
                requirement_id=requirement.requirement_id,
                claim_kind=requirement.claim_kind,
                metric_id=metric_id,
                scope_source_hash=stable_hash(()),
                baseline_source_hash=stable_hash(()),
                claim_ceiling=ClaimCeiling.GENERAL_KNOWLEDGE,
                response_mode=(
                    ResponseMode.DEGRADED
                    if authorization_valid
                    else ResponseMode.BLOCKED
                ),
                reason_codes=tuple(
                    sorted(reasons, key=lambda item: item.value)
                ),
            )
        )
    return tuple(decisions)


def degraded_boundary_sentence(decision: MetricReadinessDecision) -> str:
    """Reviewed deterministic copy; internal enum names are never exposed."""

    count = decision.scope_valid_night_count
    if count == 0:
        return (
            "目前还没有可用于这项判断的个人记录；我可以先说明一般睡眠知识，"
            "或帮你检查设备和数据准备情况。"
        )
    if count == 1:
        return (
            "目前有 1 晚可用于这项判断的记录。我可以描述这一晚，"
            "但还不能判断个人趋势或是否偏离个人基线。"
        )
    if decision.claim_ceiling == ClaimCeiling.SHORT_SERIES_DIFFERENCE:
        return (
            f"目前有 {count} 晚可用于比较的记录。我可以描述这些夜晚的差异，"
            "但还不足以判断这是你的稳定规律。"
        )
    if decision.claim_ceiling == ClaimCeiling.PROVISIONAL_PATTERN:
        return (
            f"目前有 {count} 晚可用于观察的记录，只能作为初步规律，"
            "还需要更多同一来源且质量合格的数据继续确认。"
        )
    return (
        "当前个人记录暂不能可靠支持这项判断；可以先处理不依赖该结论的部分。"
    )


def snapshot_binding_material(
    *,
    decisions: Sequence[MetricReadinessDecision],
    capability_receipts: Sequence[CapabilityEligibilityReceipt] = (),
) -> dict[str, tuple[str, ...]]:
    decisions = tuple(
        MetricReadinessDecision.model_validate(item.model_dump(mode="json"))
        for item in decisions
    )
    capability_receipts = tuple(
        CapabilityEligibilityReceipt.model_validate(
            item.model_dump(mode="json")
        )
        for item in capability_receipts
    )
    decision_refs = tuple(item.decision_ref for item in decisions)
    decision_hashes = tuple(item.decision_hash for item in decisions)
    capability_refs = tuple(item.receipt_ref for item in capability_receipts)
    capability_hashes = tuple(item.receipt_hash for item in capability_receipts)
    if len(decision_refs) != len(set(decision_refs)):
        raise ValueError("duplicate readiness decision")
    if len(capability_refs) != len(set(capability_refs)):
        raise ValueError("duplicate capability receipt")
    return {
        "readiness_decision_refs": decision_refs,
        "readiness_decision_hashes": decision_hashes,
        "capability_eligibility_refs": capability_refs,
        "capability_eligibility_hashes": capability_hashes,
    }


def claim_strength_allowed(
    claim_strength: ClaimCeiling | str,
    decision: MetricReadinessDecision,
) -> bool:
    try:
        strength = ClaimCeiling(claim_strength)
    except ValueError:
        return False
    return (
        CLAIM_CEILING_RANK[strength]
        <= CLAIM_CEILING_RANK[decision.claim_ceiling]
    )


__all__ = [
    "BaselinePolicy",
    "BaselineReadinessProjection",
    "COLD_START_POLICY_VERSION",
    "CanonicalMetricNight",
    "CapabilityEligibilityReceipt",
    "ClaimCeiling",
    "ClaimKind",
    "ClaimRequirement",
    "ColdStartReason",
    "FIXTURE_BASELINE_POLICY_VERSION",
    "MeasurementCohort",
    "MeasurementCompatibilityReceipt",
    "MetricNightCount",
    "MetricReadinessDecision",
    "ProfileFieldReadiness",
    "ProfileFieldState",
    "REVIEWED_METRICS",
    "ResponseMode",
    "claim_requirement_id",
    "claim_strength_allowed",
    "build_unavailable_entry_decisions",
    "cohorts_compatible",
    "degraded_boundary_sentence",
    "derive_metric_valid_nights",
    "evaluate_readiness",
    "load_baseline_policy",
    "project_baseline_readiness",
    "resolve_claim_requirement",
    "snapshot_binding_material",
]
