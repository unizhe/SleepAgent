from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sleepagent.domain.contracts import (
    AlgorithmVersionValue,
    AlertLifecycleState,
    AvailabilityState,
    CalibrationValue,
    ConfidenceValue,
    DataMode,
    HeartRatePayload,
    MissingIntervalPayload,
    MissingState,
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
from sleepagent.domain.reconciliation import (
    AcquisitionChannel,
    RECONCILIATION_CONFLICT_LIMITATION,
    RECONCILIATION_CONFLICT_QUALITY_FLAG,
    acquisition_channel_for_observation,
    acquisition_channel_from_provenance,
    semantic_fact_slot_key,
    semantic_value_sha256,
    with_reconciliation_conflict,
)


UTC = timezone.utc
OBSERVED_AT = datetime(2026, 8, 23, 1, 2, 3, 456789, tzinfo=UTC)
RECEIVED_AT = OBSERVED_AT + timedelta(seconds=2)
NAMESPACE = "live:perceptor-reconciliation-test"
SURFACE = "vital_signs"


def _quality(
    *,
    missing_state: MissingState = MissingState.PRESENT,
    quality_flags: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
) -> ObservationQuality:
    return ObservationQuality(
        missing_state=missing_state,
        confidence=ConfidenceValue(state=AvailabilityState.NOT_PROVIDED),
        algorithm_version=AlgorithmVersionValue(
            state=AvailabilityState.NOT_PROVIDED
        ),
        calibration=CalibrationValue(state=AvailabilityState.NOT_PROVIDED),
        quality_flags=quality_flags,
        limitations=limitations,
    )


def _provenance(
    adapter_id: str,
    *,
    suffix: str = "one",
) -> ObservationProvenance:
    return ObservationProvenance(
        provider_id="perceptor",
        provider_account_id="account-1",
        adapter_id=adapter_id,
        adapter_version="test.v1",
        raw_ingress_record_id=f"raw-{suffix}",
        raw_payload_sha256=("a" if suffix == "one" else "b") * 64,
        source_record_id=f"source-{suffix}",
        acquisition_receipt_ids=(f"receipt-{suffix}",),
    )


def _observation(
    *,
    payload: ObservationPayload | None = None,
    adapter_id: str = "yunyun-v2.5.2-push",
    suffix: str = "one",
    data_mode: DataMode = DataMode.LIVE,
    measurement_at: datetime | None = OBSERVED_AT,
    event_occurred_at: datetime | None = None,
    received_at: datetime = RECEIVED_AT,
    subject_id: str = "subject-1",
    device_id: str = "internal-device-1",
    device_binding_id: str = "binding-1",
    binding_version: int = 7,
    quality: ObservationQuality | None = None,
) -> SleepObservation:
    canonical_payload = payload or HeartRatePayload(value=64)
    observation_type = canonical_payload.observation_type
    missing_state = (
        canonical_payload.missing_state
        if isinstance(canonical_payload, MissingIntervalPayload)
        else MissingState.PRESENT
    )
    source_kind = (
        SourceKind.VENDOR_DERIVED
        if isinstance(
            canonical_payload,
            (
                SleepStageIntervalPayload,
                VendorAlertPayload,
                VendorSleepProfileMetricPayload,
            ),
        )
        else SourceKind.DEVICE_MEASURED
    )
    return SleepObservation(
        observation_id=f"observation-{suffix}",
        data_mode=data_mode,
        observation_type=observation_type,
        payload=canonical_payload,
        subject_id=subject_id,
        device_id=device_id,
        device_binding_id=device_binding_id,
        binding_version=binding_version,
        measurement_at=measurement_at,
        event_occurred_at=event_occurred_at,
        received_at=received_at,
        source_timestamp_text=f"source-time-{suffix}",
        timezone_status=TimezoneStatus.KNOWN,
        source_kind=source_kind,
        quality=quality or _quality(missing_state=missing_state),
        provenance=_provenance(adapter_id, suffix=suffix),
        source_key=f"source-key-{suffix}",
        idempotency_key=f"idempotency-{suffix}",
    )


def test_exact_push_pull_fact_and_value_identity_is_source_independent() -> None:
    push = _observation()
    pull = _observation(
        adapter_id="perceptor-pull",
        suffix="two",
        measurement_at=OBSERVED_AT.astimezone(timezone(timedelta(hours=8))),
        received_at=RECEIVED_AT + timedelta(minutes=5),
        quality=_quality(
            quality_flags=("documented_history_3_second_reconstruction",),
            limitations=("timestamp_was_adapter_reconstructed",),
        ),
    )

    assert semantic_fact_slot_key(push, NAMESPACE, SURFACE) == (
        semantic_fact_slot_key(pull, NAMESPACE, SURFACE)
    )
    assert semantic_value_sha256(push) == semantic_value_sha256(pull)
    assert acquisition_channel_for_observation(push) is AcquisitionChannel.PUSH
    assert acquisition_channel_for_observation(pull) is AcquisitionChannel.PULL
    assert AcquisitionChannel.PUSH.value == "PUSH"
    assert AcquisitionChannel.PULL.value == "PULL"


def test_semantic_fact_slot_is_isolated_by_namespace_generation() -> None:
    observation = _observation()

    generation_one = semantic_fact_slot_key(
        observation,
        NAMESPACE,
        SURFACE,
        namespace_generation=1,
    )
    assert generation_one == semantic_fact_slot_key(
        observation,
        NAMESPACE,
        SURFACE,
        namespace_generation=1,
    )
    assert generation_one != semantic_fact_slot_key(
        observation,
        NAMESPACE,
        SURFACE,
        namespace_generation=2,
    )
    with pytest.raises(ValueError, match="namespace_generation"):
        semantic_fact_slot_key(
            observation,
            NAMESPACE,
            SURFACE,
            namespace_generation=0,
        )


def test_differing_values_share_a_slot_but_not_a_value_digest() -> None:
    first = _observation(payload=HeartRatePayload(value=64))
    second = _observation(
        payload=HeartRatePayload(value=65),
        adapter_id="perceptor-pull",
        suffix="two",
    )

    assert semantic_fact_slot_key(first, NAMESPACE, SURFACE) == (
        semantic_fact_slot_key(second, NAMESPACE, SURFACE)
    )
    assert semantic_value_sha256(first) != semantic_value_sha256(second)


def test_nearby_timestamp_is_a_different_slot_without_bucketing() -> None:
    first = _observation()
    one_microsecond_later = _observation(
        suffix="two",
        measurement_at=OBSERVED_AT + timedelta(microseconds=1),
    )

    assert semantic_fact_slot_key(first, NAMESPACE, SURFACE) != (
        semantic_fact_slot_key(one_microsecond_later, NAMESPACE, SURFACE)
    )


def test_binding_and_internal_device_are_part_of_slot_identity() -> None:
    base = _observation()
    variants = (
        _observation(suffix="two", device_id="internal-device-2"),
        _observation(suffix="two", device_binding_id="binding-2"),
        _observation(suffix="two", binding_version=8),
    )
    base_key = semantic_fact_slot_key(base, NAMESPACE, SURFACE)

    assert all(
        semantic_fact_slot_key(variant, NAMESPACE, SURFACE) != base_key
        for variant in variants
    )


def test_history_and_sleep_report_semantic_surfaces_never_collapse() -> None:
    observation = _observation()

    assert semantic_fact_slot_key(observation, NAMESPACE, "vital_history") != (
        semantic_fact_slot_key(observation, NAMESPACE, "sleep_report")
    )


def test_missing_targets_are_distinct_slot_subtypes() -> None:
    missing_heart_rate = _observation(
        payload=MissingIntervalPayload(
            target_observation_type=ObservationType.HEART_RATE,
            missing_state=MissingState.MISSING,
            reason_code="vendor_null",
            interval_start_at=OBSERVED_AT,
            interval_end_at=OBSERVED_AT + timedelta(seconds=3),
        )
    )
    missing_respiratory_rate = _observation(
        payload=MissingIntervalPayload(
            target_observation_type=ObservationType.RESPIRATORY_RATE,
            missing_state=MissingState.MISSING,
            reason_code="vendor_null",
            interval_start_at=OBSERVED_AT,
            interval_end_at=OBSERVED_AT + timedelta(seconds=3),
        ),
        suffix="two",
    )

    assert semantic_fact_slot_key(missing_heart_rate, NAMESPACE, SURFACE) != (
        semantic_fact_slot_key(missing_respiratory_rate, NAMESPACE, SURFACE)
    )


def test_missing_slot_supports_a_single_known_interval_boundary() -> None:
    start_only = _observation(
        payload=MissingIntervalPayload(
            target_observation_type=ObservationType.HEART_RATE,
            missing_state=MissingState.MISSING,
            reason_code="series_ended_early",
            interval_start_at=OBSERVED_AT,
        )
    )
    no_boundaries = _observation(
        payload=MissingIntervalPayload(
            target_observation_type=ObservationType.HEART_RATE,
            missing_state=MissingState.MISSING,
            reason_code="series_absent",
        ),
        suffix="two",
    )

    assert semantic_fact_slot_key(start_only, NAMESPACE, SURFACE) != (
        semantic_fact_slot_key(no_boundaries, NAMESPACE, SURFACE)
    )


def test_stage_interval_metric_name_and_alert_instance_are_slot_subtypes() -> None:
    stage_short = _observation(
        payload=SleepStageIntervalPayload(
            stage=SleepStageState.LIGHT,
            start_at=OBSERVED_AT,
            end_at=OBSERVED_AT + timedelta(minutes=5),
        )
    )
    stage_long = _observation(
        payload=SleepStageIntervalPayload(
            stage=SleepStageState.LIGHT,
            start_at=OBSERVED_AT,
            end_at=OBSERVED_AT + timedelta(minutes=10),
        ),
        suffix="two",
    )
    metric_duration = _observation(
        payload=VendorSleepProfileMetricPayload(
            metric_name="sleep_duration_minutes",
            value_state=AvailabilityState.KNOWN,
            value=420,
            unit="minutes",
        )
    )
    metric_score = _observation(
        payload=VendorSleepProfileMetricPayload(
            metric_name="sleep_score",
            value_state=AvailabilityState.KNOWN,
            value=420,
            unit="points",
        ),
        suffix="two",
    )
    alert_one = _observation(
        payload=VendorAlertPayload(
            alert_code="BRADYCARDIA",
            lifecycle_state=AlertLifecycleState.ACTIVE,
            vendor_alert_instance_id="alert-1",
        )
    )
    alert_two = _observation(
        payload=VendorAlertPayload(
            alert_code="BRADYCARDIA",
            lifecycle_state=AlertLifecycleState.ACTIVE,
            vendor_alert_instance_id="alert-2",
        ),
        suffix="two",
    )

    assert semantic_fact_slot_key(stage_short, NAMESPACE, "sleep_report") != (
        semantic_fact_slot_key(stage_long, NAMESPACE, "sleep_report")
    )
    assert semantic_fact_slot_key(metric_duration, NAMESPACE, "sleep_report") != (
        semantic_fact_slot_key(metric_score, NAMESPACE, "sleep_report")
    )
    assert semantic_fact_slot_key(alert_one, NAMESPACE, "vendor_alerts") != (
        semantic_fact_slot_key(alert_two, NAMESPACE, "vendor_alerts")
    )


@pytest.mark.parametrize(
    "adapter_id",
    ("adapter", "push-pull-adapter", "pushpull"),
)
def test_unknown_or_ambiguous_acquisition_channel_fails_closed(
    adapter_id: str,
) -> None:
    with pytest.raises(ValueError, match="exactly one acquisition channel"):
        acquisition_channel_from_provenance(_provenance(adapter_id))


@pytest.mark.parametrize(
    ("namespace_id", "surface"),
    (
        ("replay:wrong-mode", SURFACE),
        ("live:", SURFACE),
        (" live:test", SURFACE),
        (NAMESPACE, ""),
        (NAMESPACE, "Sleep Report"),
        (NAMESPACE, "sleep/report"),
    ),
)
def test_invalid_namespace_or_surface_fails_closed(
    namespace_id: str,
    surface: str,
) -> None:
    with pytest.raises((RuntimeError, ValueError)):
        semantic_fact_slot_key(_observation(), namespace_id, surface)


def test_missing_or_unaware_observation_time_fails_closed() -> None:
    without_time = _observation(measurement_at=None, event_occurred_at=None)
    unaware_time = _observation().model_copy(
        update={"measurement_at": OBSERVED_AT.replace(tzinfo=None)}
    )

    with pytest.raises(ValueError, match="requires measurement_at"):
        semantic_fact_slot_key(without_time, NAMESPACE, SURFACE)
    with pytest.raises(ValueError, match="requires measurement_at"):
        semantic_value_sha256(without_time)
    with pytest.raises(ValueError, match="timezone-aware"):
        semantic_fact_slot_key(unaware_time, NAMESPACE, SURFACE)
    with pytest.raises(ValueError, match="timezone-aware"):
        semantic_value_sha256(unaware_time)


def test_conflict_marker_returns_an_idempotent_immutable_copy() -> None:
    original = _observation(
        quality=_quality(
            quality_flags=("existing_flag",),
            limitations=("existing_limitation",),
        )
    )

    marked = with_reconciliation_conflict(original)
    marked_again = with_reconciliation_conflict(marked)

    assert marked is not original
    assert marked.quality is not original.quality
    assert original.quality.quality_flags == ("existing_flag",)
    assert original.quality.limitations == ("existing_limitation",)
    assert marked.quality.quality_flags == (
        "existing_flag",
        RECONCILIATION_CONFLICT_QUALITY_FLAG,
    )
    assert marked.quality.limitations == (
        "existing_limitation",
        RECONCILIATION_CONFLICT_LIMITATION,
    )
    assert marked_again == marked


def test_value_digest_excludes_binding_and_envelope_provenance() -> None:
    push = _observation()
    otherwise_different_pull = _observation(
        adapter_id="perceptor-pull",
        suffix="two",
        subject_id="subject-2",
        device_id="internal-device-2",
        device_binding_id="binding-2",
        binding_version=2,
        measurement_at=OBSERVED_AT + timedelta(hours=1),
        received_at=RECEIVED_AT + timedelta(days=1),
    )

    assert semantic_value_sha256(push) == semantic_value_sha256(
        otherwise_different_pull
    )
    assert semantic_fact_slot_key(push, NAMESPACE, SURFACE) != (
        semantic_fact_slot_key(otherwise_different_pull, NAMESPACE, SURFACE)
    )
