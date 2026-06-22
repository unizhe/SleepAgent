from collections import Counter
from collections.abc import Sequence

from sleepagent.preprocessing import StageMappingSource, map_sleep_stage
from sleepagent.schemas import SleepStage, SleepStagingMetrics


DEFAULT_SLEEP_STAGE_LABELS: tuple[SleepStage, ...] = (
    SleepStage.WAKE,
    SleepStage.REM,
    SleepStage.NREM,
)


def compute_accuracy(y_true: Sequence[object], y_pred: Sequence[object]) -> float:
    true_labels, pred_labels = _validate_label_sequences(y_true, y_pred)
    correct = sum(true == pred for true, pred in zip(true_labels, pred_labels, strict=True))
    return correct / len(true_labels)


def compute_cohen_kappa(
    y_true: Sequence[object],
    y_pred: Sequence[object],
    labels: Sequence[object] | None = None,
) -> float:
    true_labels, pred_labels = _validate_label_sequences(y_true, y_pred)
    resolved_labels = _resolve_labels(true_labels, pred_labels, labels)
    observed = compute_accuracy(true_labels, pred_labels)

    true_counts = Counter(true_labels)
    pred_counts = Counter(pred_labels)
    total = len(true_labels)
    expected = sum(
        true_counts[label] * pred_counts[label]
        for label in resolved_labels
    ) / (total * total)

    denominator = 1 - expected
    if denominator == 0:
        return 1.0 if observed == 1 else 0.0
    return (observed - expected) / denominator


def compute_f1_by_class(
    y_true: Sequence[object],
    y_pred: Sequence[object],
    labels: Sequence[object] | None = None,
) -> dict[object, float]:
    true_labels, pred_labels = _validate_label_sequences(y_true, y_pred)
    resolved_labels = _resolve_labels(true_labels, pred_labels, labels)
    scores: dict[object, float] = {}

    for label in resolved_labels:
        tp = sum(
            true == label and pred == label
            for true, pred in zip(true_labels, pred_labels, strict=True)
        )
        fp = sum(
            true != label and pred == label
            for true, pred in zip(true_labels, pred_labels, strict=True)
        )
        fn = sum(
            true == label and pred != label
            for true, pred in zip(true_labels, pred_labels, strict=True)
        )
        denominator = (2 * tp) + fp + fn
        scores[label] = (2 * tp / denominator) if denominator else 0.0

    return scores


def compute_macro_f1(
    y_true: Sequence[object],
    y_pred: Sequence[object],
    labels: Sequence[object] | None = None,
) -> float:
    per_class_f1 = compute_f1_by_class(y_true, y_pred, labels=labels)
    return sum(per_class_f1.values()) / len(per_class_f1)


def compute_weighted_f1(
    y_true: Sequence[object],
    y_pred: Sequence[object],
    labels: Sequence[object] | None = None,
) -> float:
    true_labels, pred_labels = _validate_label_sequences(y_true, y_pred)
    resolved_labels = _resolve_labels(true_labels, pred_labels, labels)
    per_class_f1 = compute_f1_by_class(true_labels, pred_labels, labels=resolved_labels)
    true_counts = Counter(true_labels)
    total_support = sum(true_counts[label] for label in resolved_labels)
    if total_support == 0:
        return 0.0
    return sum(
        per_class_f1[label] * true_counts[label]
        for label in resolved_labels
    ) / total_support


def compute_sleep_staging_metrics(
    y_true: Sequence[object],
    y_pred: Sequence[object],
    true_source: StageMappingSource = "auto",
    pred_source: StageMappingSource = "auto",
) -> SleepStagingMetrics:
    true_stages = _map_stage_sequence(y_true, source=true_source)
    pred_stages = _map_stage_sequence(y_pred, source=pred_source)
    labels = DEFAULT_SLEEP_STAGE_LABELS

    return SleepStagingMetrics(
        accuracy=compute_accuracy(true_stages, pred_stages),
        cohen_kappa=compute_cohen_kappa(true_stages, pred_stages, labels=labels),
        macro_f1=compute_macro_f1(true_stages, pred_stages, labels=labels),
        weighted_f1=compute_weighted_f1(true_stages, pred_stages, labels=labels),
        per_class_f1={
            stage: score
            for stage, score in compute_f1_by_class(true_stages, pred_stages, labels=labels).items()
        },
    )


def _map_stage_sequence(
    labels: Sequence[object],
    source: StageMappingSource,
) -> list[SleepStage]:
    mapped: list[SleepStage] = []
    for label in labels:
        stage = map_sleep_stage(label, source=source, unknown_policy="raise")
        if stage is None:
            raise ValueError(f"Cannot ignore label {label!r} when computing metrics.")
        mapped.append(stage)
    return mapped


def _validate_label_sequences(
    y_true: Sequence[object],
    y_pred: Sequence[object],
) -> tuple[list[object], list[object]]:
    true_labels = list(y_true)
    pred_labels = list(y_pred)

    if not true_labels:
        raise ValueError("y_true and y_pred cannot be empty.")
    if len(true_labels) != len(pred_labels):
        raise ValueError("y_true and y_pred must have the same length.")

    return true_labels, pred_labels


def _resolve_labels(
    y_true: Sequence[object],
    y_pred: Sequence[object],
    labels: Sequence[object] | None,
) -> list[object]:
    if labels is not None:
        resolved = list(labels)
    else:
        resolved = list(dict.fromkeys([*y_true, *y_pred]))

    if not resolved:
        raise ValueError("labels cannot be empty.")
    return resolved

