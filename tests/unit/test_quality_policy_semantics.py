from __future__ import annotations

from types import SimpleNamespace

import pytest

from sleepagent.domain.contracts import (
    DataSufficiency,
    DeterministicQualityPolicy,
    QualityState,
)
from sleepagent.domain.fast_path import _quality_outcome
from sleepagent.infrastructure.postgres_sleep_slice import decide_fast_path_followup


pytestmark = pytest.mark.unit


POLICY = DeterministicQualityPolicy(
    policy_version="quality-v2-semantic-missingness",
    minimum_coverage_ratio=0.75,
    partial_coverage_ratio=0.5,
)


def _outcome(**overrides: object) -> tuple[QualityState, DataSufficiency]:
    values: dict[str, object] = {
        "observations_present": True,
        "coverage_ratio": 1.0,
        "explicit_missing_count": 0,
        "invalid_count": 0,
        "stale": False,
        "offline": False,
        "clock_invalid": False,
        "policy": POLICY,
    }
    values.update(overrides)
    return _quality_outcome(**values)  # type: ignore[arg-type]


def test_isolated_invalid_observation_degrades_night_to_partial() -> None:
    assert _outcome(explicit_missing_count=1, invalid_count=1) == (
        QualityState.PARTIAL,
        DataSufficiency.PARTIAL,
    )


def test_partial_floor_is_distinct_from_sufficient_coverage_threshold() -> None:
    assert _outcome(coverage_ratio=0.6) == (
        QualityState.PARTIAL,
        DataSufficiency.PARTIAL,
    )
    assert _outcome(coverage_ratio=0.49) == (
        QualityState.DATA_INSUFFICIENT,
        DataSufficiency.DATA_INSUFFICIENT,
    )
    assert _outcome(coverage_ratio=0.8) == (
        QualityState.SUFFICIENT,
        DataSufficiency.SUFFICIENT,
    )


@pytest.mark.parametrize(
    "condition",
    ("stale", "offline", "clock_invalid"),
)
def test_stream_integrity_failures_remain_unusable(condition: str) -> None:
    assert _outcome(**{condition: True}) == (
        QualityState.DATA_INSUFFICIENT,
        DataSufficiency.DATA_INSUFFICIENT,
    )


def test_partial_quality_routes_to_quality_aware_product_runtime() -> None:
    decision = decide_fast_path_followup(
        SimpleNamespace(health_escalation_allowed=False),
        quality=SimpleNamespace(data_sufficiency=DataSufficiency.PARTIAL),
    )

    assert decision.urgent is False
    assert decision.enqueue_product_agent is True


def test_unusable_quality_remains_withheld_from_product_runtime() -> None:
    decision = decide_fast_path_followup(
        SimpleNamespace(health_escalation_allowed=False),
        quality=SimpleNamespace(
            data_sufficiency=DataSufficiency.DATA_INSUFFICIENT
        ),
    )

    assert decision.urgent is False
    assert decision.enqueue_product_agent is False
