from __future__ import annotations

from sleepagent.product_device.provider import (
    ProviderFaultState,
    ReplayRadarProvider,
    SUPPORTED_REPLAY_SCENARIOS,
)
from sleepagent.simulation.replay import (
    get_replay_scenario,
    list_replay_scenarios,
    replay_scenario_ids,
)
from sleepagent.product_runtime.schemas import RadarDeviceStatus, RiskLevel


EXPECTED_REPLAY_SCENARIOS = (
    "normal_night",
    "device_or_data_quality_issue",
    "frequent_out_of_bed",
    "vital_fluctuation",
    "worsening_trend",
    "escalate_candidate",
    "urgent_boundary_text_input",
)
REQUIRED_REPLAY_ANOMALIES = {
    "provider_delay",
    "duplicate",
    "missing_events",
    "out_of_order",
    "offline",
    "empty_bed",
}


def test_replay_catalog_declares_exact_v1_product_scenarios() -> None:
    assert replay_scenario_ids() == EXPECTED_REPLAY_SCENARIOS
    assert SUPPORTED_REPLAY_SCENARIOS == EXPECTED_REPLAY_SCENARIOS

    summaries = list_replay_scenarios()

    assert [item.scenario_id for item in summaries] == list(EXPECTED_REPLAY_SCENARIOS)
    assert {item.risk_level for item in summaries} == {
        RiskLevel.INFO,
        RiskLevel.WATCH,
        RiskLevel.ESCALATE,
        RiskLevel.URGENT_BOUNDARY,
        RiskLevel.UNCERTAIN,
    }


def test_each_replay_scenario_saves_deterministic_input_and_expectations() -> None:
    for scenario_id in EXPECTED_REPLAY_SCENARIOS:
        scenario = get_replay_scenario(scenario_id)

        assert scenario.deterministic_input.radar_device_id == "radar-device-demo-001"
        assert scenario.deterministic_input.now.isoformat().startswith("2026-07-10")
        assert scenario.deterministic_input.night_report.night_of.isoformat() == "2026-07-09"
        assert scenario.expected.data_quality.status
        assert scenario.expected.risk_level in set(RiskLevel)
        assert scenario.expected.report_expectations.must_include_caveats
        assert scenario.expected.report_expectations.family


def test_replay_provider_returns_canonical_data_matching_catalog_expectations() -> None:
    for scenario_id in EXPECTED_REPLAY_SCENARIOS:
        scenario = get_replay_scenario(scenario_id)
        provider = ReplayRadarProvider(scenario=scenario_id)
        device = provider.list_devices()[0]
        snapshots = provider.pull_snapshots(device.radar_device_id)
        summary = provider.pull_night_report(device.radar_device_id)

        assert summary is not None
        assert device.radar_device_id == scenario.deterministic_input.radar_device_id
        assert device.bound_subject_id == scenario.deterministic_input.subject_id
        assert device.status == RadarDeviceStatus(scenario.deterministic_input.device_status)
        unique_snapshot_ids = {
            item.snapshot_id for item in scenario.deterministic_input.snapshots
        }
        assert len(snapshots) == len(unique_snapshot_ids)
        assert summary.data_coverage_ratio == scenario.expected.data_quality.coverage_ratio
        assert summary.invalid_reading_count == scenario.expected.data_quality.invalid_reading_count
        assert len(summary.missing_intervals) == scenario.expected.data_quality.missing_intervals


def test_v1_replay_catalog_covers_required_anomaly_modes() -> None:
    covered: set[str] = set()
    for scenario_id in EXPECTED_REPLAY_SCENARIOS:
        scenario = get_replay_scenario(scenario_id)
        covered.update(scenario.deterministic_input.anomalies)
        if scenario.expected.data_quality.device_offline:
            covered.add("offline")
        if any(
            item.bed_presence.value == "out_of_bed"
            for item in scenario.deterministic_input.snapshots
        ):
            covered.add("empty_bed")

    assert REQUIRED_REPLAY_ANOMALIES <= covered

    provider = ReplayRadarProvider(scenario="device_or_data_quality_issue")
    provider.pull_snapshots(provider.list_devices()[0].radar_device_id)

    assert provider.last_replay_diagnostics.delayed_event_count > 0
    assert provider.last_replay_diagnostics.duplicate_event_count == 1
    assert provider.last_replay_diagnostics.missing_event_count > 0
    assert provider.last_replay_diagnostics.out_of_order_event_count == 1
    assert provider.last_replay_diagnostics.offline_event_count == 1


def test_quality_issue_replay_surfaces_vendor_fault_without_health_claim() -> None:
    provider = ReplayRadarProvider(scenario="device_or_data_quality_issue")
    device = provider.list_devices()[0]
    summary = provider.pull_night_report(device.radar_device_id)
    health = provider.health_check()

    assert device.status == RadarDeviceStatus.OFFLINE
    assert summary is not None
    assert summary.total_sleep_minutes is None
    assert health.healthy is False
    assert health.fault_status.state == ProviderFaultState.OUTAGE
    assert get_replay_scenario("device_or_data_quality_issue").expected.risk_level == RiskLevel.UNCERTAIN


def test_frequent_out_of_bed_and_vital_fluctuation_stay_at_watch_level() -> None:
    for scenario_id in ("frequent_out_of_bed", "vital_fluctuation", "worsening_trend"):
        scenario = get_replay_scenario(scenario_id)
        provider = ReplayRadarProvider(scenario=scenario_id)
        summary = provider.pull_night_report(provider.list_devices()[0].radar_device_id)

        assert summary is not None
        assert scenario.expected.risk_level == RiskLevel.WATCH
        assert scenario.expected.questionnaire_candidates
        assert summary.data_coverage_ratio >= 0.87


def test_escalate_and_urgent_boundary_scenarios_preserve_confirmation_rules() -> None:
    escalate = get_replay_scenario("escalate_candidate")
    urgent = get_replay_scenario("urgent_boundary_text_input")

    assert escalate.expected.risk_level == RiskLevel.ESCALATE
    assert "export_doctor_material" in escalate.expected.confirmation_candidates
    assert "send_doctor_material" in escalate.expected.confirmation_candidates
    assert urgent.expected.risk_level == RiskLevel.URGENT_BOUNDARY
    assert urgent.deterministic_input.text_input is not None
    assert "notify_family_delivery_record" in urgent.expected.confirmation_candidates
