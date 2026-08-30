# 本模块负责可复现模拟数据与回放契约，不参与生产事实判定。
"""Versioned boundary from generated replay facts to PostgreSQL intake.

The canonical generator is design input, not database authority.  This module
copies only external facts and opaque provider identities into the real intake
contract.  It derives all stream identities, idempotency identities, evidence
classification, and provenance-facing pins at the server boundary.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Final, Literal, TypeAlias
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from sleepagent.simulation.contracts import (
    GeneratedReplay,
    ReplayScenario,
    SimulationContract,
)
from sleepagent.domain.contracts import (
    AlgorithmVersionValue,
    AvailabilityState,
    CalibrationValue,
    ConfidenceValue,
    MissingIntervalPayload,
    MissingState,
    MovementPayload,
    ObservationQuality,
    ObservationType,
    SourceKind,
)
from sleepagent.domain.observation_semantics import (
    MovementMetricId,
    MovementPayloadV2,
)
from sleepagent.domain.postgres_slice import (
    ReplayObservationContract,
    ReplayObservationInput,
    ReplayObservationInputV2,
)


ADAPTER_VERSION: Final = "replay_external_fact_adapter.v1"
MANIFEST_SCHEMA_VERSION: Final = "replay_ingress_manifest.v1"
ADAPTER_VERSION_V2: Final = "replay_external_fact_adapter.v2"
MANIFEST_SCHEMA_VERSION_V2: Final = "replay_ingress_manifest.v2"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _event_at(value: ReplayObservationContract) -> datetime:
    event = value.measurement_at or value.event_occurred_at
    if event is None:  # defended by ReplayObservationInput itself
        raise ValueError("adapted replay fact has no event time")
    return event


def canonical_ingress_items_sha256(
    items: tuple["ReplayIngressItem", ...],
) -> str:
    digest = hashlib.sha256()
    for item in items:
        encoded = item.canonical_bytes()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


class ReplayIngressItem(SimulationContract):
    schema_version: Literal["replay_ingress_item.v1"] = (
        "replay_ingress_item.v1"
    )
    stream_key: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=1)
    predecessor_sequence: int | None = Field(default=None, ge=1)
    observation: ReplayObservationContract

    @model_validator(mode="after")
    def validate_predecessor(self) -> "ReplayIngressItem":
        expected = None if self.sequence == 1 else self.sequence - 1
        if self.predecessor_sequence != expected:
            raise ValueError("replay ingress predecessor must be contiguous")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json"))


class ReplayIngressManifest(SimulationContract):
    schema_version: Literal["replay_ingress_manifest.v1"] = (
        "replay_ingress_manifest.v1"
    )
    scenario_id: str = Field(min_length=1)
    scenario_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generator_version: str = Field(min_length=1)
    adapter_version: Literal["replay_external_fact_adapter.v1"] = (
        ADAPTER_VERSION
    )
    component_pins_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_sequence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_count: int = Field(ge=1)
    first_received_at: datetime
    last_received_at: datetime
    night_count: Literal[1] = 1
    data_mode: Literal["replay"] = "replay"
    synthetic_non_release: Literal[True] = True


class AdaptedReplayIngress(SimulationContract):
    schema_version: Literal["adapted_replay_ingress.v1"] = (
        "adapted_replay_ingress.v1"
    )
    manifest: ReplayIngressManifest
    items: tuple[ReplayIngressItem, ...]

    @model_validator(mode="after")
    def validate_manifest(self) -> "AdaptedReplayIngress":
        if len(self.items) != self.manifest.observation_count:
            raise ValueError("ingress manifest count does not match items")
        if not self.items:
            raise ValueError("adapted replay ingress cannot be empty")
        stream_keys = {item.stream_key for item in self.items}
        if len(stream_keys) != 1:
            raise ValueError("normal one-night intake must use exactly one stream")
        sequences = [item.sequence for item in self.items]
        if sequences != list(range(1, len(self.items) + 1)):
            raise ValueError("replay ingress sequence must be contiguous")
        first = min(item.observation.received_at for item in self.items)
        last = max(item.observation.received_at for item in self.items)
        if (
            self.manifest.first_received_at != first
            or self.manifest.last_received_at != last
        ):
            raise ValueError("ingress manifest received-time bounds drifted")
        if self.manifest.canonical_sequence_sha256 != (
            canonical_ingress_items_sha256(self.items)
        ):
            raise ValueError("ingress manifest canonical sequence hash drifted")
        return self


class ReplayIngressManifestV2(SimulationContract):
    """Clock-releasable manifest for one to fifteen ordered nights."""

    schema_version: Literal["replay_ingress_manifest.v2"] = (
        MANIFEST_SCHEMA_VERSION_V2
    )
    scenario_id: str = Field(min_length=1)
    scenario_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generator_version: str = Field(min_length=1)
    adapter_version: Literal["replay_external_fact_adapter.v2"] = (
        ADAPTER_VERSION_V2
    )
    component_pins_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_sequence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_count: int = Field(ge=1)
    initial_observation_count: int = Field(ge=1)
    first_received_at: datetime
    last_received_at: datetime
    initial_release_through: datetime
    night_count: int = Field(ge=1, le=15)
    data_mode: Literal["replay"] = "replay"
    synthetic_non_release: Literal[True] = True

    @model_validator(mode="after")
    def validate_release_window(self) -> "ReplayIngressManifestV2":
        if self.initial_observation_count > self.observation_count:
            raise ValueError("initial observation count exceeds manifest count")
        if not (
            self.first_received_at
            <= self.initial_release_through
            <= self.last_received_at
        ):
            raise ValueError("initial release boundary is outside manifest bounds")
        return self


class AdaptedReplayIngressV2(SimulationContract):
    schema_version: Literal["adapted_replay_ingress.v2"] = (
        "adapted_replay_ingress.v2"
    )
    manifest: ReplayIngressManifestV2
    items: tuple[ReplayIngressItem, ...]

    @model_validator(mode="after")
    def validate_manifest(self) -> "AdaptedReplayIngressV2":
        _validate_adapted_items(self.items, self.manifest)
        initial = tuple(
            item
            for item in self.items
            if item.observation.received_at <= self.manifest.initial_release_through
        )
        if len(initial) != self.manifest.initial_observation_count:
            raise ValueError("initial release count does not match boundary")
        if initial != self.items[: len(initial)]:
            raise ValueError("initial release facts must be a sequence prefix")
        return self

    @property
    def initial_items(self) -> tuple[ReplayIngressItem, ...]:
        return self.items[: self.manifest.initial_observation_count]

    @property
    def future_items(self) -> tuple[ReplayIngressItem, ...]:
        return self.items[self.manifest.initial_observation_count :]


AdaptedReplay: TypeAlias = AdaptedReplayIngress | AdaptedReplayIngressV2


class ReplayExternalFactAdapter:
    """Adapt one generated, single-night fixture into server-owned intake."""

    version = ADAPTER_VERSION

    def __init__(self, *, observation_semantics_version: str = "v1") -> None:
        _validate_semantics_version(observation_semantics_version)
        self.observation_semantics_version = observation_semantics_version

    def adapt(
        self,
        scenario: ReplayScenario,
        generated: GeneratedReplay,
    ) -> AdaptedReplayIngress:
        if scenario.scenario_id != generated.manifest.scenario_id:
            raise ValueError("scenario and generated replay IDs do not match")
        if generated.manifest.night_count != 1 or len(scenario.nights) != 1:
            raise ValueError("replay ingress v1 requires exactly one night")
        adapter_pin = next(
            pin
            for pin in scenario.environment.component_pins
            if pin.component == "adapter"
        )
        if adapter_pin.version != self.version:
            raise ValueError("scenario adapter pin does not match the server adapter")

        ordered = sorted(
            generated.observations,
            key=lambda item: (
                item.received_at,
                item.measurement_at or item.event_occurred_at,
                item.provenance.source_record_id or "",
                item.observation_type.value,
                _canonical_json(item.payload.model_dump(mode="json")),
            ),
        )
        stream_key = "replay-stream:" + _sha256(
            {
                "provider_id": scenario.identity.provider_id,
                "provider_account_id": scenario.identity.provider_account_id,
                "provider_device_id": scenario.identity.provider_device_id,
                "subject_id": scenario.identity.subject_id,
            }
        )
        items: list[ReplayIngressItem] = []
        for sequence, source in enumerate(ordered, start=1):
            external_fact = {
                "provider_id": scenario.identity.provider_id,
                "provider_account_id": scenario.identity.provider_account_id,
                "provider_device_id": scenario.identity.provider_device_id,
                "subject_id": scenario.identity.subject_id,
                "device_id": scenario.identity.device_id,
                "device_binding_id": scenario.identity.device_binding_id,
                "binding_version": scenario.identity.binding_version,
                "timezone_name": scenario.environment.timezone_name,
                "observation_type": source.observation_type.value,
                "payload": source.payload.model_dump(mode="json"),
                "measurement_at": source.measurement_at,
                "event_occurred_at": source.event_occurred_at,
                "received_at": source.received_at,
                "source_record_id": source.provenance.source_record_id,
            }
            fact_sha256 = _sha256(external_fact)
            observation = _replay_observation_input(
                observation_semantics_version=self.observation_semantics_version,
                scenario=scenario,
                generated=generated,
                source=source,
                sequence=sequence,
                fact_sha256=fact_sha256,
                processing_step=self.version,
            )
            items.append(
                ReplayIngressItem(
                    stream_key=stream_key,
                    sequence=sequence,
                    predecessor_sequence=(None if sequence == 1 else sequence - 1),
                    observation=observation,
                )
            )

        adapted_items = tuple(items)
        manifest = ReplayIngressManifest(
            scenario_id=scenario.scenario_id,
            scenario_sha256=generated.manifest.scenario_sha256,
            generator_version=generated.manifest.generator_version,
            component_pins_sha256=_sha256(
                [
                    pin.model_dump(mode="json")
                    for pin in scenario.environment.component_pins
                ]
            ),
            canonical_sequence_sha256=(
                canonical_ingress_items_sha256(adapted_items)
            ),
            observation_count=len(adapted_items),
            first_received_at=min(
                item.observation.received_at for item in adapted_items
            ),
            last_received_at=max(
                item.observation.received_at for item in adapted_items
            ),
        )
        return AdaptedReplayIngress(manifest=manifest, items=adapted_items)


class ReplayExternalFactAdapterV2:
    """Adapt a clock-releasable facts-only scenario with server-owned pins."""

    version = ADAPTER_VERSION_V2
    source_adapter_version = "1.0.0"

    def __init__(self, *, observation_semantics_version: str = "v1") -> None:
        _validate_semantics_version(observation_semantics_version)
        self.observation_semantics_version = observation_semantics_version

    def adapt(
        self,
        scenario: ReplayScenario,
        generated: GeneratedReplay,
    ) -> AdaptedReplayIngressV2:
        if scenario.scenario_id != generated.manifest.scenario_id:
            raise ValueError("scenario and generated replay IDs do not match")
        if generated.manifest.night_count != len(scenario.nights):
            raise ValueError("generated replay night count drifted")
        if not 1 <= len(scenario.nights) <= 15:
            raise ValueError("replay ingress v2 supports one to fifteen nights")
        adapter_pin = next(
            pin
            for pin in scenario.environment.component_pins
            if pin.component == "adapter"
        )
        if adapter_pin.version != self.source_adapter_version:
            raise ValueError("scenario source adapter pin is not approved for v2")

        items = _adapt_items(
            scenario,
            generated,
            processing_step=self.version,
            observation_semantics_version=self.observation_semantics_version,
        )
        if len(scenario.nights) == 1:
            initial_count = len(items)
        else:
            second = scenario.nights[1]
            second_bedtime = datetime.combine(
                second.sleep_day,
                second.bedtime_local,
                ZoneInfo(scenario.environment.timezone_name),
            )
            initial_count = sum(
                item.observation.received_at < second_bedtime for item in items
            )
        if initial_count < 1:
            raise ValueError("replay ingress v2 has no initial-night facts")
        initial_release_through = items[initial_count - 1].observation.received_at
        manifest = ReplayIngressManifestV2(
            scenario_id=scenario.scenario_id,
            scenario_sha256=generated.manifest.scenario_sha256,
            generator_version=generated.manifest.generator_version,
            component_pins_sha256=_sha256(
                [
                    pin.model_dump(mode="json")
                    for pin in scenario.environment.component_pins
                ]
            ),
            canonical_sequence_sha256=canonical_ingress_items_sha256(items),
            observation_count=len(items),
            initial_observation_count=initial_count,
            first_received_at=min(item.observation.received_at for item in items),
            last_received_at=max(item.observation.received_at for item in items),
            initial_release_through=initial_release_through,
            night_count=len(scenario.nights),
        )
        return AdaptedReplayIngressV2(manifest=manifest, items=items)


def replay_external_fact_adapter(
    version: str,
    *,
    observation_semantics_version: str = "v1",
) -> ReplayExternalFactAdapter | ReplayExternalFactAdapterV2:
    if version == ADAPTER_VERSION:
        return ReplayExternalFactAdapter(
            observation_semantics_version=observation_semantics_version
        )
    if version == ADAPTER_VERSION_V2:
        return ReplayExternalFactAdapterV2(
            observation_semantics_version=observation_semantics_version
        )
    raise ValueError("unsupported replay external-fact adapter version")


def _adapt_items(
    scenario: ReplayScenario,
    generated: GeneratedReplay,
    *,
    processing_step: str,
    observation_semantics_version: str,
) -> tuple[ReplayIngressItem, ...]:
    ordered = sorted(
        generated.observations,
        key=lambda item: (
            item.received_at,
            item.measurement_at or item.event_occurred_at,
            item.provenance.source_record_id or "",
            item.observation_type.value,
            _canonical_json(item.payload.model_dump(mode="json")),
        ),
    )
    stream_key = "replay-stream:" + _sha256(
        {
            "provider_id": scenario.identity.provider_id,
            "provider_account_id": scenario.identity.provider_account_id,
            "provider_device_id": scenario.identity.provider_device_id,
            "subject_id": scenario.identity.subject_id,
        }
    )
    items: list[ReplayIngressItem] = []
    for sequence, source in enumerate(ordered, start=1):
        external_fact = {
            "provider_id": scenario.identity.provider_id,
            "provider_account_id": scenario.identity.provider_account_id,
            "provider_device_id": scenario.identity.provider_device_id,
            "subject_id": scenario.identity.subject_id,
            "device_id": scenario.identity.device_id,
            "device_binding_id": scenario.identity.device_binding_id,
            "binding_version": scenario.identity.binding_version,
            "timezone_name": scenario.environment.timezone_name,
            "observation_type": source.observation_type.value,
            "payload": source.payload.model_dump(mode="json"),
            "measurement_at": source.measurement_at,
            "event_occurred_at": source.event_occurred_at,
            "received_at": source.received_at,
            "source_record_id": source.provenance.source_record_id,
        }
        fact_sha256 = _sha256(external_fact)
        observation = _replay_observation_input(
            observation_semantics_version=observation_semantics_version,
            scenario=scenario,
            generated=generated,
            source=source,
            sequence=sequence,
            fact_sha256=fact_sha256,
            processing_step=processing_step,
        )
        items.append(
            ReplayIngressItem(
                stream_key=stream_key,
                sequence=sequence,
                predecessor_sequence=None if sequence == 1 else sequence - 1,
                observation=observation,
            )
        )
    return tuple(items)


def _validate_adapted_items(items: tuple[ReplayIngressItem, ...], manifest: Any) -> None:
    if len(items) != manifest.observation_count or not items:
        raise ValueError("ingress manifest count does not match items")
    if len({item.stream_key for item in items}) != 1:
        raise ValueError("replay intake must use exactly one stream")
    if [item.sequence for item in items] != list(range(1, len(items) + 1)):
        raise ValueError("replay ingress sequence must be contiguous")
    first = min(item.observation.received_at for item in items)
    last = max(item.observation.received_at for item in items)
    if manifest.first_received_at != first or manifest.last_received_at != last:
        raise ValueError("ingress manifest received-time bounds drifted")
    if manifest.canonical_sequence_sha256 != canonical_ingress_items_sha256(items):
        raise ValueError("ingress manifest canonical sequence hash drifted")


def _source_kind(observation_type: ObservationType) -> SourceKind:
    if observation_type in {
        ObservationType.SLEEP_STAGE_INTERVAL,
        ObservationType.VENDOR_SLEEP_PROFILE_METRIC,
        ObservationType.VENDOR_ALERT,
    }:
        return SourceKind.VENDOR_DERIVED
    return SourceKind.DEVICE_MEASURED


def _validate_semantics_version(value: str) -> None:
    if value not in {"v1", "v2"}:
        raise ValueError("unsupported observation semantics version")


def _replay_observation_input(
    *,
    observation_semantics_version: str,
    scenario: ReplayScenario,
    generated: GeneratedReplay,
    source: Any,
    sequence: int,
    fact_sha256: str,
    processing_step: str,
) -> ReplayObservationContract:
    _validate_semantics_version(observation_semantics_version)
    common = {
        "provider_id": scenario.identity.provider_id,
        "provider_account_id": scenario.identity.provider_account_id,
        "provider_device_id": scenario.identity.provider_device_id,
        "subject_id": scenario.identity.subject_id,
        "device_id": scenario.identity.device_id,
        "device_binding_id": scenario.identity.device_binding_id,
        "binding_version": scenario.identity.binding_version,
        "timezone_name": scenario.environment.timezone_name,
        "observation_type": source.observation_type,
        "payload": source.payload,
        "source_kind": _source_kind(source.observation_type),
        "quality": _server_quality(
            scenario,
            source.observation_type,
            source.payload,
            processing_step=processing_step,
        ),
        "measurement_at": source.measurement_at,
        "event_occurred_at": source.event_occurred_at,
        "received_at": source.received_at,
        "timezone_status": source.timezone_status,
        "source_key": f"external-fact:{fact_sha256}",
        "idempotency_identity": (
            f"replay:{generated.manifest.scenario_sha256}:"
            f"{sequence}:{fact_sha256}"
        ),
        "source_record_id": source.provenance.source_record_id,
    }
    if observation_semantics_version == "v1":
        return ReplayObservationInput(**common)
    movement_payload = None
    if source.observation_type is ObservationType.MOVEMENT:
        if not isinstance(source.payload, MovementPayload):
            raise ValueError("replay movement payload is not compatible with V2")
        movement_payload = MovementPayloadV2(
            metric_id=MovementMetricId.MOVEMENT_INDEX,
            value=source.payload.value,
            unit="vendor_index",
            vendor_semantic_code="perceptor.body_shake.index",
        )
    return ReplayObservationInputV2(
        **common,
        movement_payload_v2=movement_payload,
    )


def _server_quality(
    scenario: ReplayScenario,
    observation_type: ObservationType,
    payload: object,
    *,
    processing_step: str = ADAPTER_VERSION,
) -> ObservationQuality:
    missing = isinstance(payload, MissingIntervalPayload)
    vendor = _source_kind(observation_type) == SourceKind.VENDOR_DERIVED
    algorithm_version = next(
        pin.version
        for pin in scenario.environment.component_pins
        if pin.component == "algorithm"
    )
    return ObservationQuality(
        missing_state=(MissingState.MISSING if missing else MissingState.PRESENT),
        confidence=(
            ConfidenceValue(
                state=AvailabilityState.UNKNOWN,
                reason="server_classified_synthetic_source",
            )
            if missing or vendor
            else ConfidenceValue(
                state=AvailabilityState.KNOWN,
                value=0.98,
                reason="server_classified_deterministic_replay",
            )
        ),
        algorithm_version=AlgorithmVersionValue(
            state=AvailabilityState.KNOWN,
            value=algorithm_version,
        ),
        calibration=CalibrationValue(
            state=AvailabilityState.UNKNOWN,
            description="synthetic replay has no hardware calibration",
        ),
        completeness=0.0 if missing else 1.0,
        quality_flags=("synthetic_replay", "non_release"),
        processing_steps=(processing_step,),
        limitations=(
            "synthetic_external_world_fact",
            "not_clinical_evidence",
            "not_real_device_performance",
        ),
    )


__all__ = [
    "ADAPTER_VERSION",
    "ADAPTER_VERSION_V2",
    "MANIFEST_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION_V2",
    "AdaptedReplay",
    "AdaptedReplayIngress",
    "AdaptedReplayIngressV2",
    "ReplayExternalFactAdapter",
    "ReplayExternalFactAdapterV2",
    "ReplayIngressItem",
    "ReplayIngressManifest",
    "ReplayIngressManifestV2",
    "canonical_ingress_items_sha256",
    "replay_external_fact_adapter",
]
