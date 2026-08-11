from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
import tomllib

import pytest
from pydantic import TypeAdapter, ValidationError

from sleepagent.simulation import (
    CanonicalReplayGenerator,
    CorrectionOverlay,
    ReplayFixtureError,
    ReplayScenario,
    load_packaged_scenario,
    load_replay_scenario,
)
from tests.support.simulation_verifier import (
    load_action_timeline,
    load_expected_oracle,
)
from tests.support.simulation_verifier.contracts import ExpectedOracle, SimulationAction
from sleepagent.sleep_domain.contracts import (
    AvailabilityState,
    DeviceConnectivityPayload,
    DeviceConnectivityState,
    MissingIntervalPayload,
    ObservationType,
    VendorSleepProfileMetricPayload,
)


def _fixture(scenario_id: str, filename: str):
    return resources.files("sleepagent.simulation.fixtures").joinpath(
        "scenarios", scenario_id, filename
    )


def _verifier_fixture(scenario_id: str, filename: str) -> Path:
    return (
        Path(__file__).parents[2]
        / "simulation_verifier"
        / "fixtures"
        / "scenarios"
        / scenario_id
        / filename
    )


def _night_observations(generated, night_index: int):
    marker = f":night-{night_index}:"
    return tuple(
        observation
        for observation in generated.observations
        if marker in (observation.provenance.source_record_id or "")
    )


def test_golden_generator_is_deterministic_and_covers_canonical_types() -> None:
    scenario = load_packaged_scenario("golden-15-night")
    generator = CanonicalReplayGenerator()

    first = generator.generate(scenario)
    second = generator.generate(scenario)

    assert first == second
    assert first.manifest.night_count == 15
    assert first.manifest.observation_count == 7473
    assert (
        first.manifest.observation_sequence_sha256
        == "e06312e309ed82a09c7be6242afd86cb84fc0799f7cac96b9bf183bf95023030"
    )
    assert first.manifest.synthetic_non_release
    assert set(first.manifest.counts_by_type) == {
        ObservationType.HEART_RATE,
        ObservationType.RESPIRATORY_RATE,
        ObservationType.BED_PRESENCE,
        ObservationType.MOVEMENT,
        ObservationType.DEVICE_CONNECTIVITY,
        ObservationType.SLEEP_STAGE_INTERVAL,
        ObservationType.VENDOR_SLEEP_PROFILE_METRIC,
    }
    assert len({item.observation_id for item in first.observations}) == 7473
    assert all(item.data_mode.value == "replay" for item in first.observations)
    assert all(item.received_at.tzinfo is not None for item in first.observations)
    assert all("synthetic_replay" in item.quality.quality_flags for item in first.observations)
    assert all("non_release" in item.quality.quality_flags for item in first.observations)


def test_generator_emits_unknown_vendor_confidence_and_calibration() -> None:
    generated = CanonicalReplayGenerator().generate(
        load_packaged_scenario("golden-15-night")
    )

    vendor_observations = tuple(
        item
        for item in generated.observations
        if isinstance(
            item.payload,
            VendorSleepProfileMetricPayload,
        )
    )

    assert len(vendor_observations) == 15
    assert all(
        item.quality.confidence.state == AvailabilityState.UNKNOWN
        for item in vendor_observations
    )
    assert all(
        item.quality.calibration.state == AvailabilityState.UNKNOWN
        for item in vendor_observations
    )
    assert all(
        item.provenance.producer_name == "canonical-replay-generator"
        for item in vendor_observations
    )


def test_overlay_matrix_models_external_source_conditions_only() -> None:
    generated = CanonicalReplayGenerator().generate(
        load_packaged_scenario("overlay-matrix")
    )

    night_1 = _night_observations(generated, 1)
    assert sum(item.observation_type == ObservationType.HEART_RATE for item in night_1) < 160
    assert {
        item.payload.reason_code
        for item in night_1
        if isinstance(item.payload, MissingIntervalPayload)
    } == {"synthetic_low_coverage"}

    night_2 = _night_observations(generated, 2)
    assert not any(
        item.observation_type == ObservationType.RESPIRATORY_RATE
        for item in night_2
    )
    assert any(
        isinstance(item.payload, MissingIntervalPayload)
        and item.payload.target_observation_type == ObservationType.RESPIRATORY_RATE
        and item.payload.reason_code == "synthetic_resp_source_missing"
        for item in night_2
    )

    night_3 = _night_observations(generated, 3)
    assert any(
        isinstance(item.payload, DeviceConnectivityPayload)
        and item.payload.state == DeviceConnectivityState.OFFLINE
        for item in night_3
    )
    assert {
        item.payload.target_observation_type
        for item in night_3
        if isinstance(item.payload, MissingIntervalPayload)
    } == {
        ObservationType.HEART_RATE,
        ObservationType.RESPIRATORY_RATE,
        ObservationType.MOVEMENT,
    }

    night_4 = _night_observations(generated, 4)
    late = tuple(
        item
        for item in night_4
        if item.observation_type
        in {
            ObservationType.SLEEP_STAGE_INTERVAL,
            ObservationType.VENDOR_SLEEP_PROFILE_METRIC,
        }
    )
    assert late
    assert len({item.received_at for item in late}) == 1
    assert all(item.received_at > (item.event_occurred_at or item.measurement_at) for item in late)

    night_5 = _night_observations(generated, 5)
    corrections = tuple(
        item
        for item in night_5
        if isinstance(item.payload, VendorSleepProfileMetricPayload)
    )
    assert len(corrections) == 2
    assert [item.payload.value for item in corrections] == [480, 470.0]
    assert corrections[1].provenance.source_record_id.endswith(":v2")
    assert corrections[1].received_at > corrections[0].received_at
    assert sum(item.observation_type == ObservationType.BED_EXIT for item in night_5) == 2
    assert len(generated.manifest.correction_lineage) == 1
    lineage = generated.manifest.correction_lineage[0]
    assert lineage.correction_id == "time-in-bed-correction-1"
    assert lineage.parent_source_revision == "v1"
    assert lineage.source_revision == "v2"


def test_multiple_corrections_have_unique_ids_and_exact_parent_lineage() -> None:
    scenario = load_packaged_scenario("overlay-matrix")
    second = CorrectionOverlay(
        correction_id="time-in-bed-correction-2",
        night_index=5,
        metric_name="vendor_time_in_bed_minutes",
        corrected_value=465,
        unit="minutes",
        parent_source_revision="v2",
        source_revision="v3",
        delay_minutes=780,
    )
    chained = ReplayScenario.model_validate(
        scenario.model_copy(
            update={"overlays": (*scenario.overlays, second)}
        ).model_dump(mode="json")
    )

    generated = CanonicalReplayGenerator().generate(chained)

    assert len(generated.observations) == len(
        {item.observation_id for item in generated.observations}
    )
    assert [
        (item.parent_source_revision, item.source_revision)
        for item in generated.manifest.correction_lineage
    ] == [("v1", "v2"), ("v2", "v3")]
    assert (
        generated.manifest.correction_lineage[1].parent_observation_id
        == generated.manifest.correction_lineage[0].observation_id
    )


def test_duplicate_correction_identity_or_revision_is_rejected() -> None:
    scenario = load_packaged_scenario("overlay-matrix")
    duplicate = CorrectionOverlay(
        correction_id="time-in-bed-correction-1",
        night_index=5,
        metric_name="vendor_time_in_bed_minutes",
        corrected_value=465,
        unit="minutes",
        parent_source_revision="v1",
        source_revision="v2",
        delay_minutes=780,
    )
    payload = scenario.model_copy(
        update={"overlays": (*scenario.overlays, duplicate)}
    ).model_dump(mode="json")

    with pytest.raises(ValidationError, match="correction_id must be unique"):
        ReplayScenario.model_validate(payload)


def test_runtime_loader_physically_rejects_actions_and_oracles() -> None:
    assert not _fixture("golden-15-night", "actions.jsonl").is_file()
    assert not _fixture("golden-15-night", "expected.json").is_file()
    with pytest.raises(ReplayFixtureError, match="scenario.json only"):
        load_replay_scenario(
            _verifier_fixture("golden-15-night", "actions.jsonl")
        )
    with pytest.raises(ReplayFixtureError, match="scenario.json only"):
        load_replay_scenario(
            _verifier_fixture("golden-15-night", "expected.json")
        )


def test_runtime_distribution_excludes_verifier_oracle_package() -> None:
    project = tomllib.loads(
        (Path(__file__).parents[2] / "pyproject.toml").read_text()
    )
    package_data = project["tool"]["setuptools"]["package-data"]
    runtime_patterns = package_data["sleepagent.simulation.fixtures"]

    assert runtime_patterns == ["README.md", "scenarios/*/scenario.json"]
    assert all("simulation_verifier" not in pattern for pattern in runtime_patterns)
    assert not any(
        "simulation_verifier" in path.read_text(encoding="utf-8")
        for path in (Path(__file__).parents[2] / "sleepagent").rglob("*.py")
    )


def test_scenario_contract_rejects_derived_business_inputs() -> None:
    payload = json.loads(_fixture("golden-15-night", "scenario.json").read_text())
    payload["risk"] = "normal"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReplayScenario.model_validate(payload)

    payload.pop("risk")
    payload["environment"]["data_mode"] = "live"
    payload["environment"]["namespace_id"] = "live:forbidden"
    with pytest.raises(ValidationError):
        ReplayScenario.model_validate(payload)

    payload = json.loads(_fixture("golden-15-night", "scenario.json").read_text())
    payload["nights"][0]["cadence_minutes"] = 30
    with pytest.raises(ValidationError, match="Input should be 3"):
        ReplayScenario.model_validate(payload)


def test_seed_change_changes_observation_identity_and_sequence_hash() -> None:
    scenario = load_packaged_scenario("golden-15-night")
    changed_environment = scenario.environment.model_copy(
        update={"seed": scenario.environment.seed + 1}
    )
    changed = scenario.model_copy(update={"environment": changed_environment})
    generator = CanonicalReplayGenerator()

    original_result = generator.generate(scenario)
    changed_result = generator.generate(changed)

    assert (
        original_result.manifest.observation_sequence_sha256
        != changed_result.manifest.observation_sequence_sha256
    )
    assert (
        original_result.observations[0].observation_id
        != changed_result.observations[0].observation_id
    )


def test_fixture_prose_does_not_change_generated_observation_identity() -> None:
    scenario = load_packaged_scenario("golden-15-night")
    prose_only_change = scenario.model_copy(
        update={
            "description": "Different explanatory prose that is not a recipe input.",
            "tags": ("different-tag",),
        }
    )
    generator = CanonicalReplayGenerator()

    original_result = generator.generate(scenario)
    changed_result = generator.generate(prose_only_change)

    assert original_result.observations == changed_result.observations
    assert (
        original_result.manifest.observation_sequence_sha256
        == changed_result.manifest.observation_sequence_sha256
    )
    assert (
        original_result.manifest.scenario_sha256
        != changed_result.manifest.scenario_sha256
    )


def test_fixture_oracles_contain_no_runtime_observations() -> None:
    scenario_ids = (
        "golden-15-night",
        "overlay-matrix",
        "elevated-vitals",
        "worsening-vital-trend",
        "care-escalation",
        "urgent-human-text",
        "habit-family-report",
        "memory-forget",
    )
    action_types: set[str] = set()
    namespaces: set[str] = set()
    generated_ids: set[str] = set()
    for scenario_id in scenario_ids:
        scenario = load_packaged_scenario(scenario_id)
        timeline = load_action_timeline(scenario_id)
        oracle = load_expected_oracle(scenario_id)
        generated = CanonicalReplayGenerator().generate(scenario)
        assert oracle.scenario_id == scenario_id
        assert len(generated.observations) >= oracle.generator.minimum_observation_count
        assert scenario.environment.cohort_id == "canonical-replay-radar-v1"
        assert scenario.environment.namespace_id not in namespaces
        assert not generated_ids.intersection(
            item.observation_id for item in generated.observations
        )
        namespaces.add(scenario.environment.namespace_id)
        generated_ids.update(item.observation_id for item in generated.observations)
        action_types.update(action.action for action in timeline.actions)

    assert {
        "ask",
        "habit_answer",
        "habit_skip",
        "habit_unknown",
        "confirm",
        "decline",
        "feedback",
        "reanalysis",
        "restart",
        "memory_enable_synthetic",
        "memory_read",
        "forget",
        "withdraw",
        "doctor_request",
        "urgent_text",
    }.issubset(action_types)
    golden_actions = load_action_timeline("golden-15-night").actions
    assert any(
        action.action == "confirm"
        and action.target_kind == "care_action"
        for action in golden_actions
    )
    care_actions = load_action_timeline("care-escalation").actions
    assert any(
        action.action == "decline"
        and action.target_kind == "care_action"
        for action in care_actions
    )
    habit_actions = load_action_timeline("habit-family-report").actions
    assert {"habit_skip", "habit_unknown"}.issubset(
        action.action for action in habit_actions
    )
    assert any(
        action.action == "habit_answer"
        and action.actor == "family"
        and action.provenance == "family_observer_report"
        for action in habit_actions
    )
    memory_actions = load_action_timeline("memory-forget").actions
    assert {"restart", "memory_read", "forget", "withdraw"}.issubset(
        action.action for action in memory_actions
    )
    assert any(
        action.action == "urgent_text"
        for action in load_action_timeline("urgent-human-text").actions
    )


def test_expected_oracle_is_strict_and_covers_all_acceptance_modules() -> None:
    oracle = load_expected_oracle("golden-15-night")
    assert oracle.cold_start.checkpoints[-1].valid_nights == 15
    assert oracle.safety.urgent_model_invocation_count == 0
    assert oracle.memory.forget_invalidates_old_handle
    assert oracle.backend.normal_quality == "sufficient"
    assert oracle.persistence.restart_resources

    payload = oracle.model_dump(mode="json")
    payload["precomputed_agent_output"] = "forbidden"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExpectedOracle.model_validate(payload)


def test_simulation_action_contract_is_strict_and_provenance_bound() -> None:
    adapter = TypeAdapter(SimulationAction)
    action = {
        "schema_version": "simulation_action.v1",
        "event_id": "strict-family-answer",
        "scenario_id": "habit-family-report",
        "checkpoint": 2,
        "actor": "family",
        "occurred_at": "2026-03-19T07:05:00+08:00",
        "action": "habit_answer",
        "concept_id": "habit.family_observed_bedtime",
        "value": "around_22_30",
        "provenance": "family_observer_report",
    }
    assert adapter.validate_python(action).actor == "family"

    wrong_provenance = {**action, "provenance": "elder_self_report"}
    with pytest.raises(ValidationError, match="provenance must match"):
        adapter.validate_python(wrong_provenance)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        adapter.validate_python({**action, "precomputed_verdict": "safe"})


def test_minimal_scenarios_encode_world_facts_not_precomputed_verdicts() -> None:
    generator = CanonicalReplayGenerator()
    elevated = generator.generate(load_packaged_scenario("elevated-vitals"))
    heart_rates = [
        item.payload.value
        for item in elevated.observations
        if item.observation_type == ObservationType.HEART_RATE
    ]
    respiratory_rates = [
        item.payload.value
        for item in elevated.observations
        if item.observation_type == ObservationType.RESPIRATORY_RATE
    ]
    assert sum(heart_rates) / len(heart_rates) > 90
    assert sum(respiratory_rates) / len(respiratory_rates) > 20

    trend = generator.generate(load_packaged_scenario("worsening-vital-trend"))
    nightly_heart_rate_means = []
    for night_index in range(1, 5):
        values = [
            item.payload.value
            for item in _night_observations(trend, night_index)
            if item.observation_type == ObservationType.HEART_RATE
        ]
        nightly_heart_rate_means.append(sum(values) / len(values))
    assert nightly_heart_rate_means == sorted(nightly_heart_rate_means)
    assert len(set(nightly_heart_rate_means)) == 4

    for scenario_id in (
        "elevated-vitals",
        "worsening-vital-trend",
        "care-escalation",
        "urgent-human-text",
        "habit-family-report",
        "memory-forget",
    ):
        scenario_payload = load_packaged_scenario(scenario_id).model_dump(mode="json")
        serialized = json.dumps(scenario_payload, sort_keys=True)
        assert all(
            forbidden not in serialized
            for forbidden in (
                '"risk"',
                '"readiness"',
                '"fact_snapshot"',
                '"safety_decision"',
                '"digest"',
                '"verdict"',
            )
        )
