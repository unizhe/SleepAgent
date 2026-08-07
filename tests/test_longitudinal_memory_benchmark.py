from __future__ import annotations

from benchmarks.healthclaw_memory_governance.harness import (
    EpisodeObservation,
    evaluate_gate,
)


def test_frozen_paired_longitudinal_engineering_gate() -> None:
    observations = []
    for index in range(40):
        episode_id = f"synthetic:{index:03d}"
        observations.extend(
            (
                EpisodeObservation(
                    episode_id=episode_id,
                    condition="current_only",
                    provider_inputs=({"messages": ["current"]},),
                    quality_score=0.70,
                    safety_passed=True,
                ),
                EpisodeObservation(
                    episode_id=episode_id,
                    condition="full_history",
                    provider_inputs=(
                        {
                            "messages": [
                                "authorized structured history item"
                                for _ in range(30)
                            ]
                        },
                    ),
                    quality_score=0.82,
                    safety_passed=True,
                ),
                EpisodeObservation(
                    episode_id=episode_id,
                    condition="governed",
                    provider_inputs=(
                        {
                            "messages": [
                                "exact relevant governed item",
                                "typed episodic hint",
                            ]
                        },
                    ),
                    quality_score=0.80,
                    safety_passed=True,
                ),
            )
        )

    report = evaluate_gate(observations)

    assert report["passed"] is True
    assert report["governed_exposure_reduction"] >= 0.50
    assert report["full_minus_governed_upper_95"] <= 0.05
    assert report["governed_minus_current_lower_95"] > 0
    assert report["forbidden_event_count"] == 0
    assert report["episode_exposure_tail"]["governed"]["p95"] > 0
    assert report["claim_scope"] == (
        "engineering_release_only_not_clinical_effectiveness"
    )


def test_longitudinal_gate_fails_closed_on_insufficient_evidence() -> None:
    rows = [
        EpisodeObservation(
            episode_id="synthetic:one",
            condition=condition,
            provider_inputs=({"messages": []},),
            quality_score=0.5,
            safety_passed=True,
        )
        for condition in ("current_only", "full_history", "governed")
    ]
    try:
        evaluate_gate(rows)
    except ValueError as exc:
        assert "insufficient" in str(exc)
    else:
        raise AssertionError("small benchmark must not qualify release")
