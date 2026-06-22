import numpy as np
import pytest

from sleepagent.schemas import RespiratoryEventType
from sleepagent.training import evaluate_respiratory_model_outputs


NORMAL = RespiratoryEventType.NORMAL_BREATHING
HYPOPNEA = RespiratoryEventType.HYPOPNEA
APNEA = RespiratoryEventType.SUSPECTED_APNEA


def test_evaluates_logits_as_respiratory_metrics() -> None:
    y_true = np.asarray([0, 0, 1, 1, 2, 2], dtype=np.int64)
    logits = np.asarray(
        [
            [4.0, 1.0, 0.0],
            [0.5, 2.0, 0.1],
            [0.1, 3.0, 0.2],
            [2.5, 1.0, 0.0],
            [0.0, 0.5, 4.0],
            [0.1, 3.0, 2.0],
        ],
        dtype=np.float32,
    )

    result = evaluate_respiratory_model_outputs(y_true, logits, from_logits=True)

    assert result.y_true == (NORMAL, NORMAL, HYPOPNEA, HYPOPNEA, APNEA, APNEA)
    assert result.y_pred == (NORMAL, HYPOPNEA, HYPOPNEA, NORMAL, APNEA, HYPOPNEA)
    assert result.metrics.recall == pytest.approx(0.75)
    assert result.metrics.auc == pytest.approx(0.875)
    assert result.metrics.f1 == pytest.approx((0.5 + 0.4 + (2 / 3)) / 3)
    assert set(result.score_rows[0]) == {
        "normal_breathing",
        "hypopnea",
        "suspected_apnea",
    }
    assert sum(result.score_rows[0].values()) == pytest.approx(1.0)


def test_evaluates_probability_scores_without_softmax() -> None:
    y_true = [0, 1, 2]
    probabilities = [
        [0.8, 0.1, 0.1],
        [0.1, 0.7, 0.2],
        [0.2, 0.3, 0.5],
    ]

    result = evaluate_respiratory_model_outputs(
        y_true,
        probabilities,
        from_logits=False,
    )

    assert result.y_pred == (NORMAL, HYPOPNEA, APNEA)
    assert result.metrics.recall == pytest.approx(1.0)
    assert result.metrics.f1 == pytest.approx(1.0)
    assert result.metrics.auc == pytest.approx(1.0)


def test_evaluation_leaves_auc_empty_when_auc_is_not_defined() -> None:
    result = evaluate_respiratory_model_outputs(
        y_true=[0, 0],
        outputs=[
            [0.8, 0.1, 0.1],
            [0.7, 0.2, 0.1],
        ],
        from_logits=False,
    )

    assert result.metrics.auc is None
    assert result.auc_warning_message is not None
    assert result.metrics.f1 >= 0.0


def test_evaluation_respects_custom_class_order() -> None:
    y_true = [1, 0]
    probabilities = [
        [0.1, 0.8, 0.1],
        [0.9, 0.1, 0.0],
    ]

    result = evaluate_respiratory_model_outputs(
        y_true,
        probabilities,
        class_order=(HYPOPNEA, NORMAL, APNEA),
        from_logits=False,
    )

    assert result.y_true == (NORMAL, HYPOPNEA)
    assert result.y_pred == (NORMAL, HYPOPNEA)


def test_evaluation_rejects_invalid_shapes_and_labels() -> None:
    with pytest.raises(ValueError, match="one-dimensional"):
        evaluate_respiratory_model_outputs([[0]], [[1.0, 0.0, 0.0]])

    with pytest.raises(ValueError, match="same example count"):
        evaluate_respiratory_model_outputs([0], [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])

    with pytest.raises(ValueError, match="class dimension"):
        evaluate_respiratory_model_outputs([0], [[1.0, 0.0]])

    with pytest.raises(ValueError, match="valid class_order indices"):
        evaluate_respiratory_model_outputs([3], [[1.0, 0.0, 0.0]])

    with pytest.raises(ValueError, match="negative"):
        evaluate_respiratory_model_outputs(
            [0],
            [[0.5, -0.1, 0.6]],
            from_logits=False,
        )
