from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from sleepagent.domain.contracts import (
    BedPresencePayload,
    DeviceConnectivityPayload,
    HeartRatePayload,
    MissingIntervalPayload,
    MovementPayload,
    ObservationType,
    ProviderDeviceIdentity,
    RespiratoryRatePayload,
    SleepStageIntervalPayload,
    SourceKind,
    TimezoneStatus,
    VendorSleepProfileMetricPayload,
)
from sleepagent.integrations.perceptor.pull import (
    PullContractError,
    assert_requested_device_matches_binding,
    canonicalize_pull_result_v2,
    normalize_current,
    normalize_history,
    normalize_realtime,
    normalize_sleep_report,
    sleep_report_is_no_data,
    validate_realtime_start,
    with_durable_raw_reference,
)
from sleepagent.integrations.perceptor.reconciliation import (
    PerceptorReconciliationError,
    namespace_generation_scoped_candidate,
)
UTC = timezone.utc
REQUESTED = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
RECEIVED = datetime(2026, 8, 23, 8, 0, 1, tzinfo=UTC)
SHA = "a" * 64
DEVICE = ProviderDeviceIdentity(
    provider_device_id="synthetic-device-id",
    provider_device_name="SYNTHETIC-DEVICE",
    home_id="7000000000000000001",
)
CONTEXT = {
    "provider_account_id": "synthetic-account",
    "provider_device": DEVICE,
    "raw_sha256": SHA,
    "requested_at": REQUESTED,
    "received_at": RECEIVED,
}
FIXTURES = Path(__file__).parents[1] / "fixtures" / "perceptor_v2_5_2"


def test_current_maps_only_connectivity_and_presence_and_preserves_status_as_note() -> None:
    result = normalize_current(
        {
            "deviceState": "online",
            "probStatus": 1,
            "smbdFlag": 1,
            "probData": [],
            "alarmLog": {},
            "futureVendorField": "preserved privately",
        },
        **CONTEXT,
    )
    assert isinstance(result.candidates[0].payload, DeviceConnectivityPayload)
    assert isinstance(result.candidates[1].payload, BedPresencePayload)
    assert result.unknown_fields == ("futureVendorField",)
    assert "probStatus_preserved_as_vendor_evidence_not_clinical_truth" in result.parser_notes


def test_current_accepts_observed_real_snake_case_without_losing_semantics() -> None:
    result = normalize_current(
        {
            "device_state": "online",
            "prob_status": 6,
            "smbd_flag": "2",
            "prob_data": [],
            "alarm_log": {},
            "keep_time": "10",
            "show_sleep": 1,
            "guard_days": 24,
        },
        **CONTEXT,
    )
    assert len(result.candidates) == 2
    assert result.unknown_fields == ()
    assert isinstance(result.candidates[0].payload, DeviceConnectivityPayload)
    assert isinstance(result.candidates[1].payload, BedPresencePayload)


def test_realtime_parses_mixed_scalar_types_and_invalid_sentinel() -> None:
    result = normalize_realtime(
        {
            "HeartRate": "-1",
            "BreathRate": "16",
            "probStatus": 5,
            "endTime": "1787472180000",
            "time": "1787472000000",
            "timeFormat": "2026-08-23T16:00:00",
        },
        binding_timezone_name="Asia/Shanghai",
        **CONTEXT,
    )
    assert isinstance(result.candidates[0].payload, MissingIntervalPayload)
    assert result.candidates[0].payload.target_observation_type == ObservationType.HEART_RATE
    assert result.candidates[0].quality.missing_state.value == "invalid"
    assert isinstance(result.candidates[1].payload, RespiratoryRatePayload)
    assert result.candidates[1].measurement_at == datetime(2026, 8, 23, 8, tzinfo=UTC)
    assert result.candidates[1].timezone_status == TimezoneStatus.KNOWN
    assert isinstance(result.candidates[2].payload, BedPresencePayload)


def test_history_expands_each_compact_series_backwards_from_send_time() -> None:
    result = normalize_history(
        [{
            "device_id": "SYNTHETIC-DEVICE",
            "heart_rate": "60,61,-1",
            "breath_rate": "14,15,16",
            "body_shake": "0,1,2",
            "send_time": "2026-08-23T16:00:00",
            "create_time": "2026-08-23T16:00:00",
        }],
        binding_timezone_name="Asia/Shanghai",
        **CONTEXT,
    )
    assert len(result.candidates) == 9
    heart = result.candidates[:3]
    assert [item.measurement_at for item in heart] == [
        datetime(2026, 8, 23, 7, 59, 54, tzinfo=UTC),
        datetime(2026, 8, 23, 7, 59, 57, tzinfo=UTC),
        datetime(2026, 8, 23, 8, 0, 0, tzinfo=UTC),
    ]
    assert isinstance(heart[0].payload, HeartRatePayload)
    assert isinstance(heart[2].payload, MissingIntervalPayload)
    assert all(item.timezone_status == TimezoneStatus.NORMALIZED_FROM_BINDING for item in result.candidates)
    assert isinstance(result.candidates[-1].payload, MovementPayload)
    assert isinstance(result.candidates[-3].payload, MovementPayload)
    assert result.candidates[-3].payload.value == 0
    assert all(
        "timestamp_origin_reconstructed" in item.quality.quality_flags
        for item in result.candidates
    )
    assert all(
        "perceptor_history_3s.v1" in item.quality.quality_flags
        for item in result.candidates
    )
    assert all(
        "measurement_time_reconstructed_not_vendor_emitted"
        in item.quality.limitations
        for item in result.candidates
    )


def test_history_excludes_source_grounded_leading_context_from_exact_window() -> None:
    start = datetime(2026, 8, 23, 7, 59, 57, tzinfo=UTC)
    end = datetime(2026, 8, 23, 8, 0, 3, tzinfo=UTC)
    result = normalize_history(
        [{
            "device_id": "SYNTHETIC-DEVICE",
            "heart_rate": "60,61,62",
            "breath_rate": "14,15,16",
            "body_shake": "0,1,2",
            "send_time": "2026-08-23T16:00:00",
        }],
        binding_timezone_name="Asia/Shanghai",
        requested_window_start=start,
        requested_window_end=end,
        **CONTEXT,
    )

    assert len(result.candidates) == 6
    assert all(start <= item.measurement_at <= end for item in result.candidates)
    assert result.history_window_classification is not None
    assert result.history_window_classification.before_window_candidate_count == 3
    assert result.history_window_classification.in_window_candidate_count == 6
    assert result.history_window_classification.after_window_candidate_count == 0
    assert (
        "history_leading_context_preserved_in_raw_and_excluded_from_window"
        in result.parser_notes
    )


def test_history_still_rejects_unexplained_after_window_spillover() -> None:
    with pytest.raises(PullContractError, match="after the exact requested window"):
        normalize_history(
            [{
                "device_id": "SYNTHETIC-DEVICE",
                "heart_rate": "60",
                "breath_rate": "14",
                "body_shake": "0",
                "send_time": "2026-08-23T16:00:03",
            }],
            binding_timezone_name="Asia/Shanghai",
            requested_window_start=datetime(2026, 8, 23, 8, tzinfo=UTC),
            requested_window_end=datetime(2026, 8, 23, 8, 0, 2, tzinfo=UTC),
            **CONTEXT,
        )


def test_history_rejects_misaligned_compact_series_lengths() -> None:
    with pytest.raises(PullContractError, match="series lengths must align"):
        normalize_history(
            [{
                "device_id": "SYNTHETIC-DEVICE",
                "heart_rate": "60,61,62",
                "breath_rate": "14,15",
                "body_shake": "0,1,2",
                "send_time": "2026-08-23T16:00:00",
            }],
            binding_timezone_name="Asia/Shanghai",
            **CONTEXT,
        )


@pytest.mark.parametrize(
    ("send_time", "error"),
    (
        ("2026-03-08T02:30:00", "nonexistent binding-local"),
        ("2026-11-01T01:30:00", "ambiguous binding-local"),
    ),
)
def test_history_fails_closed_when_binding_local_time_has_no_unique_instant(
    send_time: str,
    error: str,
) -> None:
    with pytest.raises(PullContractError, match=error):
        normalize_history(
            [{
                "device_id": "SYNTHETIC-DEVICE",
                "heart_rate": "60",
                "breath_rate": "14",
                "body_shake": "0",
                "send_time": send_time,
            }],
            binding_timezone_name="America/New_York",
            **CONTEXT,
        )


def test_history_accepts_unique_binding_local_time_across_dst_season() -> None:
    result = normalize_history(
        [{
            "device_id": "SYNTHETIC-DEVICE",
            "heart_rate": "60",
            "breath_rate": "14",
            "body_shake": "0",
            "send_time": "2026-11-01T03:30:00",
        }],
        binding_timezone_name="America/New_York",
        **CONTEXT,
    )

    assert {item.measurement_at for item in result.candidates} == {
        datetime(2026, 11, 1, 8, 30, tzinfo=UTC)
    }


@pytest.mark.parametrize("malformed", ("", "60,,62", "60, "))
def test_history_rejects_malformed_compact_series(malformed: str) -> None:
    with pytest.raises(PullContractError, match="compact series"):
        normalize_history(
            [{
                "device_id": "SYNTHETIC-DEVICE",
                "heart_rate": malformed,
                "breath_rate": "14",
                "body_shake": "0",
                "send_time": "2026-08-23T16:00:00",
            }],
            binding_timezone_name="Asia/Shanghai",
            **CONTEXT,
        )


def test_history_device_mismatch_fails_closed() -> None:
    with pytest.raises(PullContractError, match="does not match DeviceBinding"):
        normalize_history(
            [{"device_id": "DIFFERENT", "heart_rate": "60", "breath_rate": "15", "body_shake": "0", "send_time": "2026-08-23T16:00:00"}],
            binding_timezone_name="Asia/Shanghai",
            **CONTEXT,
        )


def test_sleep_report_keeps_stages_vendor_derived_and_normalizes_series() -> None:
    result = normalize_sleep_report(
        {
            "sleep_profile": {
                "deep_sleep_rate": "25%",
                "sleep_time": "22-06",
            },
            "sleep_stage_list": [{"start_time": 1787472000, "end_time": 1787472600, "type": 2}],
            "heart_rate_data": [{"time_long": 1787472000, "value": 64}],
            "breathe_data": [{"time_long": 1787472000, "value": -1}],
            "body_shake_data": [{"hour": "16", "count": 2}],
            "heart_rate_avg": 64,
        },
        report_date=date(2026, 8, 23),
        binding_timezone_name="Asia/Shanghai",
        **CONTEXT,
    )
    stage = result.candidates[0]
    assert isinstance(stage.payload, SleepStageIntervalPayload)
    assert stage.source_kind == SourceKind.VENDOR_DERIVED
    assert "not_sleepagent_independent_stage_classification" in stage.quality.limitations
    physiological_series = tuple(
        item
        for item in result.candidates
        if isinstance(item.payload, (HeartRatePayload, RespiratoryRatePayload))
        or (
            isinstance(item.payload, MissingIntervalPayload)
            and item.payload.target_observation_type
            in {ObservationType.HEART_RATE, ObservationType.RESPIRATORY_RATE}
        )
    )
    assert physiological_series
    assert all(
        item.source_kind is SourceKind.DEVICE_MEASURED
        for item in physiological_series
    )
    assert any(isinstance(item.payload, MissingIntervalPayload) for item in result.candidates)
    assert sum(isinstance(item.payload, VendorSleepProfileMetricPayload) for item in result.candidates) == 2
    assert result.intentionally_unsupported_fields == (
        "sleep_profile.sleep_time",
    )
    report_metrics = tuple(
        item
        for item in result.candidates
        if isinstance(item.payload, VendorSleepProfileMetricPayload)
    )
    assert all(
        item.measurement_at == datetime.fromtimestamp(1787472600, tz=UTC)
        for item in report_metrics
    )
    assert all(item.source_timestamp_text == "2026-08-23" for item in report_metrics)
    assert all(
        item.timezone_status == TimezoneStatus.NORMALIZED_FROM_BINDING
        for item in report_metrics
    )
    assert all(
        "sleep_report_local_day_anchor" in item.quality.quality_flags
        for item in report_metrics
    )


def test_sanitized_real_sleep_report_series_has_explicit_v2_provenance() -> None:
    fixture = json.loads(
        (
            FIXTURES
            / "sanitized_recorded_real_pull_get_sleep_report_provenance.json"
        ).read_text(encoding="utf-8")
    )
    result = normalize_sleep_report(
        fixture["data"],
        report_date=date(2026, 8, 25),
        binding_timezone_name="Asia/Shanghai",
        **CONTEXT,
    )

    assert result.unknown_fields == ()
    assert result.intentionally_ignored_fields == (
        "breathe_data[].type",
        "heart_rate_data[].type",
    )
    assert len(result.candidates) == 2
    assert all(
        item.source_kind is SourceKind.DEVICE_MEASURED
        for item in result.candidates
    )
    canonical = canonicalize_pull_result_v2(result)
    assert {item.metric_id for item in canonical} == {
        "heart_rate",
        "respiratory_rate",
    }
    assert all(
        item.source_kind is SourceKind.DEVICE_MEASURED for item in canonical
    )


def test_full_sanitized_real_sleep_report_has_complete_v2_semantic_inventory() -> None:
    fixture = json.loads(
        (
            FIXTURES
            / "sanitized_recorded_real_pull_get_sleep_report_full.json"
        ).read_text(encoding="utf-8")
    )
    result = normalize_sleep_report(
        fixture["data"],
        report_date=date(2026, 8, 22),
        binding_timezone_name="Asia/Shanghai",
        **CONTEXT,
    )

    assert result.unknown_fields == ()
    assert result.intentionally_ignored_fields == (
        "heart_rate_data[].type",
        "sleep_stage_list[].end_time_str",
        "sleep_stage_list[].start_time_str",
    )
    assert result.intentionally_unsupported_fields == (
        "sleep_profile.in_bed_time",
        "sleep_profile.in_sleep_time",
        "sleep_profile.leave_bed_time",
        "sleep_profile.sleep_duration",
        "sleep_profile.sleep_time",
        "sleep_profile.wake_ups",
        "sleep_profile.wakeup_time",
    )
    canonical = canonicalize_pull_result_v2(result)
    inventory: dict[tuple[str, str, str], int] = {}
    for item in canonical:
        key = (
            item.metric_id,
            str(item.canonical_unit),
            item.source_kind.value,
        )
        inventory[key] = inventory.get(key, 0) + 1

    assert len(canonical) == 18
    assert inventory == {
        ("bed_exit_event", "event", "vendor_derived"): 2,
        ("deep_sleep_ratio", "percent", "vendor_derived"): 1,
        ("heart_rate", "beats_per_minute", "device_measured"): 3,
        ("heart_rate_mean", "beats_per_minute", "vendor_derived"): 1,
        ("movement_event_count", "count", "vendor_derived"): 2,
        ("movement_event_total", "count", "vendor_derived"): 1,
        ("respiratory_rate", "breaths_per_minute", "device_measured"): 3,
        ("respiratory_rate_mean", "breaths_per_minute", "vendor_derived"): 1,
        ("sleep_efficiency", "percent", "vendor_derived"): 1,
        ("sleep_stage", "stage_interval", "vendor_derived"): 3,
    }
    report_summaries = tuple(
        item
        for item in canonical
        if item.metric_id
        in {
            "deep_sleep_ratio",
            "heart_rate_mean",
            "movement_event_total",
            "respiratory_rate_mean",
            "sleep_efficiency",
        }
    )
    assert report_summaries
    assert all(item.aggregation_start_at is not None for item in report_summaries)
    assert all(item.aggregation_end_at is not None for item in report_summaries)
    assert all(item.trusted_for_analytics for item in canonical)


def test_sleep_report_summary_requires_documented_report_window() -> None:
    with pytest.raises(PullContractError, match="authoritative sleep-stage report window"):
        normalize_sleep_report(
            {"heart_rate_avg": 64},
            report_date=date(2025, 6, 30),
            binding_timezone_name="Asia/Shanghai",
            **CONTEXT,
        )


def test_sleep_report_unknown_fields_fail_closed_but_documented_apnea_is_raw_only() -> None:
    with pytest.raises(PullContractError, match="unknown top-level fields"):
        normalize_sleep_report(
            {"future_vendor_field": 1},
            report_date=date(2025, 6, 30),
            binding_timezone_name="Asia/Shanghai",
            **CONTEXT,
        )
    with pytest.raises(PullContractError, match="sleep_profile contains unknown"):
        normalize_sleep_report(
            {"sleep_profile": {"sleep_score": 82}},
            report_date=date(2025, 6, 30),
            binding_timezone_name="Asia/Shanghai",
            **CONTEXT,
        )

    raw_only = normalize_sleep_report(
        {
            "apnea_images": [
                {
                    "apnea_images": [12, 10, 8],
                    "begin_time": "01:15",
                    "end_time": "01:16",
                }
            ]
        },
        report_date=date(2025, 6, 30),
        binding_timezone_name="Asia/Shanghai",
        **CONTEXT,
    )
    assert raw_only.candidates == ()
    assert raw_only.intentionally_unsupported_fields == ("apnea_images",)

    with pytest.raises(PullContractError, match="documented fields"):
        normalize_sleep_report(
            {
                "apnea_images": [
                    {
                        "apnea_images": [12, 10, 8],
                        "begin_time": "01:15",
                        "end_time": "01:16",
                        "future_nested_field": 1,
                    }
                ]
            },
            report_date=date(2025, 6, 30),
            binding_timezone_name="Asia/Shanghai",
            **CONTEXT,
        )


def test_sleep_report_explicit_invalid_sentinels_retain_device_measurement_authority() -> None:
    result = normalize_sleep_report(
        {
            "heart_rate_data": [{"time_long": 1751205960, "value": -1}],
            "breathe_data": [{"time_long": 1751205960, "value": 0}],
        },
        report_date=date(2025, 6, 30),
        binding_timezone_name="Asia/Shanghai",
        **CONTEXT,
    )
    assert len(result.candidates) == 2
    assert all(
        isinstance(item.payload, MissingIntervalPayload)
        and item.source_kind is SourceKind.DEVICE_MEASURED
        and "vendor_invalid_measurement" in item.quality.quality_flags
        for item in result.candidates
    )
    canonical = canonicalize_pull_result_v2(result)
    assert all(
        item.metric_id == "missing_interval"
        and item.canonical_unit == "interval"
        and item.source_kind is SourceKind.DEVICE_MEASURED
        for item in canonical
    )


def test_sleep_report_normalizes_observed_hourly_movement_buckets() -> None:
    result = normalize_sleep_report(
        {
            "body_shake_data": [
                {"hour": "0", "count": 2},
                {"hour": "23", "count": 0},
            ],
        },
        report_date=date(2026, 8, 23),
        binding_timezone_name="Asia/Shanghai",
        **CONTEXT,
    )

    assert len(result.candidates) == 2
    assert all(isinstance(item.payload, MovementPayload) for item in result.candidates)
    assert all(item.source_kind == SourceKind.VENDOR_DERIVED for item in result.candidates)
    assert [item.measurement_at for item in result.candidates] == [
        datetime(2026, 8, 22, 16, tzinfo=UTC),
        datetime(2026, 8, 23, 15, tzinfo=UTC),
    ]
    assert all(
        "vendor_report_hour_bucket" in item.quality.quality_flags
        for item in result.candidates
    )
    assert all(
        "vendor_hourly_movement_count_not_continuous_sample"
        in item.quality.limitations
        for item in result.candidates
    )


@pytest.mark.parametrize("hour", ("night", "24", -1, True))
def test_sleep_report_rejects_arbitrary_or_out_of_range_hour_bucket(
    hour: object,
) -> None:
    with pytest.raises(PullContractError, match="body_shake_data.hour"):
        normalize_sleep_report(
            {"body_shake_data": [{"hour": hour, "count": 1}]},
            report_date=date(2026, 8, 23),
            binding_timezone_name="Asia/Shanghai",
            **CONTEXT,
        )


def test_sleep_report_real_no_data_shape_yields_no_fake_candidates() -> None:
    no_data = {
        "sleep_profile": {
            "deep_sleep_rate": None,
            "sleep_duration": None,
            "sleep_efficiency": None,
        },
        "sleep_stage_list": None,
        "heart_rate_data": None,
        "heart_rate_avg": 0,
        "breathe_data": None,
        "breathe_avg": 0,
        "body_shake_data": [],
        "sum_body_shake_times": 0,
        "getups": [],
    }
    assert sleep_report_is_no_data(no_data)
    result = normalize_sleep_report(
        no_data,
        report_date=date(2026, 8, 23),
        binding_timezone_name="Asia/Shanghai",
        **CONTEXT,
    )
    assert result.candidates == ()
    assert "sleep_report_no_data" in result.parser_notes


def test_sleep_report_no_data_classifier_rejects_malformed_or_contradictory_shapes() -> None:
    with pytest.raises(PullContractError, match="heart_rate_data"):
        sleep_report_is_no_data({
            "sleep_profile": {"sleep_duration": None},
            "sleep_stage_list": None,
            "heart_rate_data": {},
            "heart_rate_avg": 0,
            "breathe_data": None,
            "breathe_avg": 0,
            "body_shake_data": [],
            "sum_body_shake_times": 0,
            "getups": [],
        })
    with pytest.raises(PullContractError, match="incomplete"):
        sleep_report_is_no_data({})
    assert not sleep_report_is_no_data({
        "sleep_profile": {"sleep_duration": None},
        "sleep_stage_list": None,
        "heart_rate_data": None,
        "heart_rate_avg": 64,
        "breathe_data": None,
        "breathe_avg": 0,
        "body_shake_data": [],
        "sum_body_shake_times": 0,
        "getups": [],
    })


def test_durable_raw_reference_replaces_provenance_immutably() -> None:
    original = normalize_realtime(
        {
            "HeartRate": "72",
            "BreathRate": "16",
            "time": "1787472000000",
        },
        binding_timezone_name="Asia/Shanghai",
        **CONTEXT,
    )
    durable = with_durable_raw_reference(
        original,
        "raw-ingress:synthetic-pull-001",
    )
    assert durable is not original
    assert durable.candidates != original.candidates
    assert all(
        item.provenance.raw_ingress_record_id
        == "raw-ingress:synthetic-pull-001"
        for item in durable.candidates
    )
    assert all(
        item.provenance.raw_ingress_record_id == f"private-pull-raw:{SHA}"
        for item in original.candidates
    )
    assert tuple(item.candidate_id for item in durable.candidates) == tuple(
        item.candidate_id for item in original.candidates
    )
    with pytest.raises(PullContractError, match="non-empty"):
        with_durable_raw_reference(original, "")


def test_durable_candidate_and_source_identities_are_generation_scoped() -> None:
    candidate = normalize_realtime(
        {
            "HeartRate": "72",
            "BreathRate": "16",
            "time": "1787472000000",
        },
        binding_timezone_name="Asia/Shanghai",
        **CONTEXT,
    ).candidates[0]

    generation_one = namespace_generation_scoped_candidate(
        candidate,
        namespace_id="live:perceptor-generation-proof",
        namespace_generation=1,
    )
    generation_two = namespace_generation_scoped_candidate(
        candidate,
        namespace_id="live:perceptor-generation-proof",
        namespace_generation=2,
    )

    assert generation_one == namespace_generation_scoped_candidate(
        generation_one,
        namespace_id="live:perceptor-generation-proof",
        namespace_generation=1,
    )
    assert generation_one.candidate_id != generation_two.candidate_id
    assert generation_one.source_key != generation_two.source_key
    assert generation_one.idempotency_key != generation_two.idempotency_key
    assert generation_one.payload == generation_two.payload == candidate.payload
    with pytest.raises(PerceptorReconciliationError, match="mismatched or partial"):
        namespace_generation_scoped_candidate(
            generation_one,
            namespace_id="live:perceptor-generation-proof",
            namespace_generation=2,
        )


def test_request_name_must_match_bound_provider_identity() -> None:
    assert_requested_device_matches_binding("SYNTHETIC-DEVICE", DEVICE)
    with pytest.raises(PullContractError):
        assert_requested_device_matches_binding("OTHER", DEVICE)


def test_realtime_start_requires_positive_epoch_end_marker() -> None:
    assert validate_realtime_start({"end_time": 1787472180}) == datetime(
        2026, 8, 23, 8, 3, tzinfo=UTC
    )
    assert validate_realtime_start({}) is None
    with pytest.raises(PullContractError):
        validate_realtime_start({"unexpected": True})


def test_wrong_current_casing_is_preserved_unknown_not_silently_normalized() -> None:
    result = normalize_current({"Device_State": "online"}, **CONTEXT)
    assert result.candidates == ()
    assert result.unknown_fields == ("Device_State",)


def test_realtime_without_source_time_uses_receipt_with_explicit_uncertainty() -> None:
    result = normalize_realtime(
        {"HeartRate": "72", "BreathRate": "16", "probStatus": 6},
        binding_timezone_name="Asia/Shanghai",
        **CONTEXT,
    )
    assert all(item.measurement_at == RECEIVED for item in result.candidates)
    assert all(item.timezone_status == TimezoneStatus.NOT_PROVIDED for item in result.candidates)
    assert all(
        "measurement_time_uses_response_receipt" in item.quality.quality_flags
        for item in result.candidates
    )


def test_sanitized_recorded_real_pull_fixtures_replay_deterministically() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    fixture_names = (
        "sanitized_recorded_real_pull_get_current.json",
        "sanitized_recorded_real_pull_start.json",
        "sanitized_recorded_real_pull_get_real_times.json",
        "sanitized_recorded_real_pull_get_history_data.json",
        "sanitized_recorded_real_pull_get_sleep_report.json",
    )
    assert all(
        manifest["fixtures"][name] == "SANITIZED_RECORDED_REAL_PULL_FIXTURE"
        for name in fixture_names
    )

    current = json.loads((FIXTURES / fixture_names[0]).read_text())["data"]
    start = json.loads((FIXTURES / fixture_names[1]).read_text())["data"]
    realtime = json.loads((FIXTURES / fixture_names[2]).read_text())["data"]
    history = json.loads((FIXTURES / fixture_names[3]).read_text())["data"]
    sleep = json.loads((FIXTURES / fixture_names[4]).read_text())["data"]

    assert len(normalize_current(current, **CONTEXT).candidates) == 2
    assert validate_realtime_start(start) is None
    realtime_result = normalize_realtime(
        realtime, binding_timezone_name="Asia/Shanghai", **CONTEXT
    )
    assert len(realtime_result.candidates) == 3
    assert realtime_result.unknown_fields == ("smbdFlag",)
    history_result = normalize_history(
        history, binding_timezone_name="Asia/Shanghai", **CONTEXT
    )
    assert len(history_result.candidates) == 30
    assert sum(
        item.quality.missing_state.value == "invalid"
        for item in history_result.candidates
    ) == 3
    assert sleep_report_is_no_data(sleep)
    sleep_result = normalize_sleep_report(
        sleep,
        report_date=date(2026, 8, 23),
        binding_timezone_name="Asia/Shanghai",
        **CONTEXT,
    )
    assert sleep_result.candidates == ()
    assert "sleep_report_no_data" in sleep_result.parser_notes
