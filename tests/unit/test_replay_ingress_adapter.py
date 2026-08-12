from __future__ import annotations

from importlib import resources

import pytest
from pydantic import ValidationError

from sleepagent.simulation import (
    CanonicalReplayGenerator,
    ReplayExternalFactAdapter,
    ReplayScenario,
    load_packaged_scenario,
)
from sleepagent.simulation.replay_ingress import AdaptedReplayIngress
from sleepagent.simulation.replay_ingress import (
    AdaptedReplayIngressV2,
    ReplayExternalFactAdapterV2,
)
from sleepagent.simulation.seed_registry import (
    load_replay_seed_registry,
    verify_packaged_seed,
)
from sleepagent.sleep_domain.contracts import (
    BedPresencePayload,
    BedPresenceState,
)


pytestmark = pytest.mark.unit


def _adapt_normal() -> tuple[ReplayScenario, object, AdaptedReplayIngress]:
    scenario = load_packaged_scenario("normal-one-night")
    generated = CanonicalReplayGenerator().generate(scenario)
    adapted = ReplayExternalFactAdapter().adapt(scenario, generated)
    return scenario, generated, adapted


def test_normal_one_night_is_facts_only_and_adapts_deterministically() -> None:
    scenario, generated, first = _adapt_normal()
    second = ReplayExternalFactAdapter().adapt(scenario, generated)

    assert first == second
    assert first.manifest.night_count == 1
    assert first.manifest.observation_count == len(first.items)
    assert first.manifest.observation_count > 100
    assert first.manifest.canonical_sequence_sha256 != (
        generated.manifest.observation_sequence_sha256
    )
    assert [item.sequence for item in first.items] == list(
        range(1, len(first.items) + 1)
    )
    assert first.items[0].predecessor_sequence is None
    assert all(
        item.predecessor_sequence == item.sequence - 1
        for item in first.items[1:]
    )

    fixture = resources.files("sleepagent.simulation.fixtures").joinpath(
        "scenarios", "normal-one-night"
    )
    assert not fixture.joinpath("expected.json").is_file()
    assert not fixture.joinpath("actions.jsonl").is_file()


def test_adapter_discards_generator_internal_ids_and_provenance_pins() -> None:
    scenario = load_packaged_scenario("normal-one-night")
    generated = CanonicalReplayGenerator().generate(scenario)
    baseline = ReplayExternalFactAdapter().adapt(scenario, generated)
    first = generated.observations[0]
    changed_provenance = first.provenance.model_copy(
        update={
            "adapter_id": "untrusted-generator-adapter",
            "adapter_version": "untrusted-version",
            "raw_ingress_record_id": "simraw:changed",
            "raw_payload_sha256": "0" * 64,
            "source_idempotency_key": "simsource:changed",
            "producer_name": "untrusted-generator",
        }
    )
    changed = first.model_copy(
        update={
            "observation_id": "simobs:changed",
            "source_key": "simsource:changed",
            "idempotency_key": "simidem:changed",
            "provenance": changed_provenance,
        }
    )
    tampered_generated = generated.model_copy(
        update={"observations": (changed, *generated.observations[1:])}
    )

    adapted = ReplayExternalFactAdapter().adapt(scenario, tampered_generated)

    assert adapted == baseline
    encoded = adapted.model_dump_json()
    assert "simobs:" not in encoded
    assert "simraw:" not in encoded
    assert "simsource:" not in encoded
    assert "simidem:" not in encoded
    assert "untrusted-generator" not in encoded


def test_adapter_order_preserves_bed_in_before_wake() -> None:
    _, _, adapted = _adapt_normal()
    states = [
        (item.sequence, item.observation.payload.state)
        for item in adapted.items
        if isinstance(item.observation.payload, BedPresencePayload)
    ]

    assert states == [
        (states[0][0], BedPresenceState.IN_BED),
        (states[1][0], BedPresenceState.OUT_OF_BED),
    ]
    assert states[0][0] < states[1][0]


def test_ingress_manifest_rejects_sequence_hash_drift() -> None:
    _, _, adapted = _adapt_normal()
    payload = adapted.model_dump(mode="json")
    payload["manifest"]["canonical_sequence_sha256"] = "0" * 64

    with pytest.raises(ValidationError, match="canonical sequence hash"):
        AdaptedReplayIngress.model_validate(payload)


def test_adapter_rejects_multi_night_fixture() -> None:
    scenario = load_packaged_scenario("golden-15-night")
    generated = CanonicalReplayGenerator().generate(scenario)

    with pytest.raises(ValueError, match="exactly one night"):
        ReplayExternalFactAdapter().adapt(scenario, generated)


def test_v2_adapter_splits_initial_night_from_clock_releasable_future_facts() -> None:
    scenario = load_packaged_scenario("worsening-vital-trend")
    generated = CanonicalReplayGenerator().generate(scenario)

    adapted = ReplayExternalFactAdapterV2().adapt(scenario, generated)

    assert isinstance(adapted, AdaptedReplayIngressV2)
    assert adapted.manifest.schema_version == "replay_ingress_manifest.v2"
    assert adapted.manifest.night_count == 4
    assert 100 < len(adapted.initial_items) < len(adapted.items)
    assert adapted.future_items[0].sequence == len(adapted.initial_items) + 1
    assert all(
        item.observation.received_at
        > adapted.manifest.initial_release_through
        for item in adapted.future_items
    )
    assert adapted == ReplayExternalFactAdapterV2().adapt(scenario, generated)


def test_seed_registry_owns_actor_aliases_and_scope_matrix() -> None:
    registry = load_replay_seed_registry()
    seed = registry.lookup("canonical-replay-fixtures", "normal-one-night")
    scenario = verify_packaged_seed(seed)

    assert set(seed.actor_aliases) == {"elder", "family", "doctor"}
    assert set(seed.actor_aliases.values()).isdisjoint(
        actor.actor_id for actor in scenario.identity.actors
    )
    assert seed.actor_scopes["elder"] == (
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
    )
    assert "sleep:feedback:family" in seed.actor_scopes["family"]
    assert "product:sleep:care:confirm" in seed.actor_scopes["family"]
    assert "sleep:reanalysis:write" in seed.actor_scopes["doctor"]
    assert "product:sleep:care:confirm" not in seed.actor_scopes["doctor"]
