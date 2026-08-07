"""Controlled migration from the legacy Radar read model to canonical sleep data.

The compatibility projector is deliberately invoked by migration/backfill and
cutover tooling.  Canonical commits do not dual-write this table.  Shadow
comparisons persist only hashes and field names, never raw payloads or health
values.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping

from sleepagent.radar_agent.persistence.models import RadarSubject
from sleepagent.radar_agent.schemas import (
    RadarDataQualityStatus,
    RadarDevice,
    RadarDeviceStatus,
    RadarNightSummary,
)
from sleepagent.sleep_domain.contracts import (
    AvailabilityState,
    BedExitKind,
    BedExitPayload,
    BedPresencePayload,
    BedPresenceState,
    DataMode,
    DeviceConnectivityPayload,
    DeviceConnectivityState,
    MissingIntervalPayload,
    MovementPayload,
    NightEpisode,
    NightEpisodeRevision,
    QualityState,
    SleepObservation,
    SleepStageIntervalPayload,
    SleepStageState,
    VendorSleepProfileMetricPayload,
)
from sleepagent.sleep_domain.crypto import RawPayloadEncryptionPolicy
from sleepagent.sleep_domain.repository import (
    DomainNamespace,
    SleepDomainRepository,
)


UTC = timezone.utc
COMPATIBILITY_PROJECTION_VERSION = "radar-current-night.v1"
DEPLOYMENT_MODE_ENV = "SLEEPAGENT_DEPLOYMENT_MODE"
READ_AUTHORITY_ENV = "SLEEPAGENT_SLEEP_DATA_AUTHORITY"
PRODUCT_PROVIDER_MODE_ENV = "SLEEPAGENT_PRODUCT_RADAR_PROVIDER_MODE"
RADAR_DATABASE_URL_ENV = "SLEEPAGENT_RADAR_AGENT_DATABASE_URL"


class AuthorityMigrationError(RuntimeError):
    pass


class AuthorityConfigurationError(AuthorityMigrationError):
    pass


class CutoverPhaseError(AuthorityMigrationError):
    pass


class DeploymentMode(str, Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class ReadAuthorityMode(str, Enum):
    LEGACY_COMPATIBILITY = "legacy_compatibility"
    SHADOW = "shadow"
    CANONICAL = "canonical"


class DataAuthority(str, Enum):
    LEGACY_COMPATIBILITY = "legacy_compatibility"
    CANONICAL = "canonical"


class CutoverPhase(str, Enum):
    EXPANDED = "expanded"
    BACKFILLED = "backfilled"
    SHADOW_VERIFIED = "shadow_verified"
    CUTOVER = "cutover"
    ROLLBACK_REHEARSED = "rollback_rehearsed"
    ROLLED_BACK = "rolled_back"


class ShadowComparisonStatus(str, Enum):
    MATCHED = "matched"
    ACCEPTED_DIFFERENCE = "accepted_difference"
    FAILED = "failed"


@dataclass(frozen=True)
class AuthorityRuntimeConfig:
    deployment_mode: DeploymentMode
    read_authority: ReadAuthorityMode
    provider_mode: str
    database_url: str | None

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "AuthorityRuntimeConfig":
        values = os.environ if environ is None else environ
        deployment_text = values.get(
            DEPLOYMENT_MODE_ENV,
            DeploymentMode.DEVELOPMENT.value,
        ).strip().lower()
        try:
            deployment = DeploymentMode(deployment_text)
        except ValueError as exc:
            raise AuthorityConfigurationError(
                f"{DEPLOYMENT_MODE_ENV} must be development, test or production"
            ) from exc
        authority_text = values.get(READ_AUTHORITY_ENV, "").strip().lower()
        if not authority_text:
            if deployment == DeploymentMode.PRODUCTION:
                raise AuthorityConfigurationError(
                    f"{READ_AUTHORITY_ENV} is required in production"
                )
            authority_text = ReadAuthorityMode.LEGACY_COMPATIBILITY.value
        try:
            authority = ReadAuthorityMode(authority_text)
        except ValueError as exc:
            raise AuthorityConfigurationError(
                f"{READ_AUTHORITY_ENV} has an unsupported value"
            ) from exc
        provider_mode = values.get(PRODUCT_PROVIDER_MODE_ENV, "").strip().lower()
        if not provider_mode:
            provider_mode = (
                "persistent"
                if deployment == DeploymentMode.PRODUCTION
                else "disabled"
            )
        return cls(
            deployment_mode=deployment,
            read_authority=authority,
            provider_mode=provider_mode,
            database_url=values.get(RADAR_DATABASE_URL_ENV),
        )

    def validate(
        self,
        *,
        namespace: DomainNamespace,
        repository: SleepDomainRepository,
        raw_payload_policy: RawPayloadEncryptionPolicy,
    ) -> None:
        if self.provider_mode in {"fake", "replay"} and (
            self.deployment_mode == DeploymentMode.PRODUCTION
            or namespace.data_mode != DataMode.REPLAY
        ):
            raise AuthorityConfigurationError(
                "fake/replay provider modes require an explicit non-production "
                "replay namespace"
            )
        if self.deployment_mode != DeploymentMode.PRODUCTION:
            return
        if namespace.data_mode != DataMode.LIVE:
            raise AuthorityConfigurationError(
                "production authority requires a live namespace"
            )
        if repository.store.dialect != "postgres":
            raise AuthorityConfigurationError(
                "production authority requires shared PostgreSQL storage"
            )
        database_url = (self.database_url or "").strip().lower()
        if (
            not database_url
            or database_url.startswith("sqlite")
            or ":memory:" in database_url
            or "/tmp/" in database_url
        ):
            raise AuthorityConfigurationError(
                "production authority requires an explicit shared database URL"
            )
        if not raw_payload_policy.production:
            raise AuthorityConfigurationError(
                "production authority requires production raw encryption policy"
            )
        if self.provider_mode != "persistent":
            raise AuthorityConfigurationError(
                "production product data provider must be persistent"
            )

    def sha256(self) -> str:
        material = {
            "deployment_mode": self.deployment_mode.value,
            "read_authority": self.read_authority.value,
            "provider_mode": self.provider_mode,
            "database_configured": bool(self.database_url),
        }
        return hashlib.sha256(_canonical_json(material)).hexdigest()


@dataclass(frozen=True)
class CompatibilityBackfillReport:
    current_revision_count: int
    inserted_count: int
    already_present_count: int


@dataclass(frozen=True)
class ShadowReadComparison:
    comparison_id: str
    legacy_summary_id: str
    night_episode_id: str
    night_episode_revision_id: str
    status: ShadowComparisonStatus
    mismatch_fields: tuple[str, ...]
    accepted_fields: tuple[str, ...]
    critical_mismatch_count: int
    comparison_sha256: str


@dataclass(frozen=True)
class AuthorityCutoverState:
    phase: CutoverPhase
    selected_authority: DataAuthority
    configuration_sha256: str
    state_version: int
    updated_at: datetime


class CompatibilityMigrationController:
    """Runs one-way projection, shadow comparison and atomic cutover phases."""

    _NEXT_PHASES: dict[CutoverPhase | None, frozenset[CutoverPhase]] = {
        None: frozenset({CutoverPhase.EXPANDED}),
        CutoverPhase.EXPANDED: frozenset({CutoverPhase.BACKFILLED}),
        CutoverPhase.BACKFILLED: frozenset({CutoverPhase.SHADOW_VERIFIED}),
        CutoverPhase.SHADOW_VERIFIED: frozenset({CutoverPhase.CUTOVER}),
        CutoverPhase.CUTOVER: frozenset(
            {CutoverPhase.ROLLBACK_REHEARSED, CutoverPhase.ROLLED_BACK}
        ),
        CutoverPhase.ROLLBACK_REHEARSED: frozenset(
            {CutoverPhase.ROLLED_BACK}
        ),
        CutoverPhase.ROLLED_BACK: frozenset(),
    }

    def __init__(self, repository: SleepDomainRepository) -> None:
        self.repository = repository
        self.store = repository.store

    def build_current_summary(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
    ) -> tuple[RadarNightSummary, NightEpisodeRevision]:
        episode = self.repository.get_night_episode(
            namespace,
            night_episode_id=night_episode_id,
        )
        if episode is None:
            raise KeyError(f"NightEpisode not found: {night_episode_id}")
        pointer = self.repository.get_current_night_revision(
            namespace,
            night_episode_id=night_episode_id,
        )
        if pointer.current_revision_id is None:
            raise AuthorityMigrationError(
                f"NightEpisode has no committed revision: {night_episode_id}"
            )
        revision = self.repository.get_night_episode_revision(
            namespace,
            night_episode_revision_id=pointer.current_revision_id,
        )
        if revision is None:
            raise AuthorityMigrationError(
                "current NightEpisode revision pointer is dangling"
            )
        observations = self._load_revision_observations(namespace, revision)
        quality = self.repository.get_current_quality(
            namespace,
            night_episode_id=night_episode_id,
        )
        if (
            quality is not None
            and quality.source_scope.night_episode_revision_id
            != revision.night_episode_revision_id
        ):
            quality = None
        device_id = episode.binding_references[0].device_id
        stage_intervals = [
            item.payload
            for item in observations
            if isinstance(item.payload, SleepStageIntervalPayload)
            and item.payload.start_at is not None
            and item.payload.end_at is not None
        ]
        sleep_intervals = [
            item
            for item in stage_intervals
            if item.stage
            in {SleepStageState.DEEP, SleepStageState.LIGHT, SleepStageState.REM}
        ]
        sleep_start = min(
            (item.start_at for item in sleep_intervals if item.start_at),
            default=None,
        )
        sleep_end = max(
            (item.end_at for item in sleep_intervals if item.end_at),
            default=None,
        )
        stage_minutes = sum(
            (
                (item.end_at - item.start_at).total_seconds() / 60.0
                for item in sleep_intervals
                if item.start_at is not None and item.end_at is not None
            ),
            0.0,
        )
        vendor_total = _vendor_numeric_metric(
            observations,
            {
                "total_sleep_minutes",
                "total_sleep_time",
                "sleep_duration_minutes",
            },
        )
        sleep_score = _vendor_numeric_metric(
            observations,
            {"sleep_score", "score"},
        )
        total_sleep_minutes = (
            vendor_total if vendor_total is not None else stage_minutes or None
        )
        latest_connectivity = max(
            (
                item
                for item in observations
                if isinstance(item.payload, DeviceConnectivityPayload)
            ),
            key=_observation_at,
            default=None,
        )
        device_status = RadarDeviceStatus.UNKNOWN
        if latest_connectivity is not None:
            if latest_connectivity.payload.state == DeviceConnectivityState.ONLINE:
                device_status = RadarDeviceStatus.ONLINE
            elif (
                latest_connectivity.payload.state
                == DeviceConnectivityState.OFFLINE
            ):
                device_status = RadarDeviceStatus.OFFLINE
        if quality is None:
            coverage = 0.0
            quality_status = RadarDataQualityStatus.UNUSABLE
            confidence = "not_interpretable"
            health_allowed = False
            invalid_count = 0
            quality_reasons = ["current_revision_quality_unavailable"]
        else:
            coverage = quality.coverage_ratio
            quality_status = {
                QualityState.SUFFICIENT: RadarDataQualityStatus.GOOD,
                QualityState.PARTIAL: RadarDataQualityStatus.PARTIAL,
                QualityState.DATA_INSUFFICIENT: RadarDataQualityStatus.UNUSABLE,
            }[quality.quality_state]
            confidence = (
                "normal"
                if quality.quality_state == QualityState.SUFFICIENT
                else (
                    "low_confidence"
                    if quality.quality_state == QualityState.PARTIAL
                    else "not_interpretable"
                )
            )
            health_allowed = quality.quality_state == QualityState.SUFFICIENT
            invalid_count = quality.invalid_observation_count
            quality_reasons = list(quality.reason_codes)
        missing = [
            item
            for item in observations
            if isinstance(item.payload, MissingIntervalPayload)
        ]
        out_of_bed_count = sum(
            1
            for item in observations
            if (
                isinstance(item.payload, BedExitPayload)
                and item.payload.kind in {BedExitKind.BED_EXIT, BedExitKind.GET_UP}
            )
            or (
                isinstance(item.payload, BedPresencePayload)
                and item.payload.state == BedPresenceState.OUT_OF_BED
            )
        )
        movement_count = sum(
            1 for item in observations if isinstance(item.payload, MovementPayload)
        )
        summary = RadarNightSummary(
            radar_device_id=device_id,
            subject_id=episode.subject_id,
            night_of=episode.local_sleep_date,
            timezone_name=episode.timezone_name,
            night_boundary_start_at=episode.collection_start_at,
            night_boundary_end_at=episode.collection_end_at,
            device_status=device_status,
            sleep_start_at=sleep_start,
            sleep_end_at=sleep_end,
            total_sleep_minutes=total_sleep_minutes,
            sleep_score=sleep_score,
            out_of_bed_count=out_of_bed_count,
            movement_count=movement_count,
            data_coverage_ratio=coverage,
            data_quality_status=quality_status,
            confidence_label=confidence,
            health_conclusion_allowed=health_allowed,
            invalid_reading_count=invalid_count,
            abnormal_reading_count=0,
            missing_intervals=[
                (
                    f"{item.payload.interval_start_at.isoformat()}/"
                    f"{item.payload.interval_end_at.isoformat()}"
                    if item.payload.interval_start_at is not None
                    and item.payload.interval_end_at is not None
                    else f"observation:{item.observation_id}"
                )
                for item in missing
            ],
            quality_reasons=quality_reasons,
            blocked_reasons=(
                [] if health_allowed else ["deterministic_quality_not_sufficient"]
            ),
            caveats=[
                "Compatibility projection of the committed canonical revision."
            ],
            explainable_metrics={
                "projection_version": COMPATIBILITY_PROJECTION_VERSION,
                "night_episode_revision_number": revision.revision_number,
            },
            source_snapshot_ids=[],
            source_raw_event_ids=[],
            source_report_ref=(
                revision.source_report_references[0]
                if revision.source_report_references
                else None
            ),
            generated_at=revision.created_at,
        )
        return summary, revision

    def project_current(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
        overwrite: bool,
    ) -> bool:
        summary, revision = self.build_current_summary(
            namespace,
            night_episode_id=night_episode_id,
        )
        self._ensure_legacy_identity_rows(summary)
        summary_id = _summary_id(summary)
        sql = """
            INSERT INTO radar_night_summaries (
              summary_id, radar_device_id, subject_id, night_of,
              data_coverage_ratio, summary_json, generated_at,
              source_night_episode_revision_id,
              compatibility_projection_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        if overwrite:
            sql += """
                ON CONFLICT(summary_id) DO UPDATE SET
                  radar_device_id = excluded.radar_device_id,
                  subject_id = excluded.subject_id,
                  night_of = excluded.night_of,
                  data_coverage_ratio = excluded.data_coverage_ratio,
                  summary_json = excluded.summary_json,
                  generated_at = excluded.generated_at,
                  source_night_episode_revision_id =
                    excluded.source_night_episode_revision_id,
                  compatibility_projection_version =
                    excluded.compatibility_projection_version
            """
        else:
            sql += " ON CONFLICT(summary_id) DO NOTHING"
        with self.store.transaction_lock:
            cursor = self.store.connection.execute(
                _sql(self.store, sql),
                (
                    summary_id,
                    summary.radar_device_id,
                    summary.subject_id,
                    summary.night_of.isoformat(),
                    summary.data_coverage_ratio,
                    summary.model_dump_json(),
                    _dt(summary.generated_at),
                    revision.night_episode_revision_id,
                    COMPATIBILITY_PROJECTION_VERSION,
                ),
            )
            self.store.connection.commit()
            return cursor.rowcount == 1

    def backfill_current_revisions(
        self,
        namespace: DomainNamespace,
    ) -> CompatibilityBackfillReport:
        episodes = self._current_episodes(namespace)
        inserted = 0
        for episode in episodes:
            inserted += self.project_current(
                namespace,
                night_episode_id=episode.night_episode_id,
                overwrite=False,
            )
        return CompatibilityBackfillReport(
            current_revision_count=len(episodes),
            inserted_count=inserted,
            already_present_count=len(episodes) - inserted,
        )

    def finalize_current_compatibility(
        self,
        namespace: DomainNamespace,
    ) -> CompatibilityBackfillReport:
        """Replace legacy rows only after shadow verification, never on commit."""

        state = self.get_state(namespace)
        if state is None or state.phase != CutoverPhase.SHADOW_VERIFIED:
            raise CutoverPhaseError(
                "compatibility finalization requires shadow_verified phase"
            )
        episodes = self._current_episodes(namespace)
        updated = 0
        for episode in episodes:
            updated += self.project_current(
                namespace,
                night_episode_id=episode.night_episode_id,
                overwrite=True,
            )
        return CompatibilityBackfillReport(
            current_revision_count=len(episodes),
            inserted_count=updated,
            already_present_count=len(episodes) - updated,
        )

    def compare_current(
        self,
        namespace: DomainNamespace,
        *,
        night_episode_id: str,
        legacy_summary_id: str | None = None,
        accepted_difference_fields: Iterable[str] = (),
        compared_at: datetime | None = None,
    ) -> ShadowReadComparison:
        compared_at = compared_at or datetime.now(tz=UTC)
        _require_aware(compared_at, "compared_at")
        projected, revision = self.build_current_summary(
            namespace,
            night_episode_id=night_episode_id,
        )
        legacy_summary_id = legacy_summary_id or _summary_id(projected)
        with self.store.transaction_lock:
            row = self.store.connection.execute(
                _sql(
                    self.store,
                    """
                    SELECT summary_json
                    FROM radar_night_summaries
                    WHERE summary_id = ?
                    """,
                ),
                (legacy_summary_id,),
            ).fetchone()
        if row is None:
            raise AuthorityMigrationError(
                f"legacy compatibility summary not found: {legacy_summary_id}"
            )
        legacy = RadarNightSummary.model_validate_json(_database_json_text(row[0]))
        comparisons = _compare_summary_fields(legacy, projected)
        mismatches = tuple(
            sorted(name for name, matched in comparisons.items() if not matched)
        )
        allowed = frozenset(accepted_difference_fields)
        accepted = tuple(sorted(field for field in mismatches if field in allowed))
        critical = tuple(field for field in mismatches if field not in allowed)
        status = (
            ShadowComparisonStatus.MATCHED
            if not mismatches
            else (
                ShadowComparisonStatus.ACCEPTED_DIFFERENCE
                if not critical
                else ShadowComparisonStatus.FAILED
            )
        )
        comparison_material = {
            "legacy_summary_id": legacy_summary_id,
            "night_episode_revision_id": revision.night_episode_revision_id,
            "legacy_summary_sha256": _model_sha256(legacy),
            "canonical_projection_sha256": _model_sha256(projected),
            "field_matches": comparisons,
            "accepted_fields": accepted,
        }
        comparison_sha256 = hashlib.sha256(
            _canonical_json(comparison_material)
        ).hexdigest()
        comparison_id = _stable_id(
            "shadow-comparison",
            namespace.namespace_id,
            namespace.data_mode.value,
            legacy_summary_id,
            revision.night_episode_revision_id,
            comparison_sha256,
        )
        privacy_minimized = {
            "schema_version": "shadow_read_comparison.v1",
            "mismatch_fields": mismatches,
            "accepted_fields": accepted,
            "field_matches": comparisons,
            "values_persisted": False,
        }
        with self.store.transaction_lock:
            self.store.connection.execute(
                _sql(
                    self.store,
                    """
                    INSERT INTO sleep_domain_shadow_read_comparisons (
                      comparison_id, namespace_id, data_mode,
                      legacy_summary_id, night_episode_id,
                      night_episode_revision_id, status,
                      critical_mismatch_count, comparison_sha256,
                      comparison_json, compared_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(comparison_id) DO NOTHING
                    """,
                ),
                (
                    comparison_id,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    legacy_summary_id,
                    night_episode_id,
                    revision.night_episode_revision_id,
                    status.value,
                    len(critical),
                    comparison_sha256,
                    json.dumps(
                        privacy_minimized,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    _dt(compared_at),
                ),
            )
            self.store.connection.commit()
        return ShadowReadComparison(
            comparison_id=comparison_id,
            legacy_summary_id=legacy_summary_id,
            night_episode_id=night_episode_id,
            night_episode_revision_id=revision.night_episode_revision_id,
            status=status,
            mismatch_fields=mismatches,
            accepted_fields=accepted,
            critical_mismatch_count=len(critical),
            comparison_sha256=comparison_sha256,
        )

    def advance_phase(
        self,
        namespace: DomainNamespace,
        *,
        phase: CutoverPhase,
        configuration: AuthorityRuntimeConfig,
        actor_id: str,
        reason_code: str,
        occurred_at: datetime | None = None,
    ) -> AuthorityCutoverState:
        occurred_at = occurred_at or datetime.now(tz=UTC)
        _require_aware(occurred_at, "occurred_at")
        if not actor_id or not reason_code:
            raise ValueError("actor_id and reason_code are required")
        configuration.validate(
            namespace=namespace,
            repository=self.repository,
            raw_payload_policy=self.repository.raw_payload_policy,
        )
        with self.store.transaction_lock:
            cursor = self.store.connection.cursor()
            try:
                if self.store.dialect == "sqlite":
                    cursor.execute("BEGIN IMMEDIATE")
                current = self._state_for_update(cursor, namespace)
                current_phase = None if current is None else current.phase
                if phase not in self._NEXT_PHASES[current_phase]:
                    raise CutoverPhaseError(
                        f"cannot advance from "
                        f"{None if current_phase is None else current_phase.value} "
                        f"to {phase.value}"
                    )
                self._validate_phase_gate(
                    cursor,
                    namespace,
                    phase=phase,
                    configuration=configuration,
                )
                previous_authority = (
                    DataAuthority.LEGACY_COMPATIBILITY
                    if current is None
                    else current.selected_authority
                )
                selected = (
                    DataAuthority.CANONICAL
                    if phase
                    in {
                        CutoverPhase.CUTOVER,
                        CutoverPhase.ROLLBACK_REHEARSED,
                    }
                    else (
                        DataAuthority.LEGACY_COMPATIBILITY
                        if phase == CutoverPhase.ROLLED_BACK
                        else previous_authority
                    )
                )
                config_sha = configuration.sha256()
                state_version = 1 if current is None else current.state_version + 1
                event_detail = {
                    "schema_version": "authority_cutover_event.v1",
                    "state_version": state_version,
                    "read_authority": configuration.read_authority.value,
                    "rollback_probe": (
                        {
                            "selected_during_probe": (
                                DataAuthority.LEGACY_COMPATIBILITY.value
                            ),
                            "restored_authority": DataAuthority.CANONICAL.value,
                            "destructive_change": False,
                        }
                        if phase == CutoverPhase.ROLLBACK_REHEARSED
                        else None
                    ),
                }
                event_id = _stable_id(
                    "cutover-event",
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    phase.value,
                    str(state_version),
                    config_sha,
                )
                cursor.execute(
                    _sql(
                        self.store,
                        """
                        INSERT INTO sleep_domain_authority_cutover_events (
                          cutover_event_id, namespace_id, data_mode, phase,
                          previous_authority, selected_authority,
                          configuration_sha256, actor_id, reason_code,
                          event_json, occurred_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                    ),
                    (
                        event_id,
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        phase.value,
                        previous_authority.value,
                        selected.value,
                        config_sha,
                        actor_id,
                        reason_code,
                        json.dumps(
                            event_detail,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        _dt(occurred_at),
                    ),
                )
                cursor.execute(
                    _sql(
                        self.store,
                        """
                        INSERT INTO sleep_domain_authority_cutover_state (
                          namespace_id, data_mode, phase, selected_authority,
                          configuration_sha256, state_version, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(namespace_id, data_mode) DO UPDATE SET
                          phase = excluded.phase,
                          selected_authority = excluded.selected_authority,
                          configuration_sha256 =
                            excluded.configuration_sha256,
                          state_version = excluded.state_version,
                          updated_at = excluded.updated_at
                        """,
                    ),
                    (
                        namespace.namespace_id,
                        namespace.data_mode.value,
                        phase.value,
                        selected.value,
                        config_sha,
                        state_version,
                        _dt(occurred_at),
                    ),
                )
                self.store.connection.commit()
                return AuthorityCutoverState(
                    phase=phase,
                    selected_authority=selected,
                    configuration_sha256=config_sha,
                    state_version=state_version,
                    updated_at=occurred_at,
                )
            except Exception:
                self.store.connection.rollback()
                raise
            finally:
                cursor.close()

    def get_state(
        self,
        namespace: DomainNamespace,
    ) -> AuthorityCutoverState | None:
        with self.store.transaction_lock:
            cursor = self.store.connection.cursor()
            try:
                return self._state_for_update(cursor, namespace, lock=False)
            finally:
                cursor.close()

    def _state_for_update(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        *,
        lock: bool = True,
    ) -> AuthorityCutoverState | None:
        suffix = (
            " FOR UPDATE"
            if lock and self.store.dialect == "postgres"
            else ""
        )
        row = cursor.execute(
            _sql(
                self.store,
                """
                SELECT phase, selected_authority, configuration_sha256,
                       state_version, updated_at
                FROM sleep_domain_authority_cutover_state
                WHERE namespace_id = ? AND data_mode = ?
                """
                + suffix,
            ),
            (namespace.namespace_id, namespace.data_mode.value),
        ).fetchone()
        if row is None:
            return None
        return AuthorityCutoverState(
            phase=CutoverPhase(str(row[0])),
            selected_authority=DataAuthority(str(row[1])),
            configuration_sha256=str(row[2]),
            state_version=int(row[3]),
            updated_at=_parse_datetime(row[4]),
        )

    def _validate_phase_gate(
        self,
        cursor: Any,
        namespace: DomainNamespace,
        *,
        phase: CutoverPhase,
        configuration: AuthorityRuntimeConfig,
    ) -> None:
        if phase == CutoverPhase.BACKFILLED:
            episode_row = cursor.execute(
                _sql(
                    self.store,
                    """
                    SELECT COUNT(*)
                    FROM sleep_domain_night_episodes
                    WHERE namespace_id = ? AND data_mode = ?
                      AND current_revision_id IS NOT NULL
                    """,
                ),
                (namespace.namespace_id, namespace.data_mode.value),
            ).fetchone()
            import_row = cursor.execute(
                _sql(
                    self.store,
                    """
                    SELECT COUNT(*)
                    FROM sleep_domain_legacy_import_runs
                    WHERE confirmed_namespace_id = ?
                      AND confirmed_data_mode = ?
                      AND status = 'succeeded'
                    """,
                ),
                (namespace.namespace_id, namespace.data_mode.value),
            ).fetchone()
            if (
                episode_row is None
                or int(episode_row[0]) < 1
                or import_row is None
                or int(import_row[0]) < 1
            ):
                raise CutoverPhaseError(
                    "backfilled phase requires populated current revisions "
                    "and a succeeded legacy import (including a zero-row import)"
                )
        if phase == CutoverPhase.SHADOW_VERIFIED:
            row = cursor.execute(
                _sql(
                    self.store,
                    """
                    SELECT
                      COUNT(DISTINCT episodes.current_revision_id),
                      COUNT(DISTINCT comparisons.night_episode_revision_id),
                      SUM(
                        CASE WHEN comparisons.status = 'failed' THEN 1 ELSE 0 END
                      )
                    FROM sleep_domain_night_episodes AS episodes
                    LEFT JOIN sleep_domain_shadow_read_comparisons AS comparisons
                      ON comparisons.namespace_id = episodes.namespace_id
                     AND comparisons.data_mode = episodes.data_mode
                     AND comparisons.night_episode_revision_id =
                         episodes.current_revision_id
                    WHERE episodes.namespace_id = ?
                      AND episodes.data_mode = ?
                      AND episodes.current_revision_id IS NOT NULL
                    """,
                ),
                (namespace.namespace_id, namespace.data_mode.value),
            ).fetchone()
            if (
                row is None
                or int(row[0]) < 1
                or int(row[1]) != int(row[0])
                or int(row[2] or 0) != 0
            ):
                raise CutoverPhaseError(
                    "shadow_verified requires a current-revision comparison "
                    "for every populated episode and zero failures"
                )
        if (
            phase in {CutoverPhase.CUTOVER, CutoverPhase.ROLLBACK_REHEARSED}
            and configuration.read_authority != ReadAuthorityMode.CANONICAL
        ):
            raise CutoverPhaseError(
                f"{phase.value} requires canonical read-authority configuration"
            )
        if phase == CutoverPhase.CUTOVER:
            row = cursor.execute(
                _sql(
                    self.store,
                    """
                    SELECT
                      COUNT(DISTINCT episodes.current_revision_id),
                      COUNT(DISTINCT summaries.source_night_episode_revision_id)
                    FROM sleep_domain_night_episodes AS episodes
                    LEFT JOIN radar_night_summaries AS summaries
                      ON summaries.source_night_episode_revision_id =
                         episodes.current_revision_id
                     AND summaries.compatibility_projection_version = ?
                    WHERE episodes.namespace_id = ?
                      AND episodes.data_mode = ?
                      AND episodes.current_revision_id IS NOT NULL
                    """,
                ),
                (
                    COMPATIBILITY_PROJECTION_VERSION,
                    namespace.namespace_id,
                    namespace.data_mode.value,
                ),
            ).fetchone()
            if (
                row is None
                or int(row[0]) < 1
                or int(row[1]) != int(row[0])
            ):
                raise CutoverPhaseError(
                    "cutover requires a finalized compatibility projection "
                    "for every current revision"
                )
        if phase == CutoverPhase.ROLLBACK_REHEARSED:
            rows = cursor.execute(
                _sql(
                    self.store,
                    """
                    SELECT summaries.summary_json, episodes.episode_json
                    FROM sleep_domain_night_episodes AS episodes
                    JOIN radar_night_summaries AS summaries
                      ON summaries.source_night_episode_revision_id =
                         episodes.current_revision_id
                    WHERE episodes.namespace_id = ?
                      AND episodes.data_mode = ?
                      AND summaries.compatibility_projection_version = ?
                    """,
                ),
                (
                    namespace.namespace_id,
                    namespace.data_mode.value,
                    COMPATIBILITY_PROJECTION_VERSION,
                ),
            ).fetchall()
            if not rows:
                raise CutoverPhaseError(
                    "rollback rehearsal requires canonical compatibility rows"
                )
            for summary_json, episode_json in rows:
                summary = RadarNightSummary.model_validate_json(
                    _database_json_text(summary_json)
                )
                episode = NightEpisode.model_validate_json(
                    _database_json_text(episode_json)
                )
                if (
                    summary.subject_id != episode.subject_id
                    or summary.night_of != episode.local_sleep_date
                ):
                    raise CutoverPhaseError(
                        "rollback compatibility read is outside current "
                        "episode identity"
                    )

    def _current_episodes(
        self,
        namespace: DomainNamespace,
    ) -> tuple[NightEpisode, ...]:
        with self.store.transaction_lock:
            rows = self.store.connection.execute(
                _sql(
                    self.store,
                    """
                    SELECT episode_json
                    FROM sleep_domain_night_episodes
                    WHERE namespace_id = ? AND data_mode = ?
                      AND current_revision_id IS NOT NULL
                    ORDER BY night_episode_id
                    """,
                ),
                (namespace.namespace_id, namespace.data_mode.value),
            ).fetchall()
        return tuple(
            NightEpisode.model_validate_json(_database_json_text(row[0]))
            for row in rows
        )

    def _load_revision_observations(
        self,
        namespace: DomainNamespace,
        revision: NightEpisodeRevision,
    ) -> tuple[SleepObservation, ...]:
        result: list[SleepObservation] = []
        for observation_id in revision.observation_ids:
            item = self.repository.get_observation(
                namespace,
                observation_id=observation_id,
            )
            if item is None:
                raise AuthorityMigrationError(
                    f"revision observation missing: {observation_id}"
                )
            if (
                item.subject_id != revision.subject_id
                or item.data_mode != namespace.data_mode
            ):
                raise AuthorityMigrationError(
                    "revision contains cross-subject or cross-mode observation"
                )
            result.append(item)
        return tuple(result)

    def _ensure_legacy_identity_rows(self, summary: RadarNightSummary) -> None:
        now = summary.generated_at
        subject = RadarSubject(
            subject_id=summary.subject_id or "unbound",
            display_name=summary.subject_id or "Unbound",
            timezone_name=summary.timezone_name,
            created_at=now,
            updated_at=now,
        )
        device = RadarDevice(
            radar_device_id=summary.radar_device_id,
            display_name=summary.radar_device_id,
            provider="canonical-compatibility",
            status=summary.device_status,
            bound_subject_id=summary.subject_id,
            timezone_name=summary.timezone_name,
            registered_at=now,
            updated_at=now,
        )
        with self.store.transaction_lock:
            cursor = self.store.connection.cursor()
            try:
                existing_device = cursor.execute(
                    _sql(
                        self.store,
                        """
                        SELECT subject_id
                        FROM radar_devices
                        WHERE radar_device_id = ?
                        """,
                    ),
                    (device.radar_device_id,),
                ).fetchone()
                if (
                    existing_device is not None
                    and existing_device[0] is not None
                    and str(existing_device[0]) != summary.subject_id
                ):
                    raise AuthorityMigrationError(
                        "legacy device identity is bound to another subject"
                    )
                if summary.subject_id:
                    cursor.execute(
                        _sql(
                            self.store,
                            """
                            INSERT INTO radar_subjects (
                              subject_id, display_name, timezone_name,
                              subject_json, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(subject_id) DO NOTHING
                            """,
                        ),
                        (
                            subject.subject_id,
                            subject.display_name,
                            subject.timezone_name,
                            subject.model_dump_json(),
                            _dt(subject.created_at),
                            _dt(subject.updated_at),
                        ),
                    )
                cursor.execute(
                    _sql(
                        self.store,
                        """
                        INSERT INTO radar_devices (
                          radar_device_id, subject_id, provider, status,
                          device_json, registered_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(radar_device_id) DO NOTHING
                        """,
                    ),
                    (
                        device.radar_device_id,
                        device.bound_subject_id,
                        device.provider,
                        device.status.value,
                        device.model_dump_json(),
                        _dt(device.registered_at),
                        _dt(device.updated_at),
                    ),
                )
                self.store.connection.commit()
            except Exception:
                self.store.connection.rollback()
                raise
            finally:
                cursor.close()


def _vendor_numeric_metric(
    observations: tuple[SleepObservation, ...],
    names: set[str],
) -> float | None:
    values: list[tuple[datetime, float]] = []
    for observation in observations:
        payload = observation.payload
        if (
            not isinstance(payload, VendorSleepProfileMetricPayload)
            or payload.metric_name.strip().lower() not in names
            or payload.value_state != AvailabilityState.KNOWN
            or isinstance(payload.value, bool)
            or not isinstance(payload.value, (int, float))
        ):
            continue
        value = float(payload.value)
        if payload.unit and payload.unit.lower() in {"seconds", "second", "s"}:
            value /= 60.0
        values.append((_observation_at(observation), value))
    return max(values, key=lambda item: item[0])[1] if values else None


def _observation_at(observation: SleepObservation) -> datetime:
    return (
        observation.measurement_at
        or observation.event_occurred_at
        or observation.received_at
    )


def _compare_summary_fields(
    legacy: RadarNightSummary,
    projected: RadarNightSummary,
) -> dict[str, bool]:
    return {
        "subject_id": legacy.subject_id == projected.subject_id,
        "night_of": legacy.night_of == projected.night_of,
        "timezone_name": legacy.timezone_name == projected.timezone_name,
        "sleep_start_at": _datetime_close(
            legacy.sleep_start_at,
            projected.sleep_start_at,
            seconds=60,
        ),
        "sleep_end_at": _datetime_close(
            legacy.sleep_end_at,
            projected.sleep_end_at,
            seconds=60,
        ),
        "total_sleep_minutes": _number_close(
            legacy.total_sleep_minutes,
            projected.total_sleep_minutes,
            tolerance=1.0,
        ),
        "sleep_score": _number_close(
            legacy.sleep_score,
            projected.sleep_score,
            tolerance=1.0,
        ),
        "out_of_bed_count": (
            legacy.out_of_bed_count == projected.out_of_bed_count
        ),
        "movement_count": legacy.movement_count == projected.movement_count,
        "data_coverage_ratio": _number_close(
            legacy.data_coverage_ratio,
            projected.data_coverage_ratio,
            tolerance=0.01,
        ),
        "data_quality_status": (
            legacy.data_quality_status == projected.data_quality_status
        ),
    }


def _number_close(
    first: float | None,
    second: float | None,
    *,
    tolerance: float,
) -> bool:
    if first is None or second is None:
        return first is second
    return abs(first - second) <= tolerance


def _datetime_close(
    first: datetime | None,
    second: datetime | None,
    *,
    seconds: float,
) -> bool:
    if first is None or second is None:
        return first is second
    return abs((first - second).total_seconds()) <= seconds


def _summary_id(summary: RadarNightSummary) -> str:
    return f"{summary.radar_device_id}:{summary.night_of.isoformat()}"


def _model_sha256(model: RadarNightSummary) -> str:
    return hashlib.sha256(
        _canonical_json(model.model_dump(mode="json"))
    ).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:" + hashlib.sha256(
        "|".join((prefix, *parts)).encode("utf-8")
    ).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _database_json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sql(store: Any, sql: str) -> str:
    return sql if store.dialect == "sqlite" else sql.replace("?", "%s")


def _dt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthorityMigrationError("stored cutover timestamp is naive")
    return parsed.astimezone(UTC)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


__all__ = [
    "AuthorityConfigurationError",
    "AuthorityCutoverState",
    "AuthorityMigrationError",
    "AuthorityRuntimeConfig",
    "COMPATIBILITY_PROJECTION_VERSION",
    "CompatibilityBackfillReport",
    "CompatibilityMigrationController",
    "CutoverPhase",
    "CutoverPhaseError",
    "DataAuthority",
    "DeploymentMode",
    "ReadAuthorityMode",
    "ShadowComparisonStatus",
    "ShadowReadComparison",
]
