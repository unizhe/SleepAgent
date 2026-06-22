import pytest

from sleepagent.metrics import (
    compute_abnormal_event_recall,
    compute_binary_auc,
    compute_multiclass_ovr_auc,
    compute_recall_by_class,
    compute_respiratory_detection_metrics,
    map_respiratory_event_type,
)
from sleepagent.schemas import RespiratoryEventType


NORMAL = RespiratoryEventType.NORMAL_BREATHING
HYPOPNEA = RespiratoryEventType.HYPOPNEA
APNEA = RespiratoryEventType.SUSPECTED_APNEA


def test_maps_respiratory_event_aliases() -> None:
    assert map_respiratory_event_type("normal breathing") == NORMAL
    assert map_respiratory_event_type("hypopnoea") == HYPOPNEA
    assert map_respiratory_event_type("apnea") == APNEA


def test_recall_by_class_and_abnormal_recall() -> None:
    y_true = [NORMAL, NORMAL, HYPOPNEA, HYPOPNEA, APNEA, APNEA]
    y_pred = [NORMAL, HYPOPNEA, HYPOPNEA, NORMAL, APNEA, HYPOPNEA]

    per_class_recall = compute_recall_by_class(y_true, y_pred)

    assert per_class_recall[NORMAL] == pytest.approx(0.5)
    assert per_class_recall[HYPOPNEA] == pytest.approx(0.5)
    assert per_class_recall[APNEA] == pytest.approx(0.5)
    assert compute_abnormal_event_recall(y_true, y_pred) == pytest.approx(0.75)


def test_respiratory_recall_returns_zero_for_missing_label_support() -> None:
    per_class_recall = compute_recall_by_class(
        y_true=[NORMAL, NORMAL],
        y_pred=[NORMAL, HYPOPNEA],
    )

    assert per_class_recall[NORMAL] == pytest.approx(0.5)
    assert per_class_recall[HYPOPNEA] == 0.0
    assert per_class_recall[APNEA] == 0.0


def test_abnormal_recall_returns_zero_when_no_true_abnormal_events() -> None:
    assert compute_abnormal_event_recall(
        y_true=[NORMAL, NORMAL],
        y_pred=[NORMAL, HYPOPNEA],
    ) == 0.0


def test_binary_auc_handles_ties() -> None:
    assert compute_binary_auc(
        y_true_binary=[1, 1, 0, 0],
        y_score=[0.9, 0.5, 0.5, 0.1],
    ) == pytest.approx(0.875)


def test_multiclass_ovr_auc_perfect_scores() -> None:
    y_true = [NORMAL, HYPOPNEA, APNEA, NORMAL, HYPOPNEA, APNEA]
    y_score = [
        {NORMAL: 0.90, HYPOPNEA: 0.05, APNEA: 0.05},
        {NORMAL: 0.10, HYPOPNEA: 0.80, APNEA: 0.10},
        {NORMAL: 0.05, HYPOPNEA: 0.10, APNEA: 0.85},
        {NORMAL: 0.85, HYPOPNEA: 0.10, APNEA: 0.05},
        {NORMAL: 0.10, HYPOPNEA: 0.75, APNEA: 0.15},
        {NORMAL: 0.10, HYPOPNEA: 0.20, APNEA: 0.70},
    ]

    assert compute_multiclass_ovr_auc(y_true, y_score) == pytest.approx(1.0)


def test_multiclass_ovr_auc_skips_classes_without_positive_samples() -> None:
    y_true = [NORMAL, NORMAL, HYPOPNEA, HYPOPNEA]
    y_score = [
        {NORMAL: 0.90, HYPOPNEA: 0.10, APNEA: 0.00},
        {NORMAL: 0.80, HYPOPNEA: 0.20, APNEA: 0.00},
        {NORMAL: 0.20, HYPOPNEA: 0.80, APNEA: 0.00},
        {NORMAL: 0.10, HYPOPNEA: 0.90, APNEA: 0.00},
    ]

    assert compute_multiclass_ovr_auc(y_true, y_score) == pytest.approx(1.0)


def test_compute_respiratory_detection_metrics_with_auc() -> None:
    y_true = [NORMAL, NORMAL, HYPOPNEA, HYPOPNEA, APNEA, APNEA]
    y_pred = [NORMAL, HYPOPNEA, HYPOPNEA, NORMAL, APNEA, HYPOPNEA]
    y_score = [
        {"normal_breathing": 0.90, "hypopnea": 0.05, "suspected_apnea": 0.05},
        {"normal_breathing": 0.65, "hypopnea": 0.25, "suspected_apnea": 0.10},
        {"normal_breathing": 0.10, "hypopnea": 0.80, "suspected_apnea": 0.10},
        {"normal_breathing": 0.20, "hypopnea": 0.65, "suspected_apnea": 0.10},
        {"normal_breathing": 0.05, "hypopnea": 0.10, "suspected_apnea": 0.85},
        {"normal_breathing": 0.10, "hypopnea": 0.55, "suspected_apnea": 0.35},
    ]

    metrics = compute_respiratory_detection_metrics(y_true, y_pred, y_score=y_score)

    assert metrics.recall == pytest.approx(0.75)
    assert metrics.auc == pytest.approx(1.0)
    assert metrics.f1 == pytest.approx((0.5 + 0.4 + (2 / 3)) / 3)
    assert metrics.per_class_recall[NORMAL] == pytest.approx(0.5)
    assert metrics.per_class_recall[HYPOPNEA] == pytest.approx(0.5)
    assert metrics.per_class_recall[APNEA] == pytest.approx(0.5)


def test_metrics_without_scores_leave_auc_empty() -> None:
    metrics = compute_respiratory_detection_metrics(
        y_true=[NORMAL, HYPOPNEA, APNEA],
        y_pred=[NORMAL, HYPOPNEA, HYPOPNEA],
    )

    assert metrics.auc is None


def test_respiratory_metrics_reject_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="Cannot map"):
        map_respiratory_event_type("central apnea")

    with pytest.raises(ValueError, match="cannot be empty"):
        compute_recall_by_class([], [])

    with pytest.raises(ValueError, match="same length"):
        compute_respiratory_detection_metrics([NORMAL], [NORMAL, HYPOPNEA])

    with pytest.raises(ValueError, match="cannot be empty"):
        compute_binary_auc([], [])

    with pytest.raises(ValueError, match="same length"):
        compute_binary_auc([1], [0.9, 0.1])

    with pytest.raises(ValueError, match="positive and one negative"):
        compute_binary_auc([1, 1], [0.9, 0.8])

    with pytest.raises(ValueError, match="same length"):
        compute_multiclass_ovr_auc([NORMAL], [{NORMAL: 0.9}, {NORMAL: 0.8}])

    with pytest.raises(ValueError, match="At least one class"):
        compute_multiclass_ovr_auc(
            y_true=[NORMAL, NORMAL],
            y_score=[
                {NORMAL: 0.9, HYPOPNEA: 0.1, APNEA: 0.0},
                {NORMAL: 0.8, HYPOPNEA: 0.2, APNEA: 0.0},
            ],
        )

    with pytest.raises(ValueError, match="Missing score"):
        compute_multiclass_ovr_auc(
            y_true=[NORMAL, HYPOPNEA],
            y_score=[
                {NORMAL: 0.9, HYPOPNEA: 0.1},
                {NORMAL: 0.1, HYPOPNEA: 0.9},
            ],
        )
