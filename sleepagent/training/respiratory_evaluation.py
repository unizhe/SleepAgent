"""Stage 6 respiratory evaluation helpers."""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sleepagent.metrics.respiratory import compute_respiratory_detection_metrics
from sleepagent.models.respiratory_contract import DEFAULT_RESPIRATORY_CLASS_ORDER
from sleepagent.schemas import RespiratoryDetectionMetrics, RespiratoryEventType


@dataclass(frozen=True)
class RespiratoryEvaluationResult:
    """Decoded model outputs and respiratory detection metrics."""

    metrics: RespiratoryDetectionMetrics
    y_true: tuple[RespiratoryEventType, ...]
    y_pred: tuple[RespiratoryEventType, ...]
    score_rows: tuple[dict[str, float], ...]
    auc_warning_message: str | None = None


def evaluate_respiratory_model_outputs(
    y_true: Sequence[int] | Any,
    outputs: Sequence[Sequence[float]] | Any,
    *,
    class_order: Sequence[RespiratoryEventType] = DEFAULT_RESPIRATORY_CLASS_ORDER,
    from_logits: bool = True,
) -> RespiratoryEvaluationResult:
    """Convert model logits/probabilities into Recall, AUC, and F1 metrics."""
    np = _import_numpy()
    resolved_class_order = _validate_class_order(class_order)
    y_true_array = _to_numpy_array(y_true, np=np)
    output_array = _to_numpy_array(outputs, np=np)

    if y_true_array.ndim != 1:
        raise ValueError("y_true must be a one-dimensional label-index sequence.")
    if output_array.ndim != 2:
        raise ValueError("outputs must have shape (examples, classes).")
    if y_true_array.shape[0] == 0:
        raise ValueError("y_true and outputs cannot be empty.")
    if output_array.shape[0] != y_true_array.shape[0]:
        raise ValueError("outputs and y_true must contain the same example count.")
    if output_array.shape[1] != len(resolved_class_order):
        raise ValueError("outputs class dimension must match class_order length.")
    if not np.isfinite(output_array).all():
        raise ValueError("outputs must contain only finite values.")

    y_true_indices = y_true_array.astype(np.int64, copy=False)
    _validate_label_indices(y_true_indices, class_order=resolved_class_order)

    score_array = (
        _softmax(output_array.astype(float, copy=False), np=np)
        if from_logits
        else output_array.astype(float, copy=False)
    )
    if not from_logits:
        _validate_probability_scores(score_array, np=np)

    y_pred_indices = score_array.argmax(axis=1)
    y_true_labels = tuple(
        resolved_class_order[int(label_index)]
        for label_index in y_true_indices
    )
    y_pred_labels = tuple(
        resolved_class_order[int(label_index)]
        for label_index in y_pred_indices
    )
    score_rows = tuple(
        {
            label.value: float(score)
            for label, score in zip(resolved_class_order, row, strict=True)
        }
        for row in score_array
    )
    metrics, auc_warning_message = _compute_metrics_with_optional_auc(
        y_true=y_true_labels,
        y_pred=y_pred_labels,
        y_score=score_rows,
    )
    return RespiratoryEvaluationResult(
        metrics=metrics,
        y_true=y_true_labels,
        y_pred=y_pred_labels,
        score_rows=score_rows,
        auc_warning_message=auc_warning_message,
    )


def _compute_metrics_with_optional_auc(
    *,
    y_true: tuple[RespiratoryEventType, ...],
    y_pred: tuple[RespiratoryEventType, ...],
    y_score: tuple[dict[str, float], ...],
) -> tuple[RespiratoryDetectionMetrics, str | None]:
    try:
        return (
            compute_respiratory_detection_metrics(
                y_true=y_true,
                y_pred=y_pred,
                y_score=y_score,
            ),
            None,
        )
    except ValueError as exc:
        message = str(exc)
        if "AUC" not in message and "positive and negative" not in message:
            raise
        return (
            compute_respiratory_detection_metrics(
                y_true=y_true,
                y_pred=y_pred,
                y_score=None,
            ),
            message,
        )


def _validate_class_order(
    class_order: Sequence[RespiratoryEventType],
) -> tuple[RespiratoryEventType, ...]:
    resolved_class_order = tuple(class_order)
    if not resolved_class_order:
        raise ValueError("class_order cannot be empty.")
    if len(set(resolved_class_order)) != len(resolved_class_order):
        raise ValueError("class_order cannot contain duplicates.")
    return resolved_class_order


def _validate_label_indices(
    y_true_indices: Any,
    *,
    class_order: tuple[RespiratoryEventType, ...],
) -> None:
    min_label = int(y_true_indices.min())
    max_label = int(y_true_indices.max())
    if min_label < 0 or max_label >= len(class_order):
        raise ValueError("y_true labels must be valid class_order indices.")


def _validate_probability_scores(scores: Any, *, np: Any) -> None:
    if not np.isfinite(scores).all():
        raise ValueError("probability scores must contain only finite values.")
    if (scores < 0).any():
        raise ValueError("probability scores cannot be negative.")


def _softmax(logits: Any, *, np: Any) -> Any:
    shifted = logits - logits.max(axis=1, keepdims=True)
    exp_scores = np.exp(shifted)
    return exp_scores / exp_scores.sum(axis=1, keepdims=True)


def _to_numpy_array(value: Any, *, np: Any) -> Any:
    detach = getattr(value, "detach", None)
    if callable(detach):
        value = detach()
    cpu = getattr(value, "cpu", None)
    if callable(cpu):
        value = cpu()
    return np.asarray(value)


def _import_numpy() -> Any:
    try:
        return importlib.import_module("numpy")
    except ImportError as exc:
        raise RuntimeError(
            "NumPy is required to evaluate respiratory model outputs."
        ) from exc
