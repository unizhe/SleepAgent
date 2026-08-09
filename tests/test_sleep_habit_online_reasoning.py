from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from sleepagent.radar_agent.product_agent.contracts import (
    CareActionCandidate,
    CareDeliveryDecision,
    CareDeliveryModality,
    CareDeliveryTiming,
    CurrentContextRisk,
    InterruptionBurden,
    LongitudinalTrend,
    MultifactorSafetyInput,
    MultiSourceConsistency,
    OnlineDataQuality,
    OnlineEventType,
    OnlineReasoningEvent,
    OnlineRiskLevel,
    RelativeBaselineDeviation,
)
from sleepagent.radar_agent.product_agent.governance import (
    AcceptanceError,
    CareActionCatalog,
)
from sleepagent.radar_agent.product_agent.habit_profile import (
    BaselineMaturity,
    InMemoryObjectiveBaselineStore,
    NightOutOfBedBaselineValue,
    ObjectiveBaselineArtifact,
)
from sleepagent.radar_agent.product_agent.online_reasoning import (
    fuse_multifactor_safety,
    resolve_event_context,
)


NOW = datetime(2026, 7, 30, 2, 5, tzinfo=timezone.utc)


def factors(**updates) -> MultifactorSafetyInput:
    values = {
        "relative_baseline_deviation": RelativeBaselineDeviation.WITHIN_BASELINE,
        "multi_source_consistency": MultiSourceConsistency.CONSISTENT,
        "data_quality": OnlineDataQuality.USABLE,
        "current_context": CurrentContextRisk.ROUTINE,
        "longitudinal_trend": LongitudinalTrend.STABLE,
        "source_refs": ("night:1",),
    }
    values.update(updates)
    return MultifactorSafetyInput(**values)


def event(safety_factors: MultifactorSafetyInput | None = None) -> OnlineReasoningEvent:
    return OnlineReasoningEvent(
        event_id="event:night-out-of-bed:1",
        event_type=OnlineEventType.NIGHT_OUT_OF_BED,
        occurred_at=NOW,
        current_signals={
            "out_of_bed_started_at": "02:00",
            "out_of_bed_duration_minutes": 18,
            "respiratory_rate_per_minute": 24,
        },
        current_signal_refs=("night:1",),
        quality_refs=("quality:radar:1",),
        trend_refs=("trend:night-out-of-bed:30d",),
        clinical_context_refs=("clinical:medication-context:1",),
        safety_factors=safety_factors or factors(),
    )


def test_night_out_of_bed_event_freezes_all_online_context_needs() -> None:
    resolution = resolve_event_context(event())

    assert resolution.evidence_gap_code == "E-NIGHT-OBSERVATION"
    assert set(resolution.required_concept_ids) >= {
        "habit.night_toileting_pattern",
        "habit.night_out_of_bed_frequency",
        "habit.night_out_of_bed_time_window",
        "habit.night_out_of_bed_duration_minutes",
        "habit.night_activity_assistance_need",
    }
    assert set(resolution.care_delivery_concept_ids) >= {
        "habit.delivery_timing_preference",
        "habit.delivery_modality_preference",
        "habit.interruption_burden",
        "habit.family_notification_preference",
        "habit.quiet_hours",
        "habit.voice_volume_preference",
    }
    assert resolution.baseline_metric_ids == ("baseline.night_out_of_bed",)
    assert "respiratory_rate_per_minute" in resolution.required_current_signal_keys
    assert resolution.current_signals["out_of_bed_duration_minutes"] == 18
    assert resolution.current_signal_refs == ("night:1",)
    assert resolution.missing_current_signal_keys == (
        "heart_rate_bpm",
        "gait_risk",
    )
    assert resolution.required_quality_ref_kinds
    assert resolution.quality_refs == ("quality:radar:1",)
    assert resolution.trend_refs == ("trend:night-out-of-bed:30d",)
    assert resolution.clinical_context_refs == (
        "clinical:medication-context:1",
    )


def test_absolute_red_flag_cannot_be_downgraded_by_personal_baseline() -> None:
    decision = fuse_multifactor_safety(
        factors(
            absolute_red_flag=True,
            absolute_red_flag_codes=("severe_respiratory_abnormality",),
            absolute_red_flag_requires_urgent=False,
        )
    )

    assert decision.risk_level == OnlineRiskLevel.ESCALATE
    assert decision.safety_required is True
    assert decision.personalization_effect == "explanation_only"


def test_multifactor_relative_deviation_and_context_force_safety() -> None:
    decision = fuse_multifactor_safety(
        factors(
            relative_baseline_deviation=RelativeBaselineDeviation.SIGNIFICANT,
            current_context=CurrentContextRisk.CONCERNING,
            longitudinal_trend=LongitudinalTrend.WORSENING,
        )
    )

    assert decision.risk_level == OnlineRiskLevel.ESCALATE
    assert decision.safety_required is True
    assert decision.urgent_required is False


def test_night_out_of_bed_baseline_is_typed_and_separate_from_habits() -> None:
    artifact = ObjectiveBaselineArtifact(
        artifact_id="baseline-artifact:night-out-of-bed:1",
        subject_id="subject-1",
        metric_id="baseline.night_out_of_bed",
        window_start=date(2026, 7, 1),
        window_end=date(2026, 7, 30),
        timezone_name="Asia/Shanghai",
        valid_night_count=28,
        coverage_ratio=0.93,
        quality_status="usable",
        algorithm_id="night-out-of-bed-baseline",
        algorithm_version="1.0.0",
        data_version="radar:v1",
        maturity=BaselineMaturity.ESTABLISHED,
        value=NightOutOfBedBaselineValue(
            events_per_valid_night=1.2,
            usual_event_count_range=(0, 2),
            usual_local_time_windows=("01:30-03:00",),
            median_duration_minutes=6,
            p90_duration_minutes=14,
            nights_with_events=20,
            recent_change="stable",
        ),
        source_refs=("radar-range:30d",),
        generated_at=NOW,
    )
    store = InMemoryObjectiveBaselineStore((artifact,))

    result = store.read(
        subject_id="subject-1",
        metric_ids=("baseline.night_out_of_bed",),
    )

    assert len(result) == 1
    assert isinstance(result[0].value, NightOutOfBedBaselineValue)
    assert result[0].value.p90_duration_minutes == 14


def test_care_delivery_contract_enforces_policy_and_conservative_default() -> None:
    catalog = CareActionCatalog()
    default_delivery = catalog.delivery_policy.conservative_default()
    morning = CareActionCandidate.create(
        candidate_id="care:morning:1",
        candidate_version=1,
        care_action_id="morning-review-feedback",
        care_action_version=1,
        title="早晨反馈昨夜情况",
        delivery=default_delivery,
        activatable=True,
    )
    catalog.validate(morning)

    loud_delivery = CareDeliveryDecision(
        timing=CareDeliveryTiming.IMMEDIATE,
        modality=CareDeliveryModality.VOICE,
        interruption_burden=InterruptionBurden.HIGH,
        voice_volume_percent=80,
        voice_tone="gentle",
        quiet_hours_active=True,
        quiet_hours_override=True,
        device_policy_ref="device-delivery-policy.v1",
        safety_reason_codes=("fall_risk",),
    )
    loud_action = CareActionCandidate.create(
        candidate_id="care:night:1",
        candidate_version=1,
        care_action_id="nighttime-gentle-support",
        care_action_version=1,
        title="夜间语音支持",
        delivery=loud_delivery,
        activatable=True,
    )
    with pytest.raises(AcceptanceError, match="volume"):
        catalog.validate(loud_action)


def test_quiet_hours_immediate_delivery_requires_safety_override() -> None:
    with pytest.raises(ValueError, match="quiet hours"):
        CareDeliveryDecision(
            timing=CareDeliveryTiming.IMMEDIATE,
            modality=CareDeliveryModality.LIGHT,
            interruption_burden=InterruptionBurden.LOW,
            quiet_hours_active=True,
            device_policy_ref="device-delivery-policy.v1",
        )
