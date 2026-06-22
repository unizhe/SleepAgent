"""Compare Stage 3 YASA channel evaluation payloads."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


STAGE3_YASA_CHANNEL_COMPARISON_SCHEMA_VERSION = "stage3.yasa_channel_comparison.v1"
CHANNEL_COMPARISON_SCORE_KEYS = (
    "accuracy",
    "cohen_kappa",
    "macro_f1",
    "weighted_f1",
)


@dataclass(frozen=True)
class YASAChannelCandidate:
    """One evaluated EEG channel candidate."""

    name: str
    evaluation_path: Path
    metrics: dict[str, float]
    compared_epoch_count: int


@dataclass(frozen=True)
class YASAChannelComparisonResult:
    """Ranked channel comparison result."""

    candidates: list[YASAChannelCandidate]
    recommended_channel: str
    score_keys: tuple[str, ...] = CHANNEL_COMPARISON_SCORE_KEYS


def compare_yasa_channel_evaluations(
    candidates: dict[str, str | Path],
) -> YASAChannelComparisonResult:
    """Rank channel candidates by accuracy, then kappa, macro F1, and weighted F1."""
    if len(candidates) < 2:
        raise ValueError("At least two channel candidates are required for comparison.")

    parsed_candidates = [
        _load_channel_candidate(name=name, evaluation_path=evaluation_path)
        for name, evaluation_path in candidates.items()
    ]
    ranked = sorted(
        parsed_candidates,
        key=lambda candidate: tuple(
            candidate.metrics[key] for key in CHANNEL_COMPARISON_SCORE_KEYS
        ),
        reverse=True,
    )
    return YASAChannelComparisonResult(
        candidates=ranked,
        recommended_channel=ranked[0].name,
    )


def build_yasa_channel_comparison_payload(
    result: YASAChannelComparisonResult,
) -> dict[str, Any]:
    """Build a JSON-safe channel comparison payload."""
    return {
        "schema_version": STAGE3_YASA_CHANNEL_COMPARISON_SCHEMA_VERSION,
        "recommended_channel": result.recommended_channel,
        "score_keys": list(result.score_keys),
        "candidates": [
            {
                "name": candidate.name,
                "evaluation_path": str(candidate.evaluation_path),
                "compared_epoch_count": candidate.compared_epoch_count,
                "metrics": candidate.metrics,
            }
            for candidate in result.candidates
        ],
        "notes": [
            "Stage 3 local YASA EEG-channel comparison.",
            "Candidates are ranked by accuracy, then Cohen's Kappa, macro F1, and weighted F1.",
            "This payload stores only metadata and metrics, not raw EDF/XML content.",
        ],
    }


def write_yasa_channel_comparison_payload(
    payload: dict[str, Any],
    output_path: str | Path,
) -> Path:
    """Write a channel comparison payload as JSON."""
    path = Path(output_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_channel_candidate(
    *,
    name: str,
    evaluation_path: str | Path,
) -> YASAChannelCandidate:
    path = Path(evaluation_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"YASA channel evaluation JSON not found: {path}")
    if not path.is_file():
        raise ValueError(f"YASA channel evaluation path is not a file: {path}")

    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError(f"Evaluation payload missing metrics object: {path}")

    numeric_metrics: dict[str, float] = {}
    for key in CHANNEL_COMPARISON_SCORE_KEYS:
        if key not in metrics or metrics[key] is None:
            raise ValueError(f"Evaluation payload missing metric {key!r}: {path}")
        numeric_metrics[key] = float(metrics[key])

    compared_epoch_count = int(payload.get("compared_epoch_count", 0))
    if compared_epoch_count <= 0:
        raise ValueError(f"Evaluation payload has no compared epochs: {path}")

    return YASAChannelCandidate(
        name=name,
        evaluation_path=path,
        metrics=numeric_metrics,
        compared_epoch_count=compared_epoch_count,
    )
