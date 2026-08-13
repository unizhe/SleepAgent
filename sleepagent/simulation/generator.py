# 本模块负责可复现模拟数据与回放契约，不参与生产事实判定。
"""Deterministic canonical observation generation from strict replay recipes."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from sleepagent.simulation.contracts import (
    CorrectionOverlay,
    GeneratedCorrectionLineage,
    GeneratedReplay,
    GeneratedReplayManifest,
    LateReportOverlay,
    LowCoverageOverlay,
    MissingMetricOverlay,
    NightRecipe,
    OfflineOverlay,
    ReplayScenario,
    VendorAlertOverlay,
)
from sleepagent.domain.contracts import (
    AlgorithmVersionValue,
    AlertLifecycleState,
    AlertSeverity,
    AvailabilityState,
    BedExitKind,
    BedExitPayload,
    BedPresencePayload,
    BedPresenceState,
    CalibrationValue,
    ConfidenceValue,
    DataMode,
    DeviceConnectivityPayload,
    DeviceConnectivityState,
    HeartRatePayload,
    MissingIntervalPayload,
    MissingState,
    MovementPayload,
    ObservationPayload,
    ObservationProvenance,
    ObservationQuality,
    ObservationType,
    RespiratoryRatePayload,
    SleepObservation,
    SleepStageIntervalPayload,
    SleepStageState,
    SourceKind,
    TimezoneStatus,
    VendorAlertPayload,
    VendorSleepProfileMetricPayload,
)


class ReplayFixtureError(ValueError):
    """A fixture path or document violates the replay-only loading boundary."""


@dataclass(frozen=True)
class _NightWindow:
    recipe: NightRecipe
    starts_at: datetime
    ends_at: datetime

    @property
    def duration_minutes(self) -> int:
        return int((self.ends_at - self.starts_at).total_seconds() // 60)


_STAGE_CYCLE: tuple[tuple[SleepStageState, int], ...] = (
    (SleepStageState.AWAKE, 15),
    (SleepStageState.LIGHT, 70),
    (SleepStageState.DEEP, 45),
    (SleepStageState.LIGHT, 55),
    (SleepStageState.REM, 35),
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _stable_unit_interval(*parts: object) -> float:
    material = "\x1f".join(str(part) for part in parts).encode("utf-8")
    integer = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    return integer / float(2**64 - 1)


def _source_stub(identity_hash: str) -> bytes:
    return f"canonical-replay-source:{identity_hash}".encode("ascii")


def canonical_replay_source_bytes(observation: SleepObservation) -> bytes:
    """Reconstruct the opaque synthetic source stub for repository ingestion.

    It is deliberately not a vendor payload and contains no health values.  It
    exists so a canonical replay observation can traverse the real encrypted
    raw-inbox foreign-key boundary without pretending to be signed hardware
    evidence.
    """

    prefix = "simsource:"
    if not observation.source_key.startswith(prefix):
        raise ValueError("observation is not from the canonical replay generator")
    identity_hash = observation.source_key.removeprefix(prefix)
    if len(identity_hash) != 64 or any(
        character not in "0123456789abcdef" for character in identity_hash
    ):
        raise ValueError("canonical replay source key is malformed")
    payload = _source_stub(identity_hash)
    if hashlib.sha256(payload).hexdigest() != observation.provenance.raw_payload_sha256:
        raise ValueError("canonical replay source stub does not match provenance")
    return payload


def _component_version(scenario: ReplayScenario, component: str) -> str:
    return next(
        pin.version
        for pin in scenario.environment.component_pins
        if pin.component == component
    )


def _generation_input_sha256(scenario: ReplayScenario) -> str:
    """Hash only identity-bearing generation inputs, not fixture prose/tags."""

    return _sha256(
        {
            "scenario_id": scenario.scenario_id,
            "environment": scenario.environment.model_dump(mode="json"),
            "identity": scenario.identity.model_dump(mode="json"),
            "nights": [night.model_dump(mode="json") for night in scenario.nights],
            "overlays": [
                overlay.model_dump(mode="json") for overlay in scenario.overlays
            ],
        }
    )


def load_replay_scenario(path: str | Path) -> ReplayScenario:
    """Load only ``scenario.json``; action/oracle documents never enter runtime.

    The explicit basename gate is part of the physical oracle boundary.  A
    caller wanting actions or expected values must use verifier-owned code.
    """

    fixture_path = Path(path)
    if fixture_path.name != "scenario.json":
        raise ReplayFixtureError(
            "runtime loader accepts scenario.json only; actions/oracles are isolated"
        )
    if fixture_path.is_symlink():
        raise ReplayFixtureError("scenario fixture cannot be a symlink")
    try:
        raw = fixture_path.read_bytes()
    except OSError as exc:
        raise ReplayFixtureError(f"unable to read scenario fixture: {exc}") from exc
    try:
        return ReplayScenario.model_validate_json(raw)
    except ValidationError as exc:
        raise ReplayFixtureError(f"invalid replay scenario: {exc}") from exc


def load_packaged_scenario(scenario_id: str) -> ReplayScenario:
    """Load a checked-in scenario by a non-path identifier."""

    if not scenario_id or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
        for character in scenario_id
    ):
        raise ReplayFixtureError("scenario_id must be a lowercase slug")
    fixture = resources.files("sleepagent.simulation.fixtures").joinpath(
        "scenarios", scenario_id, "scenario.json"
    )
    try:
        raw = fixture.read_bytes()
    except (FileNotFoundError, OSError) as exc:
        raise ReplayFixtureError(f"unknown packaged scenario: {scenario_id}") from exc
    try:
        scenario = ReplayScenario.model_validate_json(raw)
    except ValidationError as exc:
        raise ReplayFixtureError(
            f"invalid packaged replay scenario {scenario_id}: {exc}"
        ) from exc
    if scenario.scenario_id != scenario_id:
        raise ReplayFixtureError("packaged scenario ID does not match its directory")
    return scenario


class CanonicalReplayGenerator:
    """Generate stable ``SleepObservation`` sequences without business verdicts."""

    def generate(self, scenario: ReplayScenario) -> GeneratedReplay:
        scenario_payload = scenario.model_dump(mode="json")
        scenario_sha256 = _sha256(scenario_payload)
        generation_input_sha256 = _generation_input_sha256(scenario)
        generated: list[SleepObservation] = []
        for recipe in scenario.nights:
            window = self._night_window(scenario, recipe)
            observations = self._generate_normal_night(
                scenario,
                window,
                scenario_sha256=generation_input_sha256,
            )
            observations = self._apply_overlays(
                scenario,
                window,
                observations,
                scenario_sha256=generation_input_sha256,
            )
            generated.extend(observations)

        generated.sort(key=lambda item: (item.received_at, item.observation_id))
        serialized = [item.model_dump(mode="json") for item in generated]
        counts = Counter(item.observation_type for item in generated)
        event_times = [self._event_time(item) for item in generated]
        correction_lineage = self._correction_lineage(scenario, generated)
        manifest = GeneratedReplayManifest(
            scenario_id=scenario.scenario_id,
            generator_version=scenario.environment.generator_version,
            scenario_sha256=scenario_sha256,
            observation_sequence_sha256=_sha256(serialized),
            observation_count=len(generated),
            night_count=len(scenario.nights),
            counts_by_type=dict(sorted(counts.items(), key=lambda item: item[0].value)),
            first_event_at=min(event_times),
            last_event_at=max(event_times),
            correction_lineage=correction_lineage,
        )
        return GeneratedReplay(manifest=manifest, observations=tuple(generated))

    def _night_window(
        self,
        scenario: ReplayScenario,
        recipe: NightRecipe,
    ) -> _NightWindow:
        zone = ZoneInfo(scenario.environment.timezone_name)
        starts_at = datetime.combine(recipe.sleep_day, recipe.bedtime_local, zone)
        ends_at = datetime.combine(recipe.sleep_day, recipe.wake_time_local, zone)
        if ends_at <= starts_at:
            ends_at += timedelta(days=1)
        if ends_at - starts_at < timedelta(hours=2):
            raise ValueError("night recipe must span at least two hours")
        if any(
            offset >= int((ends_at - starts_at).total_seconds() // 60)
            for offset in recipe.bed_exit_offsets_minutes
        ):
            raise ValueError("bed exit offset must fall inside the night window")
        return _NightWindow(recipe=recipe, starts_at=starts_at, ends_at=ends_at)

    def _generate_normal_night(
        self,
        scenario: ReplayScenario,
        window: _NightWindow,
        *,
        scenario_sha256: str,
    ) -> list[SleepObservation]:
        observations: list[SleepObservation] = []
        add = observations.append
        add(
            self._observation(
                scenario,
                window,
                payload=DeviceConnectivityPayload(
                    state=DeviceConnectivityState.ONLINE
                ),
                event_at=window.starts_at - timedelta(minutes=5),
                ordinal="connectivity-start",
                scenario_sha256=scenario_sha256,
            )
        )
        add(
            self._observation(
                scenario,
                window,
                payload=BedPresencePayload(state=BedPresenceState.IN_BED),
                event_at=window.starts_at,
                ordinal="bed-in",
                scenario_sha256=scenario_sha256,
            )
        )

        sample_at = window.starts_at + timedelta(
            minutes=window.recipe.cadence_minutes / 2
        )
        sample_index = 0
        while sample_at < window.ends_at:
            sample_index += 1
            add(
                self._observation(
                    scenario,
                    window,
                    payload=HeartRatePayload(
                        value=self._jittered_value(
                            scenario,
                            window,
                            sample_index,
                            "heart-rate",
                            center=window.recipe.heart_rate_center,
                            amplitude=4.0,
                        )
                    ),
                    event_at=sample_at,
                    ordinal=f"hr-{sample_index:03d}",
                    scenario_sha256=scenario_sha256,
                )
            )
            add(
                self._observation(
                    scenario,
                    window,
                    payload=RespiratoryRatePayload(
                        value=self._jittered_value(
                            scenario,
                            window,
                            sample_index,
                            "respiratory-rate",
                            center=window.recipe.respiratory_rate_center,
                            amplitude=1.2,
                        )
                    ),
                    event_at=sample_at,
                    ordinal=f"rr-{sample_index:03d}",
                    scenario_sha256=scenario_sha256,
                )
            )
            add(
                self._observation(
                    scenario,
                    window,
                    payload=MovementPayload(
                        value=self._jittered_value(
                            scenario,
                            window,
                            sample_index,
                            "movement",
                            center=window.recipe.movement_center,
                            amplitude=min(window.recipe.movement_center, 0.12),
                            floor=0.0,
                        )
                    ),
                    event_at=sample_at,
                    ordinal=f"movement-{sample_index:03d}",
                    scenario_sha256=scenario_sha256,
                )
            )
            sample_at += timedelta(minutes=window.recipe.cadence_minutes)

        stage_start = window.starts_at
        stage_index = 0
        while stage_start < window.ends_at:
            stage, duration = _STAGE_CYCLE[stage_index % len(_STAGE_CYCLE)]
            stage_end = min(stage_start + timedelta(minutes=duration), window.ends_at)
            add(
                self._observation(
                    scenario,
                    window,
                    payload=SleepStageIntervalPayload(
                        stage=stage,
                        start_at=stage_start,
                        end_at=stage_end,
                    ),
                    event_at=stage_start,
                    ordinal=f"stage-{stage_index + 1:03d}",
                    scenario_sha256=scenario_sha256,
                )
            )
            stage_start = stage_end
            stage_index += 1

        for exit_index, offset in enumerate(
            window.recipe.bed_exit_offsets_minutes,
            start=1,
        ):
            exit_at = window.starts_at + timedelta(minutes=offset)
            add(
                self._observation(
                    scenario,
                    window,
                    payload=BedExitPayload(kind=BedExitKind.BED_EXIT),
                    event_at=exit_at,
                    ordinal=f"bed-exit-{exit_index:02d}",
                    scenario_sha256=scenario_sha256,
                )
            )
            add(
                self._observation(
                    scenario,
                    window,
                    payload=BedExitPayload(kind=BedExitKind.RETURN_TO_BED),
                    event_at=min(exit_at + timedelta(minutes=5), window.ends_at),
                    ordinal=f"bed-return-{exit_index:02d}",
                    scenario_sha256=scenario_sha256,
                )
            )

        add(
            self._observation(
                scenario,
                window,
                payload=BedPresencePayload(state=BedPresenceState.OUT_OF_BED),
                event_at=window.ends_at,
                ordinal="bed-out",
                scenario_sha256=scenario_sha256,
            )
        )
        add(
            self._observation(
                scenario,
                window,
                payload=DeviceConnectivityPayload(
                    state=DeviceConnectivityState.ONLINE
                ),
                event_at=window.ends_at + timedelta(minutes=1),
                ordinal="connectivity-end",
                scenario_sha256=scenario_sha256,
            )
        )
        if window.recipe.include_vendor_profile:
            add(
                self._observation(
                    scenario,
                    window,
                    payload=VendorSleepProfileMetricPayload(
                        metric_name="vendor_time_in_bed_minutes",
                        value_state=AvailabilityState.KNOWN,
                        value=window.duration_minutes,
                        unit="minutes",
                    ),
                    event_at=window.ends_at,
                    received_at=window.ends_at + timedelta(minutes=5),
                    ordinal="vendor-profile-v1",
                    scenario_sha256=scenario_sha256,
                    source_revision="v1",
                )
            )
        return observations

    def _apply_overlays(
        self,
        scenario: ReplayScenario,
        window: _NightWindow,
        observations: list[SleepObservation],
        *,
        scenario_sha256: str,
    ) -> list[SleepObservation]:
        applicable = tuple(
            overlay
            for overlay in scenario.overlays
            if overlay.night_index == window.recipe.night_index
        )
        result = list(observations)
        for overlay in applicable:
            if isinstance(overlay, LowCoverageOverlay):
                result = self._apply_low_coverage(
                    scenario,
                    window,
                    result,
                    overlay,
                    scenario_sha256=scenario_sha256,
                )
            elif isinstance(overlay, MissingMetricOverlay):
                result = [
                    item
                    for item in result
                    if item.observation_type != overlay.target_type
                ]
                result.append(
                    self._missing_interval(
                        scenario,
                        window,
                        target_type=overlay.target_type,
                        starts_at=window.starts_at,
                        ends_at=window.ends_at,
                        reason_code=overlay.reason_code,
                        ordinal=f"missing-{overlay.target_type.value}",
                        scenario_sha256=scenario_sha256,
                    )
                )
            elif isinstance(overlay, OfflineOverlay):
                offline_start = window.starts_at + timedelta(
                    minutes=overlay.start_offset_minutes
                )
                offline_end = offline_start + timedelta(
                    minutes=overlay.duration_minutes
                )
                if offline_end > window.ends_at:
                    raise ValueError("offline overlay exceeds its night window")
                removable = {
                    ObservationType.HEART_RATE,
                    ObservationType.RESPIRATORY_RATE,
                    ObservationType.MOVEMENT,
                }
                result = [
                    item
                    for item in result
                    if not (
                        item.observation_type in removable
                        and offline_start <= self._event_time(item) < offline_end
                    )
                ]
                result.extend(
                    (
                        self._observation(
                            scenario,
                            window,
                            payload=DeviceConnectivityPayload(
                                state=DeviceConnectivityState.OFFLINE
                            ),
                            event_at=offline_start,
                            ordinal="overlay-offline",
                            scenario_sha256=scenario_sha256,
                        ),
                        self._observation(
                            scenario,
                            window,
                            payload=DeviceConnectivityPayload(
                                state=DeviceConnectivityState.ONLINE
                            ),
                            event_at=offline_end,
                            ordinal="overlay-online",
                            scenario_sha256=scenario_sha256,
                        ),
                    )
                )
                for target_type in sorted(removable, key=lambda item: item.value):
                    result.append(
                        self._missing_interval(
                            scenario,
                            window,
                            target_type=target_type,
                            starts_at=offline_start,
                            ends_at=offline_end,
                            reason_code="synthetic_device_offline",
                            ordinal=f"offline-missing-{target_type.value}",
                            scenario_sha256=scenario_sha256,
                        )
                    )
            elif isinstance(overlay, LateReportOverlay):
                received_at = window.ends_at + timedelta(
                    minutes=overlay.delay_minutes
                )
                result = [
                    (
                        item.model_copy(update={"received_at": received_at})
                        if item.observation_type in overlay.target_types
                        else item
                    )
                    for item in result
                ]
            elif isinstance(overlay, CorrectionOverlay):
                result.append(
                    self._observation(
                        scenario,
                        window,
                        payload=VendorSleepProfileMetricPayload(
                            metric_name=overlay.metric_name,
                            value_state=AvailabilityState.KNOWN,
                            value=overlay.corrected_value,
                            unit=overlay.unit,
                        ),
                        event_at=window.ends_at,
                        received_at=window.ends_at
                        + timedelta(minutes=overlay.delay_minutes),
                        ordinal=(
                            "vendor-profile-correction-"
                            f"{overlay.correction_id}"
                        ),
                        scenario_sha256=scenario_sha256,
                        source_revision=overlay.source_revision,
                    )
                )
            elif isinstance(overlay, VendorAlertOverlay):
                event_at = window.starts_at + timedelta(
                    minutes=overlay.offset_minutes
                )
                if event_at > window.ends_at:
                    raise ValueError("vendor alert overlay exceeds its night window")
                result.append(
                    self._observation(
                        scenario,
                        window,
                        payload=VendorAlertPayload(
                            alert_code=overlay.alert_code,
                            severity=AlertSeverity(overlay.severity),
                            lifecycle_state=AlertLifecycleState(
                                overlay.lifecycle_state
                            ),
                            vendor_alert_instance_id=(
                                overlay.vendor_alert_instance_id
                            ),
                        ),
                        event_at=event_at,
                        ordinal="vendor-alert-" + overlay.vendor_alert_instance_id,
                        scenario_sha256=scenario_sha256,
                    )
                )
        return result

    def _correction_lineage(
        self,
        scenario: ReplayScenario,
        observations: list[SleepObservation],
    ) -> tuple[GeneratedCorrectionLineage, ...]:
        lineage: list[GeneratedCorrectionLineage] = []
        for overlay in scenario.overlays:
            if not isinstance(overlay, CorrectionOverlay):
                continue
            night_marker = f":night-{overlay.night_index}:"
            candidates = tuple(
                item
                for item in observations
                if night_marker in (item.provenance.source_record_id or "")
                and isinstance(item.payload, VendorSleepProfileMetricPayload)
                and item.payload.metric_name == overlay.metric_name
            )
            corrected = tuple(
                item
                for item in candidates
                if (item.provenance.source_record_id or "").endswith(
                    f":{overlay.source_revision}"
                )
            )
            parents = tuple(
                item
                for item in candidates
                if (item.provenance.source_record_id or "").endswith(
                    f":{overlay.parent_source_revision}"
                )
            )
            if len(corrected) != 1 or len(parents) != 1:
                raise ValueError(
                    "correction lineage must resolve one exact parent and child"
                )
            lineage.append(
                GeneratedCorrectionLineage(
                    correction_id=overlay.correction_id,
                    observation_id=corrected[0].observation_id,
                    parent_observation_id=parents[0].observation_id,
                    parent_source_revision=overlay.parent_source_revision,
                    source_revision=overlay.source_revision,
                )
            )
        return tuple(lineage)

    def _apply_low_coverage(
        self,
        scenario: ReplayScenario,
        window: _NightWindow,
        observations: list[SleepObservation],
        overlay: LowCoverageOverlay,
        *,
        scenario_sha256: str,
    ) -> list[SleepObservation]:
        targets = set(overlay.target_types)
        retained: list[SleepObservation] = []
        for observation in observations:
            if observation.observation_type not in targets:
                retained.append(observation)
                continue
            rank = _stable_unit_interval(
                scenario.environment.seed,
                scenario.scenario_id,
                window.recipe.night_index,
                "coverage",
                observation.observation_id,
            )
            if rank < overlay.coverage_fraction:
                retained.append(observation)
        for target_type in overlay.target_types:
            retained.append(
                self._missing_interval(
                    scenario,
                    window,
                    target_type=target_type,
                    starts_at=window.starts_at,
                    ends_at=window.ends_at,
                    reason_code="synthetic_low_coverage",
                    ordinal=f"low-coverage-{target_type.value}",
                    scenario_sha256=scenario_sha256,
                )
            )
        return retained

    def _missing_interval(
        self,
        scenario: ReplayScenario,
        window: _NightWindow,
        *,
        target_type: ObservationType,
        starts_at: datetime,
        ends_at: datetime,
        reason_code: str,
        ordinal: str,
        scenario_sha256: str,
    ) -> SleepObservation:
        return self._observation(
            scenario,
            window,
            payload=MissingIntervalPayload(
                target_observation_type=target_type,
                missing_state=MissingState.MISSING,
                reason_code=reason_code,
                interval_start_at=starts_at,
                interval_end_at=ends_at,
            ),
            event_at=starts_at,
            ordinal=ordinal,
            scenario_sha256=scenario_sha256,
        )

    def _observation(
        self,
        scenario: ReplayScenario,
        window: _NightWindow,
        *,
        payload: ObservationPayload,
        event_at: datetime,
        ordinal: str,
        scenario_sha256: str,
        received_at: datetime | None = None,
        source_revision: str = "v1",
    ) -> SleepObservation:
        observation_type = payload.observation_type
        source_kind = (
            SourceKind.VENDOR_DERIVED
            if observation_type
            in {
                ObservationType.SLEEP_STAGE_INTERVAL,
                ObservationType.VENDOR_SLEEP_PROFILE_METRIC,
                ObservationType.VENDOR_ALERT,
            }
            else SourceKind.DEVICE_MEASURED
        )
        identity_material = {
            "generator_version": scenario.environment.generator_version,
            "scenario_sha256": scenario_sha256,
            "seed": scenario.environment.seed,
            "night_index": window.recipe.night_index,
            "ordinal": ordinal,
            "source_revision": source_revision,
        }
        identity_hash = _sha256(identity_material)
        source_hash = hashlib.sha256(_source_stub(identity_hash)).hexdigest()
        missing = isinstance(payload, MissingIntervalPayload)
        measurement_at = (
            event_at
            if observation_type
            in {
                ObservationType.HEART_RATE,
                ObservationType.RESPIRATORY_RATE,
                ObservationType.MOVEMENT,
            }
            else None
        )
        return SleepObservation(
            observation_id=f"simobs:{identity_hash[:32]}",
            data_mode=DataMode.REPLAY,
            observation_type=observation_type,
            payload=payload,
            subject_id=scenario.identity.subject_id,
            device_id=scenario.identity.device_id,
            device_binding_id=scenario.identity.device_binding_id,
            binding_version=scenario.identity.binding_version,
            measurement_at=measurement_at,
            event_occurred_at=None if measurement_at is not None else event_at,
            received_at=received_at or event_at + timedelta(seconds=5),
            timezone_status=TimezoneStatus.KNOWN,
            source_kind=source_kind,
            quality=self._quality(
                scenario,
                vendor_derived=source_kind == SourceKind.VENDOR_DERIVED,
                missing=missing,
            ),
            provenance=ObservationProvenance(
                provider_id=scenario.identity.provider_id,
                provider_account_id=scenario.identity.provider_account_id,
                adapter_id="canonical-replay-adapter",
                adapter_version=_component_version(scenario, "adapter"),
                raw_ingress_record_id=f"simraw:{identity_hash[:32]}",
                raw_payload_sha256=source_hash,
                source_record_id=(
                    f"{scenario.scenario_id}:night-{window.recipe.night_index}:"
                    f"{ordinal}:{source_revision}"
                ),
                source_idempotency_key=f"simsource:{identity_hash}",
                producer_name="canonical-replay-generator",
            ),
            source_key=f"simsource:{identity_hash}",
            idempotency_key=f"simidem:{identity_hash}",
        )

    def _quality(
        self,
        scenario: ReplayScenario,
        *,
        vendor_derived: bool,
        missing: bool,
    ) -> ObservationQuality:
        if missing:
            confidence = ConfidenceValue(
                state=AvailabilityState.UNKNOWN,
                reason="source_interval_missing",
            )
            completeness = 0.0
            missing_state = MissingState.MISSING
        elif vendor_derived:
            confidence = ConfidenceValue(
                state=AvailabilityState.UNKNOWN,
                reason="synthetic_vendor_confidence_unknown",
            )
            completeness = 1.0
            missing_state = MissingState.PRESENT
        else:
            confidence = ConfidenceValue(
                state=AvailabilityState.KNOWN,
                value=0.98,
                reason="deterministic_replay_generator",
            )
            completeness = 1.0
            missing_state = MissingState.PRESENT
        return ObservationQuality(
            missing_state=missing_state,
            confidence=confidence,
            algorithm_version=AlgorithmVersionValue(
                state=AvailabilityState.KNOWN,
                value=_component_version(scenario, "algorithm"),
            ),
            calibration=CalibrationValue(
                state=AvailabilityState.UNKNOWN,
                description="synthetic replay has no hardware calibration",
            ),
            completeness=completeness,
            quality_flags=("synthetic_replay", "non_release"),
            processing_steps=(scenario.environment.generator_version,),
            limitations=(
                "synthetic_external_world_fact",
                "not_clinical_evidence",
                "not_real_device_performance",
            ),
        )

    def _jittered_value(
        self,
        scenario: ReplayScenario,
        window: _NightWindow,
        sample_index: int,
        channel: str,
        *,
        center: float,
        amplitude: float,
        floor: float | None = None,
    ) -> float:
        unit = _stable_unit_interval(
            scenario.environment.seed,
            scenario.environment.generator_version,
            scenario.scenario_id,
            window.recipe.night_index,
            sample_index,
            channel,
        )
        value = center + ((unit * 2.0) - 1.0) * amplitude
        if floor is not None:
            value = max(floor, value)
        return round(value, 2)

    @staticmethod
    def _event_time(observation: SleepObservation) -> datetime:
        event_time = observation.measurement_at or observation.event_occurred_at
        if event_time is None:
            raise ValueError(
                f"generated observation lacks event time: {observation.observation_id}"
            )
        return event_time


__all__ = [
    "CanonicalReplayGenerator",
    "ReplayFixtureError",
    "canonical_replay_source_bytes",
    "load_packaged_scenario",
    "load_replay_scenario",
]
