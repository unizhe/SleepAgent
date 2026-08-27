# 本模块负责睡眠领域规则与数据语义，不依赖 HTTP 或进程装配。
"""Persistent, privacy-minimized Product Agent data provider.

This module reads one exact NightEpisode revision from the unified durable
repository.  It never decrypts Raw Inbox records and never returns the legacy
Radar ``raw_payload``/``data_payload`` compatibility shapes.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Literal, cast

from pydantic import Field, model_validator

from sleepagent.runtime.contracts import (
    AuthenticatedBinding,
    FactSnapshot,
    SourceScope,
    SourceScopeKind,
    stable_hash,
)
from sleepagent.runtime.cold_start import (
    CapabilityEligibilityReceipt,
    MetricReadinessDecision,
    snapshot_binding_material,
)
from sleepagent.runtime.tools import TrendRiskSignal
from sleepagent.domain.contracts import (
    AnalysisRole,
    AnalysisRoleView,
    BedExitPayload,
    BedPresencePayload,
    DataMode,
    DeviceConnectivityPayload,
    DomainNamespace,
    HeartRatePayload,
    MissingIntervalPayload,
    MovementPayload,
    NamespaceMismatchError,
    RespiratoryRatePayload,
    SleepDomainContract,
    SleepObservation,
    SleepStageIntervalPayload,
    UnknownObservationPayload,
    VendorAlertPayload,
    VendorSleepProfileMetricPayload,
)


INTERNAL_AGENT_ANALYSIS_SCOPE = "internal_agent_analysis"
CANONICAL_SLEEP_READ_SCOPE = "read_sleep_data"
ROLE_VIEW_SCOPES = {
    AnalysisRole.ELDER: "read_elder_view",
    AnalysisRole.FAMILY: "read_family_view",
    AnalysisRole.DOCTOR: "read_doctor_view",
}
PUBLIC_SUBJECT_REF_PREFIX = "subject:sha256:"

LONGITUDINAL_VITAL_MIN_NIGHTS = 3
LONGITUDINAL_HEART_RATE_MIN_TOTAL_DELTA = 5.0
LONGITUDINAL_RESPIRATORY_RATE_MIN_TOTAL_DELTA = 1.5
FORBIDDEN_AGENT_CONTEXT_KEYS = frozenset(
    {
        "raw_payload",
        "data_payload",
        "encrypted_payload",
        "raw_vendor_text",
        "vendor_text",
        "source_timestamp_text",
        "source_text",
        "source_start_text",
        "source_end_text",
        "vendor_status_code",
        "message",
        "title",
    }
)


def public_product_subject_ref(subject_id: str) -> str:
    """Return the stable public Product pseudonym for an internal subject ID."""

    if not subject_id:
        raise ValueError("public Product subject reference requires a subject ID")
    return PUBLIC_SUBJECT_REF_PREFIX + stable_hash(
        {
            "schema_version": "public_product_subject_ref.v1",
            "subject_id": subject_id,
        }
    )


class ProductDataAuthorization(SleepDomainContract):
    actor_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    role: Literal["elder", "family", "doctor", "system"]
    data_mode: DataMode
    authorization_scope: tuple[str, ...]

    def require_canonical_read(self) -> None:
        required = (
            INTERNAL_AGENT_ANALYSIS_SCOPE
            if self.role == "system"
            else CANONICAL_SLEEP_READ_SCOPE
        )
        if required not in self.authorization_scope:
            raise PermissionError(f"missing required scope: {required}")


class RoleViewAuthorization(SleepDomainContract):
    actor_id: str = Field(..., min_length=1)
    subject_id: str = Field(..., min_length=1)
    role: AnalysisRole
    data_mode: DataMode
    authorization_scope: tuple[str, ...]


class ProductNightVitalSummary(SleepDomainContract):
    """Privacy-minimized centers for one exact finalized Episode revision."""

    schema_version: Literal["product_night_vital_summary.v1"] = (
        "product_night_vital_summary.v1"
    )
    local_sleep_date: date
    night_episode_revision_ref: str = Field(..., min_length=1)
    heart_rate_center: float = Field(..., gt=0, le=300)
    respiratory_rate_center: float = Field(..., gt=0, le=150)
    heart_rate_sample_count: int = Field(..., ge=1)
    respiratory_rate_sample_count: int = Field(..., ge=1)


class ProductLongitudinalRiskContext(SleepDomainContract):
    """Source-bound, non-diagnostic trend signal eligible for Care routing."""

    schema_version: Literal["product_longitudinal_risk_context.v1"] = (
        "product_longitudinal_risk_context.v1"
    )
    policy_version: Literal["product-longitudinal-vital-watch.v1"] = (
        "product-longitudinal-vital-watch.v1"
    )
    date_start: date
    date_end: date
    valid_night_count: int = Field(..., ge=LONGITUDINAL_VITAL_MIN_NIGHTS)
    night_summaries: tuple[ProductNightVitalSummary, ...] = Field(
        min_length=LONGITUDINAL_VITAL_MIN_NIGHTS
    )
    trend_signals: tuple[TrendRiskSignal, ...] = Field(min_length=1)
    reason_codes: tuple[Literal["consistent_vital_increase_three_nights"], ...]

    @model_validator(mode="after")
    def require_exact_ordered_window(self) -> "ProductLongitudinalRiskContext":
        dates = tuple(item.local_sleep_date for item in self.night_summaries)
        if dates != tuple(sorted(dates)) or len(dates) != len(set(dates)):
            raise ValueError("longitudinal vital nights must be unique and ordered")
        if self.date_start != dates[0] or self.date_end != dates[-1]:
            raise ValueError("longitudinal vital dates do not bind the summaries")
        if self.valid_night_count != len(self.night_summaries):
            raise ValueError("longitudinal valid-night count is inconsistent")
        return self


def build_longitudinal_vital_risk_context(
    summaries: tuple[ProductNightVitalSummary, ...],
) -> ProductLongitudinalRiskContext | None:
    """Classify a recent three-night dual-vital increase as a watch signal."""

    window = tuple(sorted(summaries, key=lambda item: item.local_sleep_date))[
        -LONGITUDINAL_VITAL_MIN_NIGHTS:
    ]
    if len(window) < LONGITUDINAL_VITAL_MIN_NIGHTS:
        return None
    if any(
        later.local_sleep_date - earlier.local_sleep_date != timedelta(days=1)
        for earlier, later in zip(window, window[1:])
    ):
        return None
    heart = tuple(item.heart_rate_center for item in window)
    respiratory = tuple(item.respiratory_rate_center for item in window)
    if not (
        all(later > earlier for earlier, later in zip(heart, heart[1:]))
        and all(
            later > earlier
            for earlier, later in zip(respiratory, respiratory[1:])
        )
        and heart[-1] - heart[0]
        >= LONGITUDINAL_HEART_RATE_MIN_TOTAL_DELTA
        and respiratory[-1] - respiratory[0]
        >= LONGITUDINAL_RESPIRATORY_RATE_MIN_TOTAL_DELTA
    ):
        return None
    source_refs = tuple(
        item.night_episode_revision_ref for item in window
    )
    return ProductLongitudinalRiskContext(
        date_start=window[0].local_sleep_date,
        date_end=window[-1].local_sleep_date,
        valid_night_count=len(window),
        night_summaries=window,
        trend_signals=(
            TrendRiskSignal(
                risk_level="watch",
                confidence=min(1.0, len(window) / 4),
                source_refs=source_refs,
            ),
        ),
        reason_codes=("consistent_vital_increase_three_nights",),
    )


class ProductRevisionFacts(SleepDomainContract):
    """Safe, versioned tool input for one exact NightEpisode revision."""

    schema_version: Literal["product_revision_facts.v1"] = (
        "product_revision_facts.v1"
    )
    night_episode_id: str = Field(..., min_length=1)
    night_episode_revision_id: str = Field(..., min_length=1)
    night_episode_revision_number: int = Field(..., ge=1)
    subject_id: str = Field(..., min_length=1)
    data_mode: DataMode
    timezone_name: str = Field(..., min_length=1)
    local_sleep_date: str = Field(..., min_length=1)
    data_sufficiency: str = Field(..., min_length=1)
    canonical_observations: tuple[dict[str, Any], ...]
    deterministic_quality: dict[str, Any]
    deterministic_risk: dict[str, Any]
    longitudinal_risk_context: ProductLongitudinalRiskContext | None = None
    conflict_summaries: tuple[dict[str, Any], ...]
    provenance_references: tuple[str, ...]
    canonical_data_version: str = Field(..., min_length=64, max_length=64)

    @model_validator(mode="after")
    def reject_private_or_mixed_mode_content(self) -> "ProductRevisionFacts":
        assert_agent_safe_payload(
            self.model_dump(mode="json"),
            expected_data_mode=self.data_mode,
        )
        return self

    def tool_inputs(self) -> dict[str, dict[str, Any]]:
        # ProductRevisionFacts is the local lineage DTO.  It deliberately
        # contains subject and observation identities and must never double as
        # the provider-facing DTO.
        evidence = self.agent_night_evidence()
        agent_refs = list(self.agent_source_refs())
        quality = self.provider_quality_summary()
        coverage_ratio = quality.get("coverage_ratio", 0.0)
        risk = self.provider_risk_summary()
        risk.setdefault("data_sufficiency", self.data_sufficiency)
        risk_arguments: dict[str, Any] = {
            "data": risk,
            "source_refs": agent_refs,
        }
        if self.longitudinal_risk_context is not None:
            risk_arguments["trend_signals"] = [
                {
                    "risk_level": item.risk_level,
                    "confidence": item.confidence,
                    "source_refs": agent_refs,
                }
                for item in self.longitudinal_risk_context.trend_signals
            ]
            risk_arguments["trend_observation"] = {
                "quality_status": (
                    "good"
                    if self.data_sufficiency == "sufficient"
                    else "partial"
                ),
                "confidence_label": "normal",
                "health_conclusion_allowed": (
                    self.data_sufficiency == "sufficient"
                ),
                "source_refs": agent_refs,
            }
        return {
            "radar.get_night_evidence": {
                "data": evidence,
                "source_refs": agent_refs,
            },
            "radar.assess_data_quality": {
                "coverage_ratio": coverage_ratio,
                "data": quality,
                "source_refs": agent_refs,
            },
            "radar.get_device_status": {
                "data": {
                    "schema_version": "canonical_radar_device_status.v1",
                    "data_mode": self.data_mode.value,
                    "offline": bool(quality.get("offline", False)),
                    "stale": bool(quality.get("stale", False)),
                },
                "source_refs": agent_refs,
            },
            "risk.classify_signal": {
                **risk_arguments,
            },
        }

    def agent_source_refs(self) -> tuple[str, ...]:
        """Return one non-identifying, hash-bound provenance-set reference."""

        return (
            "governed_evidence_set:sha256:"
            f"{stable_hash(self.provenance_references)}:"
            f"count:{len(self.provenance_references)}",
        )

    def provider_quality_summary(self) -> dict[str, Any]:
        """Allowlist quality semantics; retain all row/scope IDs locally."""

        quality = self.deterministic_quality
        scope = quality.get("source_scope")
        safe_scope = self._provider_source_scope(scope)
        return {
            key: quality[key]
            for key in (
                "schema_version",
                "data_mode",
                "quality_state",
                "data_sufficiency",
                "missingness_state",
                "coverage_ratio",
                "expected_bin_count",
                "covered_bin_count",
                "explicit_missing_interval_count",
                "invalid_observation_count",
                "stale",
                "offline",
                "clock_invalid",
                "policy_version",
                "reason_codes",
            )
            if key in quality
        } | ({"source_scope": safe_scope} if safe_scope else {})

    def provider_risk_summary(self) -> dict[str, Any]:
        """Allowlist deterministic risk semantics without persistent IDs."""

        risk = self.deterministic_risk
        scope = risk.get("source_scope")
        safe_scope = self._provider_source_scope(scope)
        summary = {
            key: risk[key]
            for key in (
                "schema_version",
                "data_mode",
                "risk_state",
                "data_sufficiency",
                "policy_version",
                "reason_codes",
                "health_escalation_allowed",
                "is_all_clear",
            )
            if key in risk
        }
        if safe_scope:
            summary["source_scope"] = safe_scope
        if "active_vendor_alert_instance_ids" in risk:
            summary["active_vendor_alert_count"] = len(
                risk.get("active_vendor_alert_instance_ids") or ()
            )
        if "pending_domain_review_rule_ids" in risk:
            summary["pending_domain_review_rule_count"] = len(
                risk.get("pending_domain_review_rule_ids") or ()
            )
        return summary

    @staticmethod
    def _provider_source_scope(scope: Any) -> dict[str, Any]:
        if not isinstance(scope, dict):
            return {}
        observation_ids = scope.get("observation_ids")
        return {
            key: value
            for key, value in (
                ("observation_count", (
                    len(observation_ids)
                    if isinstance(observation_ids, (list, tuple))
                    else None
                )),
                ("observation_types", scope.get("observation_types")),
                ("window_start_at", scope.get("window_start_at")),
                ("window_end_at", scope.get("window_end_at")),
            )
            if value is not None
        }

    def agent_night_evidence(self) -> dict[str, Any]:
        """Project exact revision facts into a bounded Agent-facing summary."""

        return {
            "schema_version": "product_night_evidence.v1",
            "data_mode": self.data_mode.value,
            "timezone_name": self.timezone_name,
            "local_sleep_date": self.local_sleep_date,
            "data_sufficiency": self.data_sufficiency,
            "canonical_observation_count": len(self.canonical_observations),
            "conflict_count": len(self.conflict_summaries),
            "deterministic_quality": self.provider_quality_summary(),
            "deterministic_risk": self.provider_risk_summary(),
            "deterministic_night_summary": self.deterministic_night_summary(),
            "evidence_authority": self.evidence_authority_summary(),
            "longitudinal_risk_context": (
                None
                if self.longitudinal_risk_context is None
                else self._provider_longitudinal_risk_context()
            ),
        }

    def _provider_longitudinal_risk_context(self) -> dict[str, Any]:
        context = self.longitudinal_risk_context
        if context is None:
            return {}
        source_refs = list(self.agent_source_refs())
        return {
            "schema_version": context.schema_version,
            "policy_version": context.policy_version,
            "date_start": context.date_start.isoformat(),
            "date_end": context.date_end.isoformat(),
            "valid_night_count": context.valid_night_count,
            "night_summaries": [
                {
                    "local_sleep_date": item.local_sleep_date.isoformat(),
                    "heart_rate_center": item.heart_rate_center,
                    "respiratory_rate_center": item.respiratory_rate_center,
                    "heart_rate_sample_count": item.heart_rate_sample_count,
                    "respiratory_rate_sample_count": (
                        item.respiratory_rate_sample_count
                    ),
                }
                for item in context.night_summaries
            ],
            "trend_signals": [
                {
                    "risk_level": item.risk_level,
                    "confidence": item.confidence,
                    "source_refs": source_refs,
                }
                for item in context.trend_signals
            ],
            "reason_codes": list(context.reason_codes),
        }

    def evidence_authority_summary(self) -> dict[str, Any]:
        """Expose governed source authority without identifiers or raw payloads."""

        push_measurements = 0
        pull_measurements = 0
        matched_measurements = 0
        pull_backfilled_measurements = 0
        vendor_derived_stage_count = 0
        independent_stage_count = 0
        for observation in self.canonical_observations:
            payload = observation.get("payload")
            if not isinstance(payload, dict):
                continue
            observation_type = str(payload.get("observation_type") or "")
            source_kind = str(observation.get("source_kind") or "")
            channels = {
                str(item)
                for item in observation.get("acquisition_channels", ())
                if str(item) in {"PUSH", "PULL"}
            }
            device_measurement = observation_type in {
                "heart_rate",
                "respiratory_rate",
                "movement",
                "bed_presence",
            }
            if device_measurement:
                push_measurements += int("PUSH" in channels)
                pull_measurements += int("PULL" in channels)
                matched_measurements += int(channels == {"PUSH", "PULL"})
                pull_backfilled_measurements += int(channels == {"PULL"})
            if observation_type == "sleep_stage_interval":
                if source_kind == "vendor_derived":
                    vendor_derived_stage_count += 1
                else:
                    independent_stage_count += 1

        relation = "not_applicable"
        if push_measurements and pull_measurements:
            relation = (
                "exact_semantic_overlap"
                if matched_measurements
                else "non_identical_cadence"
            )
        stage_authority = "unavailable"
        if vendor_derived_stage_count and not independent_stage_count:
            stage_authority = "vendor_derived"
        elif vendor_derived_stage_count or independent_stage_count:
            stage_authority = "mixed"
        return {
            "sleep_stage_authority": stage_authority,
            "sleep_stage_vendor_derived_interval_count": (
                vendor_derived_stage_count
            ),
            "pull_backfilled_measurement_count": pull_backfilled_measurements,
            "push_measurement_count": push_measurements,
            "pull_measurement_count": pull_measurements,
            "matched_push_pull_measurement_count": matched_measurements,
            "push_pull_relation": relation,
            "pull_timestamp_semantics": (
                "reconstructed_from_vendor_batch_cadence"
                if pull_backfilled_measurements
                else "not_applicable"
            ),
            "independent_sleepagent_stage_classification": False,
        }

    def deterministic_night_summary(self) -> dict[str, Any]:
        """Reduce canonical samples to bounded facts before Agent reasoning."""

        samples: dict[str, list[float]] = {
            "heart_rate": [],
            "respiratory_rate": [],
            "movement": [],
        }
        stage_minutes: dict[str, float] = {}
        bed_events: list[tuple[str, datetime]] = []
        window_start: datetime | None = None
        window_end: datetime | None = None
        for observation in self.canonical_observations:
            payload = observation.get("payload")
            if not isinstance(payload, dict):
                continue
            observation_type = str(payload.get("observation_type") or "")
            value = payload.get("value")
            if observation_type in samples and isinstance(value, (int, float)):
                samples[observation_type].append(float(value))
            if observation_type == "sleep_stage_interval":
                start = _aware_wire_datetime(payload.get("start_at"))
                end = _aware_wire_datetime(payload.get("end_at"))
                stage = payload.get("stage")
                if start is not None and end is not None and end > start:
                    window_start = start if window_start is None else min(window_start, start)
                    window_end = end if window_end is None else max(window_end, end)
                    if isinstance(stage, str) and stage:
                        stage_minutes[stage] = stage_minutes.get(stage, 0.0) + (
                            end - start
                        ).total_seconds() / 60
            if observation_type == "bed_exit":
                event_at = _aware_wire_datetime(
                    observation.get("event_occurred_at")
                    or observation.get("measurement_at")
                )
                kind = payload.get("kind")
                if event_at is not None and isinstance(kind, str):
                    bed_events.append((kind, event_at))

        exits: list[dict[str, Any]] = []
        pending_exit: datetime | None = None
        for kind, event_at in sorted(bed_events, key=lambda item: item[1]):
            if kind == "bed_exit":
                pending_exit = event_at
            elif kind == "return_to_bed" and pending_exit is not None:
                exits.append(
                    {
                        "left_bed_at": pending_exit.isoformat(),
                        "local_time": pending_exit.strftime("%H:%M"),
                        "returned_at": event_at.isoformat(),
                        "duration_minutes": round(
                            (event_at - pending_exit).total_seconds() / 60,
                            1,
                        ),
                    }
                )
                pending_exit = None
        if pending_exit is not None:
            exits.append(
                {
                    "left_bed_at": pending_exit.isoformat(),
                    "local_time": pending_exit.strftime("%H:%M"),
                    "returned_at": None,
                    "duration_minutes": None,
                }
            )

        return {
            "schema_version": "product_deterministic_night_summary.v1",
            "sleep_window_start": (
                None if window_start is None else window_start.isoformat()
            ),
            "sleep_window_end": None if window_end is None else window_end.isoformat(),
            "sleep_window_minutes": (
                None
                if window_start is None or window_end is None
                else round((window_end - window_start).total_seconds() / 60, 1)
            ),
            "stage_minutes": {
                key: round(value, 1) for key, value in sorted(stage_minutes.items())
            },
            "vital_centers": {
                key: (None if not values else round(sum(values) / len(values), 1))
                for key, values in samples.items()
            },
            "sample_counts": {key: len(values) for key, values in samples.items()},
            "bed_exit_count": len(exits),
            "bed_exit_events": exits,
        }


class PersistentProductDataProvider:
    """Read committed canonical data without touching Raw Inbox payload bytes."""

    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def load_revision(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_revision_id: str,
        authorization: ProductDataAuthorization,
    ) -> ProductRevisionFacts:
        authorization.require_canonical_read()
        if authorization.data_mode != namespace.data_mode:
            raise NamespaceMismatchError("authorization data_mode mismatch")
        revision = self.repository.get_night_episode_revision(
            namespace,
            night_episode_revision_id=night_episode_revision_id,
        )
        if revision is None:
            raise KeyError(
                f"NightEpisodeRevision not found: {night_episode_revision_id}"
            )
        if (
            revision.data_mode != namespace.data_mode
            or revision.subject_id != authorization.subject_id
        ):
            raise PermissionError("NightEpisodeRevision is outside authorization")
        episode = self.repository.get_night_episode(
            namespace,
            night_episode_id=revision.night_episode_id,
        )
        if episode is None:
            raise KeyError(f"NightEpisode not found: {revision.night_episode_id}")
        if (
            episode.subject_id != revision.subject_id
            or episode.data_mode != revision.data_mode
        ):
            raise NamespaceMismatchError("NightEpisode revision identity mismatch")

        observations: list[SleepObservation] = []
        safe_observations: list[dict[str, Any]] = []
        source_refs: list[str] = [
            (
                f"night_episode_revision:{namespace.data_mode.value}:"
                f"{revision.night_episode_revision_id}:"
                f"{revision.observation_set_sha256}"
            )
        ]
        for observation_id in revision.observation_ids:
            observation = self.repository.get_observation(
                namespace,
                observation_id=observation_id,
            )
            if observation is None:
                raise KeyError(
                    f"canonical observation not found: {observation_id}"
                )
            if (
                observation.subject_id != revision.subject_id
                or observation.data_mode != revision.data_mode
            ):
                raise NamespaceMismatchError(
                    "revision contains a cross-subject or cross-mode observation"
                )
            observations.append(observation)
            projected, refs = _project_observation(observation)
            safe_observations.append(projected)
            source_refs.extend(refs)
        conflicts = self.repository.list_observation_conflicts(
            namespace,
            observation_ids=revision.observation_ids,
        )
        conflict_summaries = tuple(
            {
                "conflict_ref": f"observation_conflict:{item.conflict_id}",
                "first_observation_ref": (
                    f"canonical_observation:{item.first_observation_id}"
                ),
                "second_observation_ref": (
                    f"canonical_observation:{item.second_observation_id}"
                ),
                "resolution": "unresolved",
                "detected_at": item.detected_at.isoformat(),
            }
            for item in conflicts
        )
        source_refs.extend(
            item["conflict_ref"] for item in conflict_summaries
        )

        quality = self.repository.get_current_quality(
            namespace,
            night_episode_id=revision.night_episode_id,
        )
        risk = self.repository.get_current_risk(
            namespace,
            night_episode_id=revision.night_episode_id,
        )
        scoped_observation_ids = set(revision.observation_ids)
        fallback_sufficiency = (
            revision.data_sufficiency.value
            if observations
            else "data_insufficient"
        )
        quality_payload: dict[str, Any] = {
            "data_mode": revision.data_mode.value,
            "data_sufficiency": fallback_sufficiency,
            "quality_flags": list(revision.quality_flags),
            "coverage_ratio": 0.0 if not observations else 1.0,
            "reason_codes": (
                list(revision.quality_flags)
                if observations
                else ["no_canonical_observations"]
            ),
        }
        if quality is not None and _scope_matches_revision(
            revision.night_episode_revision_id,
            scoped_observation_ids,
            quality.source_scope.night_episode_revision_id,
            set(quality.source_scope.observation_ids),
        ):
            quality_payload = quality.model_dump(mode="json")
            source_refs.append(f"quality_assessment:{quality.assessment_id}")
        risk_payload: dict[str, Any] = {
            "data_mode": revision.data_mode.value,
            "risk_state": "unknown",
            "data_sufficiency": revision.data_sufficiency.value,
            "reason_codes": ["no_exact_revision_risk_summary"],
            "is_all_clear": False,
        }
        if risk is not None and _scope_matches_revision(
            revision.night_episode_revision_id,
            scoped_observation_ids,
            risk.source_scope.night_episode_revision_id,
            set(risk.source_scope.observation_ids),
        ):
            risk_payload = risk.model_dump(mode="json")
            source_refs.append(f"current_risk:{risk.current_risk_id}")
        for report_ref, report_hash in zip(
            revision.source_report_references,
            revision.source_report_sha256,
        ):
            source_refs.append(
                f"source_report:{report_ref}:sha256:{report_hash}"
            )

        unique_refs = tuple(dict.fromkeys(source_refs))
        version_material = {
            "night_episode_revision_id": revision.night_episode_revision_id,
            "observation_set_sha256": revision.observation_set_sha256,
            "source_report_sha256": revision.source_report_sha256,
            "data_mode": revision.data_mode.value,
            "observations": safe_observations,
            "quality": quality_payload,
            "risk": risk_payload,
            "conflicts": conflict_summaries,
            "pinned_adapter_versions": episode.pinned_adapter_versions,
            "pinned_observation_schema_versions": (
                episode.pinned_observation_schema_versions
            ),
            "pinned_policy_versions": episode.pinned_policy_versions,
        }
        return ProductRevisionFacts(
            night_episode_id=revision.night_episode_id,
            night_episode_revision_id=revision.night_episode_revision_id,
            night_episode_revision_number=revision.revision_number,
            subject_id=revision.subject_id,
            data_mode=revision.data_mode,
            timezone_name=episode.timezone_name,
            local_sleep_date=episode.local_sleep_date.isoformat(),
            data_sufficiency=revision.data_sufficiency.value,
            canonical_observations=tuple(safe_observations),
            deterministic_quality=quality_payload,
            deterministic_risk=risk_payload,
            conflict_summaries=conflict_summaries,
            provenance_references=unique_refs,
            canonical_data_version=stable_hash(version_material),
        )

    def build_fact_snapshot(
        self,
        facts: ProductRevisionFacts,
        *,
        binding: AuthenticatedBinding,
        fact_snapshot_id: str,
        care_context_version: int,
        memory_context_version: int,
        created_at: datetime,
        readiness_decisions: tuple[MetricReadinessDecision, ...] = (),
        capability_receipts: tuple[CapabilityEligibilityReceipt, ...] = (),
    ) -> FactSnapshot:
        local_date = date.fromisoformat(facts.local_sleep_date)
        cold_start_bindings = snapshot_binding_material(
            decisions=readiness_decisions,
            capability_receipts=capability_receipts,
        )
        # This legacy scalar is display-only.  It is authoritative only when
        # the snapshot has exactly one metric decision; multi-metric snapshots
        # use the typed per-metric counts and deliberately expose zero here.
        if len(readiness_decisions) == 1:
            compatibility_count = (
                readiness_decisions[0].scope_valid_night_count
            )
        elif readiness_decisions:
            compatibility_count = 0
        else:
            compatibility_count = 0
        scope = SourceScope(
            kind=SourceScopeKind.CURRENT_NIGHT,
            as_of=created_at,
            timezone_name=facts.timezone_name,
            date_start=local_date,
            date_end=local_date,
            valid_night_count=compatibility_count,
        )
        return FactSnapshot.create(
            fact_snapshot_id=fact_snapshot_id,
            binding=binding,
            source_scope=scope,
            canonical_data_version=facts.canonical_data_version,
            care_context_version=care_context_version,
            memory_context_version=memory_context_version,
            active_constraint_codes=tuple(
                sorted(
                    {
                        *facts.deterministic_quality.get("reason_codes", ()),
                        *facts.deterministic_risk.get("reason_codes", ()),
                    }
                )
            ),
            source_refs=facts.provenance_references,
            readiness_decision_refs=cold_start_bindings[
                "readiness_decision_refs"
            ],
            readiness_decision_hashes=cold_start_bindings[
                "readiness_decision_hashes"
            ],
            capability_eligibility_refs=cold_start_bindings[
                "capability_eligibility_refs"
            ],
            capability_eligibility_hashes=cold_start_bindings[
                "capability_eligibility_hashes"
            ],
            created_at=created_at,
        )

    def get_role_view(
        self,
        namespace: DomainNamespace,
        *,
        analysis_revision_id: str,
        authorization: RoleViewAuthorization,
    ) -> AnalysisRoleView:
        if authorization.data_mode != namespace.data_mode:
            raise NamespaceMismatchError("role-view authorization mode mismatch")
        required_scope = ROLE_VIEW_SCOPES[authorization.role]
        if required_scope not in authorization.authorization_scope:
            raise PermissionError(f"missing required scope: {required_scope}")
        view = self.repository.get_analysis_role_view(
            namespace,
            analysis_revision_id=analysis_revision_id,
            role=authorization.role,
        )
        if view is None:
            raise KeyError(
                f"analysis role view not found: {analysis_revision_id}"
            )
        if (
            view.subject_id != authorization.subject_id
            or view.data_mode != authorization.data_mode
            or view.role != authorization.role
        ):
            raise PermissionError("analysis role view is outside authorization")
        return cast(AnalysisRoleView, view)


def assert_agent_safe_payload(
    payload: Any,
    *,
    expected_data_mode: DataMode,
) -> None:
    """Fail closed if a Product Agent input contains raw/legacy/mixed data."""

    def visit(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, (bytes, bytearray, memoryview)):
            raise ValueError(
                "binary/raw payload is forbidden in Agent context at "
                + ".".join(path)
            )
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).strip().lower()
                if normalized in FORBIDDEN_AGENT_CONTEXT_KEYS:
                    raise ValueError(
                        f"forbidden Agent context key: {'.'.join((*path, normalized))}"
                    )
                if normalized == "data_mode" and item != expected_data_mode.value:
                    raise NamespaceMismatchError(
                        "Agent context contains another data_mode"
                    )
                visit(item, (*path, normalized))
            return
        if isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                visit(item, (*path, str(index)))

    visit(payload, ())


def _project_observation(
    observation: SleepObservation,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    payload = observation.payload
    if isinstance(payload, (HeartRatePayload, RespiratoryRatePayload, MovementPayload)):
        safe_payload = {
            "observation_type": payload.observation_type.value,
            "value": payload.value,
            "unit": payload.unit,
        }
    elif isinstance(payload, BedPresencePayload):
        safe_payload = {
            "observation_type": payload.observation_type.value,
            "state": payload.state.value,
        }
    elif isinstance(payload, DeviceConnectivityPayload):
        safe_payload = {
            "observation_type": payload.observation_type.value,
            "state": payload.state.value,
        }
    elif isinstance(payload, VendorAlertPayload):
        safe_payload = {
            "observation_type": payload.observation_type.value,
            "alert_code": payload.alert_code,
            "severity": payload.severity.value,
            "lifecycle_state": payload.lifecycle_state.value,
            "vendor_alert_instance_ref": payload.vendor_alert_instance_id,
        }
    elif isinstance(payload, SleepStageIntervalPayload):
        safe_payload = {
            "observation_type": payload.observation_type.value,
            "stage": payload.stage.value,
            "start_at": (
                payload.start_at.isoformat() if payload.start_at else None
            ),
            "end_at": payload.end_at.isoformat() if payload.end_at else None,
            "time_state": "known" if payload.start_at else "timezone_unknown",
        }
    elif isinstance(payload, VendorSleepProfileMetricPayload):
        safe_payload = {
            "observation_type": payload.observation_type.value,
            "metric_name": payload.metric_name,
            "value_state": payload.value_state.value,
            "value": payload.value,
            "unit": payload.unit,
        }
    elif isinstance(payload, BedExitPayload):
        safe_payload = {
            "observation_type": payload.observation_type.value,
            "kind": payload.kind.value,
        }
    elif isinstance(payload, MissingIntervalPayload):
        safe_payload = {
            "observation_type": payload.observation_type.value,
            "target_observation_type": payload.target_observation_type.value,
            "missing_state": payload.missing_state.value,
            "reason_code": payload.reason_code,
            "interval_start_at": (
                payload.interval_start_at.isoformat()
                if payload.interval_start_at
                else None
            ),
            "interval_end_at": (
                payload.interval_end_at.isoformat()
                if payload.interval_end_at
                else None
            ),
        }
    elif isinstance(payload, UnknownObservationPayload):
        safe_payload = {
            "observation_type": payload.observation_type.value,
            "source_type": payload.source_type,
            "reason_code": payload.reason_code,
        }
    else:  # pragma: no cover - discriminated union is exhaustive
        raise TypeError(f"unsupported canonical observation: {type(payload).__name__}")

    provenance = observation.provenance
    refs = (
        f"canonical_observation:{observation.observation_id}",
        (
            f"raw_ingress:{observation.data_mode.value}:"
            f"{provenance.raw_ingress_record_id}:"
            f"sha256:{provenance.raw_payload_sha256}"
        ),
        f"adapter:{provenance.adapter_id}@{provenance.adapter_version}",
        *(
            f"acquisition_receipt:{receipt_id}"
            for receipt_id in provenance.acquisition_receipt_ids
        ),
    )
    projected = {
        "observation_ref": refs[0],
        "data_mode": observation.data_mode.value,
        "observation_type": observation.observation_type.value,
        "subject_id": observation.subject_id,
        "device_ref": f"device:{observation.device_id}",
        "binding_ref": (
            f"device_binding:{observation.device_binding_id}:"
            f"v{observation.binding_version}"
        ),
        "measurement_at": (
            observation.measurement_at.isoformat()
            if observation.measurement_at
            else None
        ),
        "event_occurred_at": (
            observation.event_occurred_at.isoformat()
            if observation.event_occurred_at
            else None
        ),
        "received_at": observation.received_at.isoformat(),
        "timezone_status": observation.timezone_status.value,
        "source_kind": observation.source_kind.value,
        "payload": safe_payload,
        "quality": observation.quality.model_dump(mode="json"),
        "provenance_references": list(refs[1:]),
    }
    assert_agent_safe_payload(
        projected,
        expected_data_mode=observation.data_mode,
    )
    return projected, refs


def _aware_wire_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _scope_matches_revision(
    revision_id: str,
    observation_ids: set[str],
    scope_revision_id: str | None,
    scope_observation_ids: set[str],
) -> bool:
    if scope_revision_id is not None:
        return scope_revision_id == revision_id
    return scope_observation_ids == observation_ids


__all__ = [
    "CANONICAL_SLEEP_READ_SCOPE",
    "FORBIDDEN_AGENT_CONTEXT_KEYS",
    "INTERNAL_AGENT_ANALYSIS_SCOPE",
    "ROLE_VIEW_SCOPES",
    "PersistentProductDataProvider",
    "ProductDataAuthorization",
    "ProductRevisionFacts",
    "RoleViewAuthorization",
    "assert_agent_safe_payload",
]
