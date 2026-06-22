from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


RESPIRATORY_MODEL_STATUS_NOT_VALIDATED = "not_validated_for_risk_conclusion"
RESPIRATORY_MODEL_STATUS_VALIDATED = "validated_demo_for_agent_risk"


@dataclass(frozen=True)
class RespiratoryModelGateThresholds:
    min_abnormal_recall: float = 0.80
    min_abnormal_f1: float = 0.50
    min_auc: float = 0.85
    require_fixed_test_split: bool = True
    require_external_holdout_split: bool = True
    reject_all_normal_collapse: bool = True


@dataclass(frozen=True)
class RespiratoryModelGateResult:
    respiratory_model_status: str
    passed: bool
    reasons: list[str]
    thresholds: RespiratoryModelGateThresholds

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_respiratory_model_gate(
    *,
    metrics: dict[str, float | int | None],
    predicted_class_counts: dict[str, int] | None = None,
    fixed_test_split_passed: bool = False,
    external_holdout_split_passed: bool = False,
    thresholds: RespiratoryModelGateThresholds | None = None,
) -> RespiratoryModelGateResult:
    resolved_thresholds = thresholds or RespiratoryModelGateThresholds()
    reasons: list[str] = []
    recall = _float_metric(metrics, "recall")
    f1 = _float_metric(metrics, "f1")
    auc = _float_metric(metrics, "auc")

    if recall is None or recall < resolved_thresholds.min_abnormal_recall:
        reasons.append("abnormal_recall_below_gate")
    if f1 is None or f1 < resolved_thresholds.min_abnormal_f1:
        reasons.append("abnormal_f1_below_gate")
    if auc is None or auc < resolved_thresholds.min_auc:
        reasons.append("auc_below_gate")
    if resolved_thresholds.require_fixed_test_split and not fixed_test_split_passed:
        reasons.append("fixed_test_split_not_passed")
    if (
        resolved_thresholds.require_external_holdout_split
        and not external_holdout_split_passed
    ):
        reasons.append("external_holdout_split_not_passed")
    if resolved_thresholds.reject_all_normal_collapse and _is_all_normal_collapse(
        predicted_class_counts or {}
    ):
        reasons.append("all_normal_prediction_collapse")

    passed = not reasons
    return RespiratoryModelGateResult(
        respiratory_model_status=(
            RESPIRATORY_MODEL_STATUS_VALIDATED
            if passed
            else RESPIRATORY_MODEL_STATUS_NOT_VALIDATED
        ),
        passed=passed,
        reasons=reasons,
        thresholds=resolved_thresholds,
    )


def not_validated_respiratory_model_gate(reason: str) -> RespiratoryModelGateResult:
    return RespiratoryModelGateResult(
        respiratory_model_status=RESPIRATORY_MODEL_STATUS_NOT_VALIDATED,
        passed=False,
        reasons=[reason],
        thresholds=RespiratoryModelGateThresholds(),
    )


def _float_metric(
    metrics: dict[str, float | int | None],
    key: str,
) -> float | None:
    value = metrics.get(key)
    if value is None:
        return None
    return float(value)


def _is_all_normal_collapse(predicted_class_counts: dict[str, int]) -> bool:
    non_zero = {
        label
        for label, count in predicted_class_counts.items()
        if count > 0
    }
    return non_zero == {"normal_breathing"}
