# 本模块负责可复现模拟数据与回放契约，不参与生产事实判定。
"""Immutable server registry for replay seed identity and authority pins."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from importlib import resources
from typing import Literal

from pydantic import Field, model_validator

from sleepagent.simulation.contracts import ReplayScenario, SimulationContract


class ReplaySeedRegistryError(ValueError):
    """The packaged seed registry or its scenario artifact has drifted."""


class ReplaySeedDefinition(SimulationContract):
    seed_id: str = Field(min_length=1)
    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    scenario_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generator_version: str = Field(min_length=1)
    adapter_version: Literal[
        "replay_external_fact_adapter.v1",
        "replay_external_fact_adapter.v2",
    ]
    component_pins_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_sequence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_count: int = Field(ge=1)
    night_count: int = Field(ge=1, le=15)
    first_received_at: datetime
    last_received_at: datetime
    namespace_id: str = Field(pattern=r"^replay:[a-z0-9][a-z0-9._:-]{0,127}$")
    subject_id: str = Field(min_length=1)
    timezone_name: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_account_id: str = Field(min_length=1)
    provider_device_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    device_binding_id: str = Field(min_length=1)
    binding_version: int = Field(ge=1)
    scenario_clock_start: datetime
    model_version: str = Field(min_length=1)
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor_aliases: dict[Literal["elder", "family", "doctor"], str]
    actor_scopes: dict[
        Literal["elder", "family", "doctor"],
        tuple[str, ...],
    ]

    @model_validator(mode="after")
    def validate_server_authority(self) -> "ReplaySeedDefinition":
        roles = {"elder", "family", "doctor"}
        if set(self.actor_aliases) != roles or set(self.actor_scopes) != roles:
            raise ValueError("seed registry requires exactly three product roles")
        expected = {
            "elder": {
                "product:sleep:today:read",
                "product:sleep:trends:read",
                "product:sleep:care:read",
                "product:sleep:records:read",
                "product:sleep:interaction:write",
                "product:sleep:interaction:answer",
                "product:sleep:care:confirm",
                "product:sleep:feedback:write",
                "product:sleep:operation:read",
                "sleep:episode:read",
                "sleep:operation:read",
                "sleep:monitoring:write",
                "sleep:feedback:self",
                "sleep:reanalysis:write",
            },
            "family": {
                "product:sleep:today:read",
                "product:sleep:trends:read",
                "product:sleep:care:read",
                "product:sleep:records:read",
                "product:sleep:interaction:write",
                "product:sleep:interaction:answer",
                "product:sleep:care:confirm",
                "product:sleep:feedback:write",
                "product:sleep:operation:read",
                "sleep:operation:read",
                "sleep:monitoring:write",
                "sleep:feedback:family",
                "sleep:reanalysis:write",
            },
            "doctor": {
                "product:sleep:today:read",
                "product:sleep:trends:read",
                "product:sleep:care:read",
                "product:sleep:records:read",
                "product:sleep:interaction:write",
                "product:sleep:interaction:answer",
                "product:sleep:operation:read",
                "sleep:operation:read",
                "sleep:reanalysis:write",
            },
        }
        if {
            role: set(scopes) for role, scopes in self.actor_scopes.items()
        } != expected:
            raise ValueError("seed registry scope matrix drifted")
        if len(set(self.actor_aliases.values())) != 3:
            raise ValueError("seed registry actor aliases must be unique")
        if self.last_received_at < self.first_received_at:
            raise ValueError("seed registry received-time bounds are inverted")
        return self

    def database_metadata(
        self,
        *,
        artifact_family: str,
        schema_manifest_sha256: str,
    ) -> dict[str, object]:
        payload = self.model_dump(mode="json")
        payload.update(
            {
                "artifact_family": artifact_family,
                "manifest_schema_version": (
                    "replay_ingress_manifest.v1"
                    if self.adapter_version == "replay_external_fact_adapter.v1"
                    else "replay_ingress_manifest.v2"
                ),
                "schema_manifest_sha256": schema_manifest_sha256,
            }
        )
        return payload


class ReplaySeedRegistry(SimulationContract):
    schema_version: Literal["replay_seed_registry.v1"] = (
        "replay_seed_registry.v1"
    )
    artifact_family: str = Field(min_length=1)
    seeds: tuple[ReplaySeedDefinition, ...]

    @model_validator(mode="after")
    def validate_unique_seeds(self) -> "ReplaySeedRegistry":
        seed_ids = [seed.seed_id for seed in self.seeds]
        scenario_ids = [seed.scenario_id for seed in self.seeds]
        if not self.seeds:
            raise ValueError("seed registry cannot be empty")
        if len(seed_ids) != len(set(seed_ids)):
            raise ValueError("seed registry seed_id values must be unique")
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("seed registry scenario_id values must be unique")
        return self

    def lookup(self, artifact_family: str, scenario_id: str) -> ReplaySeedDefinition:
        if artifact_family != self.artifact_family:
            raise ReplaySeedRegistryError("unknown replay artifact family")
        matches = [seed for seed in self.seeds if seed.scenario_id == scenario_id]
        if len(matches) != 1:
            raise ReplaySeedRegistryError("unknown replay scenario")
        return matches[0]


def load_replay_seed_registry() -> ReplaySeedRegistry:
    raw = resources.files("sleepagent.simulation").joinpath(
        "replay_seed_registry.json"
    ).read_bytes()
    try:
        return ReplaySeedRegistry.model_validate_json(raw)
    except Exception as exc:
        raise ReplaySeedRegistryError("invalid packaged replay seed registry") from exc


def verify_packaged_seed(seed: ReplaySeedDefinition) -> ReplayScenario:
    """Verify only the strict scenario document; generation remains Worker-only."""

    raw = resources.files("sleepagent.simulation.fixtures").joinpath(
        "scenarios", seed.scenario_id, "scenario.json"
    ).read_bytes()
    try:
        scenario = ReplayScenario.model_validate_json(raw)
    except Exception as exc:
        raise ReplaySeedRegistryError("packaged replay scenario is invalid") from exc
    canonical = json.dumps(
        scenario.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != seed.scenario_sha256:
        raise ReplaySeedRegistryError("packaged replay scenario checksum drifted")
    if scenario.scenario_id != seed.scenario_id:
        raise ReplaySeedRegistryError("packaged replay scenario ID drifted")
    if scenario.environment.generator_version != seed.generator_version:
        raise ReplaySeedRegistryError("packaged replay generator pin drifted")
    adapter_pin = next(
        pin
        for pin in scenario.environment.component_pins
        if pin.component == "adapter"
    )
    expected_source_adapter = (
        seed.adapter_version
        if seed.adapter_version == "replay_external_fact_adapter.v1"
        else "1.0.0"
    )
    if adapter_pin.version != expected_source_adapter:
        raise ReplaySeedRegistryError("packaged replay adapter pin drifted")
    if len(scenario.nights) != seed.night_count:
        raise ReplaySeedRegistryError("packaged replay night count drifted")
    return scenario


__all__ = [
    "ReplaySeedDefinition",
    "ReplaySeedRegistry",
    "ReplaySeedRegistryError",
    "load_replay_seed_registry",
    "verify_packaged_seed",
]
