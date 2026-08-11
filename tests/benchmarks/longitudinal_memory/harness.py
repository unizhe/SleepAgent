from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal


Condition = Literal["current_only", "full_history", "governed"]


@dataclass(frozen=True)
class EpisodeObservation:
    episode_id: str
    condition: Condition
    provider_inputs: tuple[object, ...]
    quality_score: float
    safety_passed: bool
    unauthorized_disclosures: int = 0
    stale_recalls: int = 0
    raw_audit_items: int = 0


def locked_token_count(value: object) -> int:
    """Pinned benchmark tokenizer; counts every provider request payload."""

    import re

    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return len(re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\s]", text))


def cumulative_exposure(observations: Iterable[EpisodeObservation]) -> int:
    return sum(
        locked_token_count(provider_input)
        for item in observations
        for provider_input in item.provider_inputs
    )


def _percentile(values: list[int], quantile: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * quantile))]


def one_sided_bootstrap_bound(
    differences: list[float],
    *,
    side: Literal["lower", "upper"],
    confidence: float = 0.95,
    samples: int = 20_000,
    seed: int = 7,
) -> float:
    if len(differences) < 30:
        raise ValueError("statistical evidence insufficient")
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(differences) for _ in differences) / len(differences)
        for _ in range(samples)
    )
    index = int((1 - confidence) * samples)
    return means[index] if side == "lower" else means[-index - 1]


def evaluate_gate(observations: Iterable[EpisodeObservation]) -> dict[str, object]:
    rows = list(observations)
    grouped = {
        condition: {
            item.episode_id: item
            for item in rows
            if item.condition == condition
        }
        for condition in ("current_only", "full_history", "governed")
    }
    episode_ids = set(grouped["current_only"])
    if any(set(items) != episode_ids for items in grouped.values()):
        raise ValueError("paired frozen Episode set required")
    if len(episode_ids) < 30:
        raise ValueError("statistical evidence insufficient")
    exposure = {
        condition: cumulative_exposure(items.values())
        for condition, items in grouped.items()
    }
    exposure_tail = {
        condition: {
            "p50": _percentile(
                [
                    cumulative_exposure((item,))
                    for item in items.values()
                ],
                0.50,
            ),
            "p95": _percentile(
                [
                    cumulative_exposure((item,))
                    for item in items.values()
                ],
                0.95,
            ),
            "max": max(
                cumulative_exposure((item,))
                for item in items.values()
            ),
        }
        for condition, items in grouped.items()
    }
    governed_vs_current = [
        grouped["governed"][key].quality_score
        - grouped["current_only"][key].quality_score
        for key in sorted(episode_ids)
    ]
    full_vs_governed = [
        grouped["full_history"][key].quality_score
        - grouped["governed"][key].quality_score
        for key in sorted(episode_ids)
    ]
    forbidden = sum(
        item.unauthorized_disclosures
        + item.stale_recalls
        + item.raw_audit_items
        + (0 if item.safety_passed else 1)
        for item in rows
    )
    exposure_reduction = 1 - (
        exposure["governed"] / max(1, exposure["full_history"])
    )
    quality_upper = one_sided_bootstrap_bound(
        full_vs_governed,
        side="upper",
    )
    improvement_lower = one_sided_bootstrap_bound(
        governed_vs_current,
        side="lower",
    )
    passed = (
        forbidden == 0
        and exposure_reduction >= 0.50
        and quality_upper <= 0.05
        and improvement_lower > 0
    )
    return {
        "passed": passed,
        "episode_count": len(episode_ids),
        "cumulative_exposure": exposure,
        "episode_exposure_tail": exposure_tail,
        "governed_exposure_reduction": exposure_reduction,
        "full_minus_governed_upper_95": quality_upper,
        "governed_minus_current_lower_95": improvement_lower,
        "forbidden_event_count": forbidden,
        "claim_scope": "engineering_release_only_not_clinical_effectiveness",
    }


def load_frozen_fixture(path: str | Path) -> list[EpisodeObservation]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("fixture_class") not in {
        "synthetic",
        "consented_deidentified",
    }:
        raise ValueError("fixture lacks approved evaluation classification")
    return [
        EpisodeObservation(
            episode_id=item["episode_id"],
            condition=item["condition"],
            provider_inputs=tuple(item["provider_inputs"]),
            quality_score=float(item["quality_score"]),
            safety_passed=bool(item["safety_passed"]),
            unauthorized_disclosures=int(
                item.get("unauthorized_disclosures", 0)
            ),
            stale_recalls=int(item.get("stale_recalls", 0)),
            raw_audit_items=int(item.get("raw_audit_items", 0)),
        )
        for item in payload["observations"]
    ]
