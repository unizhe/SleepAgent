from collections.abc import Mapping, Sequence

from sleepagent.metrics.classification import compute_macro_f1
from sleepagent.schemas import RespiratoryDetectionMetrics, RespiratoryEventType


DEFAULT_RESPIRATORY_EVENT_LABELS: tuple[RespiratoryEventType, ...] = (
    RespiratoryEventType.NORMAL_BREATHING,
    RespiratoryEventType.HYPOPNEA,
    RespiratoryEventType.SUSPECTED_APNEA,
)

ABNORMAL_RESPIRATORY_EVENT_LABELS: tuple[RespiratoryEventType, ...] = (
    RespiratoryEventType.HYPOPNEA,
    RespiratoryEventType.SUSPECTED_APNEA,
)

_RESPIRATORY_LABEL_ALIASES = {
    "normal": RespiratoryEventType.NORMAL_BREATHING,
    "normal breathing": RespiratoryEventType.NORMAL_BREATHING,
    "normal_breathing": RespiratoryEventType.NORMAL_BREATHING,
    "breathing": RespiratoryEventType.NORMAL_BREATHING,
    "hypopnea": RespiratoryEventType.HYPOPNEA,
    "hypopnoea": RespiratoryEventType.HYPOPNEA,
    "suspected apnea": RespiratoryEventType.SUSPECTED_APNEA,
    "suspected_apnea": RespiratoryEventType.SUSPECTED_APNEA,
    "apnea": RespiratoryEventType.SUSPECTED_APNEA,
    "apnoea": RespiratoryEventType.SUSPECTED_APNEA,
}


def map_respiratory_event_type(label: object) -> RespiratoryEventType:
    if isinstance(label, RespiratoryEventType):
        return label
    if isinstance(label, str):
        normalized = _normalize_label(label)
        if normalized in _RESPIRATORY_LABEL_ALIASES:
            return _RESPIRATORY_LABEL_ALIASES[normalized]
    raise ValueError(f"Cannot map respiratory event label {label!r}.")


def compute_recall_by_class(
    y_true: Sequence[object],
    y_pred: Sequence[object],
    labels: Sequence[RespiratoryEventType] = DEFAULT_RESPIRATORY_EVENT_LABELS,
) -> dict[RespiratoryEventType, float]:
    true_labels, pred_labels = _map_respiratory_sequences(y_true, y_pred)
    recalls: dict[RespiratoryEventType, float] = {}

    for label in labels:
        tp = sum(
            true == label and pred == label
            for true, pred in zip(true_labels, pred_labels, strict=True)
        )
        fn = sum(
            true == label and pred != label
            for true, pred in zip(true_labels, pred_labels, strict=True)
        )
        denominator = tp + fn
        recalls[label] = tp / denominator if denominator else 0.0

    return recalls


def compute_abnormal_event_recall(
    y_true: Sequence[object],
    y_pred: Sequence[object],
    abnormal_labels: Sequence[RespiratoryEventType] = ABNORMAL_RESPIRATORY_EVENT_LABELS,
) -> float:
    true_labels, pred_labels = _map_respiratory_sequences(y_true, y_pred)
    abnormal_set = set(abnormal_labels)
    true_abnormal_count = sum(true in abnormal_set for true in true_labels)
    if true_abnormal_count == 0:
        return 0.0
    detected_abnormal_count = sum(
        true in abnormal_set and pred in abnormal_set
        for true, pred in zip(true_labels, pred_labels, strict=True)
    )
    return detected_abnormal_count / true_abnormal_count


def compute_binary_auc(y_true_binary: Sequence[bool | int], y_score: Sequence[float]) -> float:
    true_values = [bool(value) for value in y_true_binary]
    scores = list(y_score)
    if not true_values:
        raise ValueError("y_true_binary and y_score cannot be empty.")
    if len(true_values) != len(scores):
        raise ValueError("y_true_binary and y_score must have the same length.")

    positive_scores = [
        score
        for label, score in zip(true_values, scores, strict=True)
        if label
    ]
    negative_scores = [
        score
        for label, score in zip(true_values, scores, strict=True)
        if not label
    ]
    if not positive_scores or not negative_scores:
        raise ValueError("AUC requires at least one positive and one negative sample.")

    pair_score = 0.0
    for positive_score in positive_scores:
        for negative_score in negative_scores:
            if positive_score > negative_score:
                pair_score += 1.0
            elif positive_score == negative_score:
                pair_score += 0.5

    return pair_score / (len(positive_scores) * len(negative_scores))


def compute_multiclass_ovr_auc(
    y_true: Sequence[object],
    y_score: Sequence[Mapping[object, float]],
    labels: Sequence[RespiratoryEventType] = DEFAULT_RESPIRATORY_EVENT_LABELS,
) -> float:
    true_labels = [map_respiratory_event_type(label) for label in y_true]
    score_rows = list(y_score)
    if not true_labels:
        raise ValueError("y_true and y_score cannot be empty.")
    if len(true_labels) != len(score_rows):
        raise ValueError("y_true and y_score must have the same length.")

    auc_scores: list[float] = []
    for label in labels:
        binary_labels = [true == label for true in true_labels]
        class_scores = [_score_for_label(row, label) for row in score_rows]
        try:
            auc_scores.append(compute_binary_auc(binary_labels, class_scores))
        except ValueError:
            continue

    if not auc_scores:
        raise ValueError("At least one class must have positive and negative samples for AUC.")
    return sum(auc_scores) / len(auc_scores)


def compute_respiratory_detection_metrics(
    y_true: Sequence[object],
    y_pred: Sequence[object],
    y_score: Sequence[Mapping[object, float]] | None = None,
) -> RespiratoryDetectionMetrics:
    true_labels, pred_labels = _map_respiratory_sequences(y_true, y_pred)
    labels = DEFAULT_RESPIRATORY_EVENT_LABELS

    auc = None
    if y_score is not None:
        auc = compute_multiclass_ovr_auc(true_labels, y_score, labels=labels)

    return RespiratoryDetectionMetrics(
        recall=compute_abnormal_event_recall(true_labels, pred_labels),
        auc=auc,
        f1=compute_macro_f1(true_labels, pred_labels, labels=labels),
        per_class_recall=compute_recall_by_class(true_labels, pred_labels, labels=labels),
    )


def _map_respiratory_sequences(
    y_true: Sequence[object],
    y_pred: Sequence[object],
) -> tuple[list[RespiratoryEventType], list[RespiratoryEventType]]:
    true_labels = [map_respiratory_event_type(label) for label in y_true]
    pred_labels = [map_respiratory_event_type(label) for label in y_pred]
    if not true_labels:
        raise ValueError("y_true and y_pred cannot be empty.")
    if len(true_labels) != len(pred_labels):
        raise ValueError("y_true and y_pred must have the same length.")
    return true_labels, pred_labels


def _score_for_label(
    score_row: Mapping[object, float],
    label: RespiratoryEventType,
) -> float:
    if label in score_row:
        return score_row[label]
    if label.value in score_row:
        return score_row[label.value]
    raise ValueError(f"Missing score for respiratory label {label.value!r}.")


def _normalize_label(label: str) -> str:
    return " ".join(
        label.strip().lower().replace("-", " ").replace("_", " ").split()
    )
