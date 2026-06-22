import pytest

from sleepagent.metrics import (
    compute_accuracy,
    compute_cohen_kappa,
    compute_f1_by_class,
    compute_macro_f1,
    compute_sleep_staging_metrics,
    compute_weighted_f1,
)
from sleepagent.schemas import SleepStage


def _example_labels() -> tuple[list[SleepStage], list[SleepStage]]:
    y_true = [
        SleepStage.WAKE,
        SleepStage.WAKE,
        SleepStage.NREM,
        SleepStage.NREM,
        SleepStage.REM,
        SleepStage.REM,
    ]
    y_pred = [
        SleepStage.WAKE,
        SleepStage.NREM,
        SleepStage.NREM,
        SleepStage.NREM,
        SleepStage.REM,
        SleepStage.WAKE,
    ]
    return y_true, y_pred


def test_basic_classification_metrics() -> None:
    y_true, y_pred = _example_labels()
    labels = [SleepStage.WAKE, SleepStage.REM, SleepStage.NREM]

    per_class_f1 = compute_f1_by_class(y_true, y_pred, labels=labels)

    assert compute_accuracy(y_true, y_pred) == pytest.approx(4 / 6)
    assert compute_cohen_kappa(y_true, y_pred, labels=labels) == pytest.approx(0.5)
    assert per_class_f1[SleepStage.WAKE] == pytest.approx(0.5)
    assert per_class_f1[SleepStage.REM] == pytest.approx(2 / 3)
    assert per_class_f1[SleepStage.NREM] == pytest.approx(0.8)
    assert compute_macro_f1(y_true, y_pred, labels=labels) == pytest.approx(
        (0.5 + (2 / 3) + 0.8) / 3
    )
    assert compute_weighted_f1(y_true, y_pred, labels=labels) == pytest.approx(
        (0.5 + (2 / 3) + 0.8) / 3
    )


def test_compute_sleep_staging_metrics_maps_raw_labels() -> None:
    metrics = compute_sleep_staging_metrics(
        y_true=[0, 1, 2, 4],
        y_pred=["Wake", "N1", "REM", "REM"],
        true_source="yasa",
    )

    assert metrics.accuracy == pytest.approx(0.75)
    assert metrics.per_class_f1[SleepStage.WAKE] == pytest.approx(1.0)
    assert metrics.per_class_f1[SleepStage.REM] == pytest.approx(2 / 3)
    assert metrics.per_class_f1[SleepStage.NREM] == pytest.approx(2 / 3)


def test_cohen_kappa_returns_one_for_perfect_single_class_prediction() -> None:
    y_true = [SleepStage.NREM, SleepStage.NREM]
    y_pred = [SleepStage.NREM, SleepStage.NREM]

    assert compute_cohen_kappa(y_true, y_pred, labels=[SleepStage.NREM]) == 1.0


def test_classification_metrics_return_zero_for_missing_label_support() -> None:
    y_true = [SleepStage.WAKE, SleepStage.WAKE]
    y_pred = [SleepStage.WAKE, SleepStage.WAKE]
    labels = [SleepStage.WAKE, SleepStage.REM, SleepStage.NREM]

    per_class_f1 = compute_f1_by_class(y_true, y_pred, labels=labels)

    assert per_class_f1[SleepStage.WAKE] == 1.0
    assert per_class_f1[SleepStage.REM] == 0.0
    assert per_class_f1[SleepStage.NREM] == 0.0
    assert compute_macro_f1(y_true, y_pred, labels=labels) == pytest.approx(1 / 3)
    assert compute_weighted_f1(y_true, y_pred, labels=labels) == 1.0


def test_weighted_f1_returns_zero_when_requested_labels_have_no_support() -> None:
    assert compute_weighted_f1(
        y_true=[SleepStage.WAKE],
        y_pred=[SleepStage.WAKE],
        labels=[SleepStage.REM],
    ) == 0.0


def test_metrics_reject_empty_or_mismatched_inputs() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        compute_accuracy([], [])

    with pytest.raises(ValueError, match="same length"):
        compute_accuracy([SleepStage.WAKE], [SleepStage.WAKE, SleepStage.REM])

    with pytest.raises(ValueError, match="labels cannot be empty"):
        compute_macro_f1([SleepStage.WAKE], [SleepStage.WAKE], labels=[])
