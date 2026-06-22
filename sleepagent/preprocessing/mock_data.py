from datetime import datetime, timedelta, timezone
from math import pi, sin
from random import Random

from sleepagent.schemas import (
    PatientProfile,
    RespiratoryDetectionMetrics,
    RespiratoryEvent,
    RespiratoryEventType,
    RespiratorySummary,
    RespiratoryTrendPoint,
    RiskLevel,
    Sex,
    SignalChannel,
    SleepAnalysisResult,
    SleepEpoch,
    SleepRecordMetadata,
    SleepStage,
    SleepStageSummary,
    SleepStagingMetrics,
)


def generate_mock_sleep_analysis(
    record_id: str = "mock-shhs-0001",
    subject_id: str = "mock-subject-0001",
    duration_hours: float = 8.0,
    seed: int = 42,
    abnormal_event_rate_per_hour: float = 6.0,
) -> SleepAnalysisResult:
    """Generate deterministic mock sleep analysis data for MVP development."""
    if duration_hours <= 0:
        raise ValueError("duration_hours must be greater than 0.")
    if abnormal_event_rate_per_hour < 0:
        raise ValueError("abnormal_event_rate_per_hour cannot be negative.")

    rng = Random(seed)
    duration_seconds = duration_hours * 3600
    recording_start = datetime(2026, 1, 1, 22, 0, tzinfo=timezone.utc)

    metadata = SleepRecordMetadata(
        record_id=record_id,
        source_dataset="mock",
        patient=PatientProfile(
            subject_id=subject_id,
            age_years=rng.randint(65, 85),
            sex=rng.choice([Sex.FEMALE, Sex.MALE]),
            notes=["Synthetic profile for MVP development."],
        ),
        recording_start=recording_start,
        duration_seconds=duration_seconds,
        channels=_mock_channels(),
    )

    epochs = _generate_epochs(duration_seconds=duration_seconds, rng=rng)
    sleep_summary = _summarize_sleep(epochs=epochs, duration_seconds=duration_seconds)
    respiratory_events = _generate_respiratory_events(
        duration_seconds=duration_seconds,
        sleep_hours=max(sleep_summary.total_sleep_time_minutes / 60, 0.1),
        abnormal_event_rate_per_hour=abnormal_event_rate_per_hour,
        rng=rng,
    )
    respiratory_trend = _generate_respiratory_trend(
        duration_seconds=duration_seconds,
        events=respiratory_events,
        rng=rng,
    )
    respiratory_summary = _summarize_respiration(
        events=respiratory_events,
        trend=respiratory_trend,
        sleep_hours=max(sleep_summary.total_sleep_time_minutes / 60, 0.1),
    )

    return SleepAnalysisResult(
        metadata=metadata,
        epochs=epochs,
        respiratory_events=respiratory_events,
        respiratory_trend=respiratory_trend,
        sleep_summary=sleep_summary,
        respiratory_summary=respiratory_summary,
        sleep_staging_metrics=SleepStagingMetrics(
            accuracy=0.86,
            cohen_kappa=0.78,
            macro_f1=0.84,
            weighted_f1=0.86,
            per_class_f1={
                SleepStage.WAKE: 0.80,
                SleepStage.REM: 0.82,
                SleepStage.NREM: 0.90,
            },
        ),
        respiratory_detection_metrics=RespiratoryDetectionMetrics(
            recall=0.82,
            auc=0.88,
            f1=0.79,
            per_class_recall={
                RespiratoryEventType.NORMAL_BREATHING: 0.90,
                RespiratoryEventType.HYPOPNEA: 0.80,
                RespiratoryEventType.SUSPECTED_APNEA: 0.76,
            },
        ),
        risk_level=_infer_risk_level(respiratory_summary),
        generated_at=recording_start + timedelta(seconds=duration_seconds),
        notes=[
            "Mock data only; not derived from SHHS or a real patient.",
            "Use this payload for API, frontend, and Agent contract development.",
        ],
    )


def generate_mock_sleep_data(**kwargs: object) -> SleepAnalysisResult:
    """Alias kept short for scripts and interactive experiments."""
    return generate_mock_sleep_analysis(**kwargs)


def _mock_channels() -> list[SignalChannel]:
    return [
        SignalChannel(name="EEG C4-A1", sampling_rate_hz=125.0, unit="uV", source="PSG"),
        SignalChannel(name="EOG", sampling_rate_hz=50.0, unit="uV", source="PSG"),
        SignalChannel(name="Chin EMG", sampling_rate_hz=125.0, unit="uV", source="PSG"),
        SignalChannel(name="Airflow", sampling_rate_hz=10.0, unit="a.u.", source="PSG"),
        SignalChannel(name="Thoracic effort", sampling_rate_hz=10.0, unit="a.u.", source="PSG"),
        SignalChannel(name="Abdominal effort", sampling_rate_hz=10.0, unit="a.u.", source="PSG"),
        SignalChannel(name="SpO2", sampling_rate_hz=1.0, unit="percent", source="PSG"),
    ]


def _generate_epochs(duration_seconds: float, rng: Random) -> list[SleepEpoch]:
    epoch_seconds = 30.0
    epoch_count = int(duration_seconds // epoch_seconds)
    epochs: list[SleepEpoch] = []

    for index in range(epoch_count):
        start_second = index * epoch_seconds
        stage = _stage_for_second(start_second, duration_seconds, rng)
        confidence_base = {
            SleepStage.WAKE: 0.82,
            SleepStage.REM: 0.84,
            SleepStage.NREM: 0.90,
        }[stage]
        epochs.append(
            SleepEpoch(
                start_second=start_second,
                duration_seconds=epoch_seconds,
                stage=stage,
                confidence=_bounded_score(rng, confidence_base, spread=0.08),
            )
        )

    return epochs


def _stage_for_second(start_second: float, duration_seconds: float, rng: Random) -> SleepStage:
    if start_second < 10 * 60 or start_second > duration_seconds - 10 * 60:
        return SleepStage.WAKE

    if rng.random() < 0.03:
        return SleepStage.WAKE

    cycle_second = (start_second - 10 * 60) % (90 * 60)
    if cycle_second < 68 * 60:
        return SleepStage.NREM
    if cycle_second < 85 * 60:
        return SleepStage.REM
    return SleepStage.WAKE


def _summarize_sleep(epochs: list[SleepEpoch], duration_seconds: float) -> SleepStageSummary:
    wake_seconds = sum(epoch.duration_seconds for epoch in epochs if epoch.stage == SleepStage.WAKE)
    rem_seconds = sum(epoch.duration_seconds for epoch in epochs if epoch.stage == SleepStage.REM)
    nrem_seconds = sum(epoch.duration_seconds for epoch in epochs if epoch.stage == SleepStage.NREM)
    sleep_seconds = rem_seconds + nrem_seconds

    return SleepStageSummary(
        total_recording_minutes=round(duration_seconds / 60, 2),
        total_sleep_time_minutes=round(sleep_seconds / 60, 2),
        wake_minutes=round(wake_seconds / 60, 2),
        rem_minutes=round(rem_seconds / 60, 2),
        nrem_minutes=round(nrem_seconds / 60, 2),
        sleep_efficiency_percent=round((sleep_seconds / duration_seconds) * 100, 2),
    )


def _generate_respiratory_events(
    duration_seconds: float,
    sleep_hours: float,
    abnormal_event_rate_per_hour: float,
    rng: Random,
) -> list[RespiratoryEvent]:
    abnormal_count = int(round(sleep_hours * abnormal_event_rate_per_hour))
    hypopnea_count = int(round(abnormal_count * 0.65))
    suspected_apnea_count = max(abnormal_count - hypopnea_count, 0)
    normal_count = max(int(duration_seconds / 3600 * 4), 1)

    events: list[RespiratoryEvent] = []
    events.extend(
        _events_for_type(
            event_type=RespiratoryEventType.NORMAL_BREATHING,
            count=normal_count,
            duration_range=(30.0, 90.0),
            confidence_range=(0.82, 0.98),
            duration_seconds=duration_seconds,
            rng=rng,
        )
    )
    events.extend(
        _events_for_type(
            event_type=RespiratoryEventType.HYPOPNEA,
            count=hypopnea_count,
            duration_range=(10.0, 45.0),
            confidence_range=(0.70, 0.93),
            duration_seconds=duration_seconds,
            rng=rng,
            desaturation_range=(3.0, 6.0),
        )
    )
    events.extend(
        _events_for_type(
            event_type=RespiratoryEventType.SUSPECTED_APNEA,
            count=suspected_apnea_count,
            duration_range=(10.0, 35.0),
            confidence_range=(0.66, 0.90),
            duration_seconds=duration_seconds,
            rng=rng,
            desaturation_range=(4.0, 10.0),
        )
    )

    return sorted(events, key=lambda event: event.start_second)


def _events_for_type(
    event_type: RespiratoryEventType,
    count: int,
    duration_range: tuple[float, float],
    confidence_range: tuple[float, float],
    duration_seconds: float,
    rng: Random,
    desaturation_range: tuple[float, float] | None = None,
) -> list[RespiratoryEvent]:
    events: list[RespiratoryEvent] = []
    latest_start = max(duration_seconds - duration_range[1] - 60, 1)

    for _ in range(count):
        oxygen_desaturation_percent = None
        if desaturation_range is not None:
            oxygen_desaturation_percent = round(rng.uniform(*desaturation_range), 2)

        events.append(
            RespiratoryEvent(
                start_second=round(rng.uniform(10 * 60, latest_start), 2),
                duration_seconds=round(rng.uniform(*duration_range), 2),
                event_type=event_type,
                confidence=round(rng.uniform(*confidence_range), 3),
                oxygen_desaturation_percent=oxygen_desaturation_percent,
            )
        )

    return events


def _generate_respiratory_trend(
    duration_seconds: float,
    events: list[RespiratoryEvent],
    rng: Random,
) -> list[RespiratoryTrendPoint]:
    trend: list[RespiratoryTrendPoint] = []
    step_seconds = 5 * 60
    abnormal_starts = [
        event.start_second
        for event in events
        if event.event_type in {RespiratoryEventType.HYPOPNEA, RespiratoryEventType.SUSPECTED_APNEA}
    ]

    for second in range(0, int(duration_seconds) + 1, step_seconds):
        circadian_wave = sin((second / 3600) * pi)
        nearby_events = sum(abs(second - event_start) < step_seconds for event_start in abnormal_starts)
        rate = 14.0 + 1.2 * circadian_wave + rng.uniform(-0.8, 0.8)
        spo2 = 96.0 - min(nearby_events * 0.4, 3.0) + rng.uniform(-0.6, 0.4)
        trend.append(
            RespiratoryTrendPoint(
                second=float(second),
                breaths_per_minute=round(max(rate, 6.0), 2),
                spo2_percent=round(max(min(spo2, 100.0), 70.0), 2),
            )
        )

    return trend


def _summarize_respiration(
    events: list[RespiratoryEvent],
    trend: list[RespiratoryTrendPoint],
    sleep_hours: float,
) -> RespiratorySummary:
    normal_count = sum(
        event.event_type == RespiratoryEventType.NORMAL_BREATHING for event in events
    )
    hypopnea_count = sum(event.event_type == RespiratoryEventType.HYPOPNEA for event in events)
    suspected_apnea_count = sum(
        event.event_type == RespiratoryEventType.SUSPECTED_APNEA for event in events
    )
    mean_rate = sum(point.breaths_per_minute for point in trend) / len(trend) if trend else None

    return RespiratorySummary(
        ahi=round((hypopnea_count + suspected_apnea_count) / sleep_hours, 2),
        normal_breathing_count=normal_count,
        hypopnea_count=hypopnea_count,
        suspected_apnea_count=suspected_apnea_count,
        mean_respiratory_rate_bpm=round(mean_rate, 2) if mean_rate is not None else None,
    )


def _infer_risk_level(summary: RespiratorySummary) -> RiskLevel:
    if summary.ahi >= 15 or summary.suspected_apnea_count >= 20:
        return RiskLevel.HIGH
    if summary.ahi >= 5 or summary.suspected_apnea_count >= 5:
        return RiskLevel.MODERATE
    return RiskLevel.LOW


def _bounded_score(rng: Random, base: float, spread: float) -> float:
    return round(max(min(base + rng.uniform(-spread, spread), 1.0), 0.0), 3)
