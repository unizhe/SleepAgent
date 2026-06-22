"""Evaluation metrics for SleepAgent models."""

from sleepagent.metrics.classification import (
    DEFAULT_SLEEP_STAGE_LABELS,
    compute_accuracy,
    compute_cohen_kappa,
    compute_f1_by_class,
    compute_macro_f1,
    compute_sleep_staging_metrics,
    compute_weighted_f1,
)
from sleepagent.metrics.respiratory import (
    ABNORMAL_RESPIRATORY_EVENT_LABELS,
    DEFAULT_RESPIRATORY_EVENT_LABELS,
    compute_abnormal_event_recall,
    compute_binary_auc,
    compute_multiclass_ovr_auc,
    compute_recall_by_class,
    compute_respiratory_detection_metrics,
    map_respiratory_event_type,
)

__all__ = [
    "ABNORMAL_RESPIRATORY_EVENT_LABELS",
    "DEFAULT_RESPIRATORY_EVENT_LABELS",
    "DEFAULT_SLEEP_STAGE_LABELS",
    "compute_accuracy",
    "compute_abnormal_event_recall",
    "compute_binary_auc",
    "compute_cohen_kappa",
    "compute_f1_by_class",
    "compute_macro_f1",
    "compute_multiclass_ovr_auc",
    "compute_recall_by_class",
    "compute_respiratory_detection_metrics",
    "compute_sleep_staging_metrics",
    "compute_weighted_f1",
    "map_respiratory_event_type",
]
