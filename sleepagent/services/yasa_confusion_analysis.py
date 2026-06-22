"""Confusion-matrix analysis for Stage 3 YASA-vs-SHHS sleep staging."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sleepagent.preprocessing.shhs_sleep_stages import extract_shhs_sleep_stage_sequence
from sleepagent.schemas import SleepEpoch, SleepStage


STAGE3_YASA_CONFUSION_ANALYSIS_SCHEMA_VERSION = "stage3.yasa_confusion_analysis.v1"
DEFAULT_CONFUSION_LABELS: tuple[SleepStage, ...] = (
    SleepStage.WAKE,
    SleepStage.REM,
    SleepStage.NREM,
)


@dataclass(frozen=True)
class YASAConfusionAnalysisResult:
    """Confusion analysis result for one YASA summary against one SHHS XML file."""

    yasa_summary_path: Path
    shhs_xml_path: Path
    compared_epoch_count: int
    total_error_count: int
    accuracy: float
    labels: tuple[SleepStage, ...]
    confusion_counts: dict[SleepStage, dict[SleepStage, int]]
    row_percentages: dict[SleepStage, dict[SleepStage, float]]
    top_confusions: list[dict[str, Any]]
    rem_nrem_confusion_count: int
    rem_nrem_confusion_share_of_errors: float | None


def analyze_yasa_shhs_confusion(
    *,
    yasa_summary_path: str | Path,
    shhs_xml_path: str | Path,
    labels: tuple[SleepStage, ...] = DEFAULT_CONFUSION_LABELS,
) -> YASAConfusionAnalysisResult:
    """Build a confusion matrix from a YASA summary JSON and SHHS XML labels."""
    summary_path = Path(yasa_summary_path).expanduser().resolve()
    predicted_epochs = _load_yasa_epochs(summary_path)
    reference_sequence = extract_shhs_sleep_stage_sequence(shhs_xml_path)
    compared_epoch_count = min(len(predicted_epochs), len(reference_sequence.epochs))
    if compared_epoch_count <= 0:
        raise ValueError("No overlapping epochs available for confusion analysis.")

    y_true = [epoch.stage for epoch in reference_sequence.epochs[:compared_epoch_count]]
    y_pred = [epoch.stage for epoch in predicted_epochs[:compared_epoch_count]]
    confusion_counts = _build_confusion_counts(y_true, y_pred, labels)
    correct_count = sum(confusion_counts[label][label] for label in labels)
    total_error_count = compared_epoch_count - correct_count
    rem_nrem_count = (
        confusion_counts[SleepStage.REM][SleepStage.NREM]
        + confusion_counts[SleepStage.NREM][SleepStage.REM]
    )

    return YASAConfusionAnalysisResult(
        yasa_summary_path=summary_path,
        shhs_xml_path=reference_sequence.path,
        compared_epoch_count=compared_epoch_count,
        total_error_count=total_error_count,
        accuracy=correct_count / compared_epoch_count,
        labels=labels,
        confusion_counts=confusion_counts,
        row_percentages=_row_percentages(confusion_counts, labels),
        top_confusions=_top_confusions(confusion_counts, labels),
        rem_nrem_confusion_count=rem_nrem_count,
        rem_nrem_confusion_share_of_errors=(
            rem_nrem_count / total_error_count if total_error_count else None
        ),
    )


def build_yasa_confusion_analysis_payload(result: YASAConfusionAnalysisResult) -> dict[str, Any]:
    """Build a JSON-safe confusion analysis payload."""
    return {
        "schema_version": STAGE3_YASA_CONFUSION_ANALYSIS_SCHEMA_VERSION,
        "yasa_summary_path": str(result.yasa_summary_path),
        "shhs_xml_path": str(result.shhs_xml_path),
        "compared_epoch_count": result.compared_epoch_count,
        "total_error_count": result.total_error_count,
        "accuracy": result.accuracy,
        "labels": [label.value for label in result.labels],
        "confusion_counts": _stringify_nested_stage_dict(result.confusion_counts),
        "row_percentages": _stringify_nested_stage_dict(result.row_percentages),
        "top_confusions": result.top_confusions,
        "rem_nrem_confusion_count": result.rem_nrem_confusion_count,
        "rem_nrem_confusion_share_of_errors": result.rem_nrem_confusion_share_of_errors,
        "notes": [
            "Rows are SHHS Profusion reference stages; columns are YASA predicted stages.",
            "Labels use the SleepAgent MVP Wake / REM / NREM taxonomy.",
        ],
    }


def write_yasa_confusion_analysis_payload(
    payload: dict[str, Any],
    output_path: str | Path,
) -> Path:
    """Write a Stage 3 YASA confusion analysis payload as JSON."""
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_yasa_epochs(summary_path: Path) -> list[SleepEpoch]:
    if not summary_path.exists():
        raise FileNotFoundError(f"YASA summary JSON not found: {summary_path}")
    with summary_path.open(encoding="utf-8") as file:
        payload = json.load(file)
    raw_epochs = payload.get("epochs")
    if not isinstance(raw_epochs, list) or not raw_epochs:
        raise ValueError("YASA summary payload must include a non-empty epochs list.")
    return [SleepEpoch.model_validate(epoch) for epoch in raw_epochs]


def _build_confusion_counts(
    y_true: list[SleepStage],
    y_pred: list[SleepStage],
    labels: tuple[SleepStage, ...],
) -> dict[SleepStage, dict[SleepStage, int]]:
    counts = {
        true_label: {pred_label: 0 for pred_label in labels}
        for true_label in labels
    }
    for true_label, pred_label in zip(y_true, y_pred, strict=True):
        if true_label in counts and pred_label in counts[true_label]:
            counts[true_label][pred_label] += 1
    return counts


def _row_percentages(
    counts: dict[SleepStage, dict[SleepStage, int]],
    labels: tuple[SleepStage, ...],
) -> dict[SleepStage, dict[SleepStage, float]]:
    percentages: dict[SleepStage, dict[SleepStage, float]] = {}
    for true_label in labels:
        row_total = sum(counts[true_label].values())
        percentages[true_label] = {
            pred_label: (counts[true_label][pred_label] / row_total if row_total else 0.0)
            for pred_label in labels
        }
    return percentages


def _top_confusions(
    counts: dict[SleepStage, dict[SleepStage, int]],
    labels: tuple[SleepStage, ...],
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    total_errors = sum(
        count
        for true_label in labels
        for pred_label, count in counts[true_label].items()
        if true_label != pred_label
    )
    for true_label in labels:
        for pred_label in labels:
            if true_label == pred_label:
                continue
            count = counts[true_label][pred_label]
            if count:
                pairs.append(
                    {
                        "true_stage": true_label.value,
                        "predicted_stage": pred_label.value,
                        "count": count,
                        "share_of_errors": count / total_errors if total_errors else None,
                    }
                )
    return sorted(pairs, key=lambda pair: pair["count"], reverse=True)


def _stringify_nested_stage_dict(
    values: dict[SleepStage, dict[SleepStage, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        true_label.value: {
            pred_label.value: value
            for pred_label, value in row.items()
        }
        for true_label, row in values.items()
    }
