"""Analyze Stage 3 YASA small-batch reproduction metrics."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any


STAGE3_YASA_BATCH_ANALYSIS_SCHEMA_VERSION = "stage3.yasa_batch_analysis.v1"
DEFAULT_BATCH_WARNING_THRESHOLDS = {
    "accuracy": 0.90,
    "cohen_kappa": 0.80,
    "macro_f1": 0.85,
    "weighted_f1": 0.90,
}
CORE_BATCH_METRICS = ("accuracy", "cohen_kappa", "macro_f1", "weighted_f1")


@dataclass(frozen=True)
class YASABatchAnalysisResult:
    """Analysis result for a Stage 3 YASA batch summary payload."""

    batch_summary_path: Path
    success_count: int
    failure_count: int
    metric_summary: dict[str, dict[str, float | None]]
    per_class_f1_summary: dict[str, dict[str, float | None]]
    lowest_records: list[dict[str, Any]]
    records_below_thresholds: list[dict[str, Any]]
    failures: list[dict[str, Any]]


def analyze_yasa_batch_summary(
    batch_summary_path: str | Path,
    *,
    thresholds: dict[str, float] | None = None,
    lowest_limit: int = 5,
) -> YASABatchAnalysisResult:
    """Analyze a Stage 3 YASA batch summary JSON file."""
    path = Path(batch_summary_path).expanduser().resolve()
    payload = _load_batch_summary_payload(path)
    successes = _successes(payload)
    failures = _failures(payload)
    active_thresholds = dict(DEFAULT_BATCH_WARNING_THRESHOLDS)
    if thresholds:
        active_thresholds.update(thresholds)

    return YASABatchAnalysisResult(
        batch_summary_path=path,
        success_count=len(successes),
        failure_count=len(failures),
        metric_summary=_summarize_core_metrics(successes),
        per_class_f1_summary=_summarize_per_class_f1(successes),
        lowest_records=_lowest_records(successes, lowest_limit),
        records_below_thresholds=_records_below_thresholds(successes, active_thresholds),
        failures=failures,
    )


def build_yasa_batch_analysis_payload(result: YASABatchAnalysisResult) -> dict[str, Any]:
    """Build a JSON-safe analysis payload for Stage 3 batch metrics."""
    return {
        "schema_version": STAGE3_YASA_BATCH_ANALYSIS_SCHEMA_VERSION,
        "batch_summary_path": str(result.batch_summary_path),
        "success_count": result.success_count,
        "failure_count": result.failure_count,
        "metric_summary": result.metric_summary,
        "per_class_f1_summary": result.per_class_f1_summary,
        "lowest_records": result.lowest_records,
        "records_below_thresholds": result.records_below_thresholds,
        "failures": result.failures,
        "notes": [
            "Stage 3 local YASA batch metric distribution analysis.",
            "Threshold flags are advisory and are meant to choose follow-up inspection samples.",
        ],
    }


def write_yasa_batch_analysis_payload(payload: dict[str, Any], output_path: str | Path) -> Path:
    """Write a Stage 3 YASA batch analysis payload as JSON."""
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_batch_summary_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"YASA batch summary JSON not found: {path}")
    if not path.is_file():
        raise ValueError(f"YASA batch summary path is not a file: {path}")
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError("YASA batch summary JSON must contain an object payload.")
    return payload


def _successes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_successes = payload.get("successes", [])
    if not isinstance(raw_successes, list):
        raise ValueError("YASA batch summary 'successes' must be a list.")
    for success in raw_successes:
        if not isinstance(success, dict):
            raise ValueError("Each YASA batch success entry must be an object.")
        if not isinstance(success.get("metrics"), dict):
            raise ValueError("Each YASA batch success entry must include metrics.")
    return raw_successes


def _failures(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_failures = payload.get("failures", [])
    if not isinstance(raw_failures, list):
        raise ValueError("YASA batch summary 'failures' must be a list.")
    return [failure for failure in raw_failures if isinstance(failure, dict)]


def _summarize_core_metrics(successes: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    return {
        metric: _summarize_values(_metric_values(successes, metric))
        for metric in CORE_BATCH_METRICS
    }


def _summarize_per_class_f1(successes: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    labels: set[str] = set()
    for success in successes:
        per_class = success["metrics"].get("per_class_f1", {})
        if isinstance(per_class, dict):
            labels.update(str(label) for label in per_class)

    summary: dict[str, dict[str, float | None]] = {}
    for label in sorted(labels):
        values: list[float] = []
        for success in successes:
            per_class = success["metrics"].get("per_class_f1", {})
            if isinstance(per_class, dict) and label in per_class:
                values.append(float(per_class[label]))
        summary[label] = _summarize_values(values)
    return summary


def _summarize_values(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {
            "count": 0.0,
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
            "population_std": None,
        }
    return {
        "count": float(len(values)),
        "mean": mean(values),
        "median": median(values),
        "min": min(values),
        "max": max(values),
        "population_std": pstdev(values),
    }


def _metric_values(successes: list[dict[str, Any]], metric: str) -> list[float]:
    values: list[float] = []
    for success in successes:
        metrics = success["metrics"]
        if metric in metrics and metrics[metric] is not None:
            values.append(float(metrics[metric]))
    return values


def _lowest_records(successes: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    ranked = sorted(
        successes,
        key=lambda success: (
            float(success["metrics"].get("accuracy", 0.0)),
            float(success["metrics"].get("cohen_kappa", 0.0)),
            float(success["metrics"].get("macro_f1", 0.0)),
        ),
    )
    return [_record_brief(success) for success in ranked[: max(limit, 0)]]


def _records_below_thresholds(
    successes: list[dict[str, Any]],
    thresholds: dict[str, float],
) -> list[dict[str, Any]]:
    flagged: list[dict[str, Any]] = []
    for success in successes:
        metrics = success["metrics"]
        flags = {
            metric: {
                "value": float(metrics[metric]),
                "threshold": float(threshold),
            }
            for metric, threshold in thresholds.items()
            if metric in metrics and float(metrics[metric]) < threshold
        }
        if flags:
            record = _record_brief(success)
            record["flags"] = flags
            flagged.append(record)
    return sorted(
        flagged,
        key=lambda record: (
            float(record["metrics"].get("accuracy", 0.0)),
            float(record["metrics"].get("cohen_kappa", 0.0)),
        ),
    )


def _record_brief(success: dict[str, Any]) -> dict[str, Any]:
    metrics = success["metrics"]
    return {
        "record_id": success.get("record_id"),
        "compared_epoch_count": success.get("compared_epoch_count"),
        "evaluation_path": success.get("evaluation_path"),
        "metrics": {
            metric: metrics.get(metric)
            for metric in CORE_BATCH_METRICS
            if metric in metrics
        },
        "per_class_f1": metrics.get("per_class_f1", {}),
    }
