"""Evaluate Stage 3 YASA sleep staging output against SHHS XML labels."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sleepagent.metrics import compute_sleep_staging_metrics
from sleepagent.models import summarize_sleep_epochs
from sleepagent.preprocessing.shhs_sleep_stages import (
    SHHSSleepStageSequence,
    extract_shhs_sleep_stage_sequence,
)
from sleepagent.schemas import SleepEpoch, SleepStagingMetrics


STAGE3_YASA_SHHS_EVALUATION_SCHEMA_VERSION = "stage3.yasa_shhs_evaluation.v1"


@dataclass(frozen=True)
class YASASHHSEvaluationResult:
    """YASA-vs-SHHS sleep staging evaluation result."""

    yasa_summary_path: Path
    shhs_xml_path: Path
    shhs_stage_source: str
    yasa_epoch_count: int
    shhs_epoch_count: int
    compared_epoch_count: int
    dropped_yasa_epochs: int
    dropped_shhs_epochs: int
    metrics: SleepStagingMetrics
    reference_summary: dict[str, Any]
    prediction_summary: dict[str, Any]


def evaluate_yasa_summary_against_shhs_xml(
    *,
    yasa_summary_path: str | Path,
    shhs_xml_path: str | Path,
) -> YASASHHSEvaluationResult:
    """Evaluate a Stage 3 YASA runner JSON payload against SHHS XML stages."""
    summary_path = Path(yasa_summary_path).expanduser().resolve()
    payload = _load_yasa_summary_payload(summary_path)
    predicted_epochs = _load_yasa_epochs(payload)
    reference_sequence = extract_shhs_sleep_stage_sequence(shhs_xml_path)
    compared_epoch_count = min(len(predicted_epochs), len(reference_sequence.epochs))
    if compared_epoch_count <= 0:
        raise ValueError("No overlapping epochs available for YASA-vs-SHHS evaluation.")

    y_true = [epoch.stage for epoch in reference_sequence.epochs[:compared_epoch_count]]
    y_pred = [epoch.stage for epoch in predicted_epochs[:compared_epoch_count]]
    metrics = compute_sleep_staging_metrics(y_true, y_pred)

    return YASASHHSEvaluationResult(
        yasa_summary_path=summary_path,
        shhs_xml_path=reference_sequence.path,
        shhs_stage_source=reference_sequence.source,
        yasa_epoch_count=len(predicted_epochs),
        shhs_epoch_count=len(reference_sequence.epochs),
        compared_epoch_count=compared_epoch_count,
        dropped_yasa_epochs=max(len(predicted_epochs) - compared_epoch_count, 0),
        dropped_shhs_epochs=max(len(reference_sequence.epochs) - compared_epoch_count, 0),
        metrics=metrics,
        reference_summary=summarize_sleep_epochs(
            reference_sequence.epochs[:compared_epoch_count],
        ).model_dump(mode="json"),
        prediction_summary=payload.get("sleep_summary", {}),
    )


def build_yasa_shhs_evaluation_payload(
    result: YASASHHSEvaluationResult,
) -> dict[str, Any]:
    """Build a JSON-safe payload for Stage 3 YASA-vs-SHHS evaluation."""
    return {
        "schema_version": STAGE3_YASA_SHHS_EVALUATION_SCHEMA_VERSION,
        "yasa_summary_path": str(result.yasa_summary_path),
        "shhs_xml_path": str(result.shhs_xml_path),
        "shhs_stage_source": result.shhs_stage_source,
        "yasa_epoch_count": result.yasa_epoch_count,
        "shhs_epoch_count": result.shhs_epoch_count,
        "compared_epoch_count": result.compared_epoch_count,
        "dropped_yasa_epochs": result.dropped_yasa_epochs,
        "dropped_shhs_epochs": result.dropped_shhs_epochs,
        "metrics": result.metrics.model_dump(mode="json"),
        "reference_sleep_summary": result.reference_summary,
        "prediction_sleep_summary": result.prediction_summary,
        "notes": [
            "Stage 3 local YASA-vs-SHHS sleep staging evaluation.",
            "SHHS labels are mapped to the MVP Wake / REM / NREM taxonomy.",
            "If lengths differ, metrics use the leading overlapping epoch range only.",
        ],
    }


def write_yasa_shhs_evaluation_payload(
    payload: dict[str, Any],
    output_path: str | Path,
) -> Path:
    """Write a Stage 3 YASA-vs-SHHS evaluation payload as JSON."""
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_yasa_summary_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"YASA summary JSON not found: {path}")
    if not path.is_file():
        raise ValueError(f"YASA summary path is not a file: {path}")
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError("YASA summary JSON must contain an object payload.")
    return payload


def _load_yasa_epochs(payload: dict[str, Any]) -> list[SleepEpoch]:
    raw_epochs = payload.get("epochs")
    if not isinstance(raw_epochs, list) or not raw_epochs:
        raise ValueError("YASA summary payload must include a non-empty epochs list.")
    return [SleepEpoch.model_validate(epoch) for epoch in raw_epochs]
