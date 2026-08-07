"""Persistent, privacy-minimized Product Agent data provider.

This module reads one exact NightEpisode revision from the unified durable
repository.  It never decrypts Raw Inbox records and never returns the legacy
Radar ``raw_payload``/``data_payload`` compatibility shapes.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from sleepagent.radar_agent.product_agent.contracts import (
    AuthenticatedBinding,
    FactSnapshot,
    SourceScope,
    SourceScopeKind,
    stable_hash,
)
from sleepagent.radar_agent.product_agent.cold_start import (
    CapabilityEligibilityReceipt,
    MetricReadinessDecision,
    snapshot_binding_material,
)
from sleepagent.sleep_domain.contracts import (
    AnalysisRole,
    AnalysisRoleView,
    BedExitPayload,
    BedPresencePayload,
    DataMode,
    DeviceConnectivityPayload,
    HeartRatePayload,
    MissingIntervalPayload,
    MovementPayload,
    RespiratoryRatePayload,
    SleepDomainContract,
    SleepObservation,
    SleepStageIntervalPayload,
    UnknownObservationPayload,
    VendorAlertPayload,
    VendorSleepProfileMetricPayload,
)
from sleepagent.sleep_domain.repository import (
    DomainNamespace,
    NamespaceMismatchError,
    SleepDomainRepository,
)


INTERNAL_AGENT_ANALYSIS_SCOPE = "internal_agent_analysis"
CANONICAL_SLEEP_READ_SCOPE = "read_sleep_data"
ROLE_VIEW_SCOPES = {
    AnalysisRole.ELDER: "read_elder_view",
    AnalysisRole.FAMILY: "read_family_view",
    AnalysisRole.DOCTOR: "read_doctor_view",
}
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
        evidence = self.model_dump(mode="json")
        quality = dict(self.deterministic_quality)
        coverage_ratio = quality.get("coverage_ratio", 0.0)
        risk_state = str(self.deterministic_risk.get("risk_state", "unknown"))
        risk_score = 0.8 if risk_state == "reviewed_signal" else 0.4
        return {
            "radar.get_night_evidence": {
                "data": evidence,
                "source_refs": list(self.provenance_references),
            },
            "radar.assess_data_quality": {
                "coverage_ratio": coverage_ratio,
                "data": quality,
                "source_refs": list(self.provenance_references),
            },
            "radar.get_device_status": {
                "data": {
                    "data_mode": self.data_mode.value,
                    "offline": bool(quality.get("offline", False)),
                    "stale": bool(quality.get("stale", False)),
                },
                "source_refs": list(self.provenance_references),
            },
            "risk.classify_signal": {
                "score": risk_score,
                "data": dict(self.deterministic_risk),
                "source_refs": list(self.provenance_references),
            },
        }


class PersistentProductDataProvider:
    """Read committed canonical data without touching Raw Inbox payload bytes."""

    def __init__(self, repository: SleepDomainRepository) -> None:
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
            **cold_start_bindings,
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
        return view


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
