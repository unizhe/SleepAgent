import pytest

from sleepagent.models import (
    build_sleep_epochs_from_yasa_labels,
    build_yasa_sleep_staging_result,
    extract_yasa_confidences,
    infer_yasa_epoch_duration_seconds,
)
from sleepagent.schemas import SleepStage


class FakeYASAHypnogram:
    def __init__(
        self,
        hypno: list[object],
        *,
        proba: list[dict[str, float]] | None = None,
        sampling_frequency: float = 1 / 30,
        freq: str = "30s",
    ) -> None:
        self.hypno = hypno
        self.proba = proba
        self.sampling_frequency = sampling_frequency
        self.freq = freq


def test_builds_epochs_and_summary_from_yasa_numeric_labels() -> None:
    result = build_yasa_sleep_staging_result([0, 1, 2, 3, 4])

    assert [epoch.stage for epoch in result.epochs] == [
        SleepStage.WAKE,
        SleepStage.NREM,
        SleepStage.NREM,
        SleepStage.NREM,
        SleepStage.REM,
    ]
    assert [epoch.start_second for epoch in result.epochs] == [0, 30, 60, 90, 120]
    assert result.sleep_summary.total_recording_minutes == pytest.approx(2.5)
    assert result.sleep_summary.wake_minutes == pytest.approx(0.5)
    assert result.sleep_summary.nrem_minutes == pytest.approx(1.5)
    assert result.sleep_summary.rem_minutes == pytest.approx(0.5)
    assert result.sleep_summary.sleep_efficiency_percent == pytest.approx(80.0)


def test_uses_yasa_probabilities_for_epoch_confidence_and_metrics() -> None:
    hypnogram = FakeYASAHypnogram(
        ["WAKE", "N2", "REM"],
        proba=[
            {"WAKE": 0.90, "N1": 0.02, "N2": 0.04, "N3": 0.01, "REM": 0.03},
            {"WAKE": 0.10, "N1": 0.12, "N2": 0.68, "N3": 0.05, "REM": 0.05},
            {"WAKE": 0.04, "N1": 0.02, "N2": 0.03, "N3": 0.01, "REM": 0.90},
        ],
    )

    result = build_yasa_sleep_staging_result(
        hypnogram,
        reference_stages=[0, 2, 5],
        reference_source="shhs",
    )

    assert [epoch.confidence for epoch in result.epochs] == pytest.approx([0.90, 0.68, 0.90])
    assert result.sleep_staging_metrics is not None
    assert result.sleep_staging_metrics.accuracy == pytest.approx(1.0)
    assert result.sleep_staging_metrics.per_class_f1[SleepStage.WAKE] == pytest.approx(1.0)
    assert result.sleep_staging_metrics.per_class_f1[SleepStage.REM] == pytest.approx(1.0)
    assert result.sleep_staging_metrics.per_class_f1[SleepStage.NREM] == pytest.approx(1.0)


def test_can_infer_epoch_duration_from_yasa_metadata() -> None:
    assert infer_yasa_epoch_duration_seconds(FakeYASAHypnogram(["WAKE"])) == pytest.approx(30.0)
    assert infer_yasa_epoch_duration_seconds(
        FakeYASAHypnogram(["WAKE"], sampling_frequency=1 / 20, freq="20s")
    ) == pytest.approx(20.0)
    assert infer_yasa_epoch_duration_seconds(
        object(),
        epoch_duration_seconds=15.0,
    ) == pytest.approx(15.0)


def test_rejects_invalid_yasa_adapter_inputs() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        build_sleep_epochs_from_yasa_labels([])

    with pytest.raises(ValueError, match="same length"):
        build_sleep_epochs_from_yasa_labels([0, 1], confidences=[0.8])

    with pytest.raises(ValueError, match="probability rows"):
        extract_yasa_confidences(
            FakeYASAHypnogram(["WAKE", "REM"], proba=[{"WAKE": 1.0}]),
            expected_length=2,
        )

    with pytest.raises(ValueError, match="confidence"):
        build_sleep_epochs_from_yasa_labels([0], confidences=[1.2])
