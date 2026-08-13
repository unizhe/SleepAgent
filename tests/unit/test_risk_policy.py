from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from sleepagent.runtime.contracts import (
    CurrentContextRisk,
    LongitudinalTrend,
    MultifactorSafetyInput,
    OnlineDataQuality,
)
import sleepagent.runtime.policies as risk_policy_module
from sleepagent.runtime.policies import URGENT_TERMS
import sleepagent.runtime.tools as risk_tool_module
from sleepagent.runtime.tools import (
    DeterministicRiskSnapshot,
    RiskClassificationInput,
    RiskClassificationLevel,
    RiskClassificationTool,
    RiskObservation,
    TrendRiskSignal,
)
from tests.support.golden_fixtures import load_product_capability_goldens


def test_urgent_policy_preserves_legacy_terms_priority_and_evidence_ref() -> None:
    expected = load_product_capability_goldens()["risk_classification"]
    tool = RiskClassificationTool()
    for term in URGENT_TERMS:
        text = f"context before {term} context after"
        migrated = tool.classify(
            RiskClassificationInput(
                text_inputs=(text,),
                observation=_observation(
                    quality_status="unusable",
                    confidence_label="not_interpretable",
                    health_conclusion_allowed=False,
                ),
                trend_signals=(
                    TrendRiskSignal(
                        risk_level="escalate",
                        confidence=0.8,
                        source_refs=("trend:legacy",),
                    ),
                ),
            )
        )

        assert migrated.risk_level == RiskClassificationLevel.URGENT_BOUNDARY
        assert migrated.reason_codes == ("urgent_text_boundary",)
        assert migrated.source_refs == (
            expected["urgent_evidence_refs"][term],
        )
        assert migrated.should_stop_sleep_trend_explanation is True
        assert migrated.urgent_required is True


def test_quality_block_precedes_legacy_trend_escalation() -> None:
    expected = load_product_capability_goldens()["risk_classification"][
        "quality_block"
    ]
    trend_signals = (
        _trend_signal("sleep", "watch"),
        _trend_signal("movement", "watch"),
        _trend_signal("vital", "watch"),
    )

    migrated = RiskClassificationTool().classify(
        RiskClassificationInput(
            observation=_observation(
                quality_status="unusable",
                confidence_label="not_interpretable",
                health_conclusion_allowed=False,
            ),
            trend_signals=trend_signals,
        )
    )

    assert migrated.risk_level.value == expected["risk_level"]
    assert migrated.risk_level is RiskClassificationLevel.UNCERTAIN
    assert list(migrated.source_refs) == expected["evidence_refs"]
    assert migrated.reason_codes == ("data_not_interpretable",)
    assert migrated.quality_blocks_escalation is True
    assert migrated.should_stop_sleep_trend_explanation is True


@pytest.mark.parametrize(
    ("watch_count", "out_of_bed", "movement", "expected"),
    (
        (3, 1, 8, RiskClassificationLevel.ESCALATE),
        (2, 6, 8, RiskClassificationLevel.ESCALATE),
        (2, 1, 25, RiskClassificationLevel.ESCALATE),
        (2, 5, 24, RiskClassificationLevel.WATCH),
        (1, 12, 40, RiskClassificationLevel.WATCH),
        (0, 1, 8, RiskClassificationLevel.NORMAL),
    ),
)
def test_structured_risk_thresholds_match_legacy(
    watch_count: int,
    out_of_bed: int,
    movement: int,
    expected: RiskClassificationLevel,
) -> None:
    trend_signals = tuple(
        _trend_signal(str(index), "watch")
        for index in range(watch_count)
    )

    migrated = RiskClassificationTool().classify(
        RiskClassificationInput(
            observation=_observation(
                out_of_bed_count=out_of_bed,
                movement_count=movement,
            ),
            trend_signals=trend_signals,
        )
    )

    frozen_level = load_product_capability_goldens()["risk_classification"][
        "thresholds"
    ][f"{watch_count}:{out_of_bed}:{movement}"]
    assert expected is {
        "info": RiskClassificationLevel.NORMAL,
        "watch": RiskClassificationLevel.WATCH,
        "escalate": RiskClassificationLevel.ESCALATE,
    }[frozen_level]
    assert migrated.risk_level == expected


def test_explicit_escalate_signal_matches_legacy() -> None:
    expected = load_product_capability_goldens()["risk_classification"][
        "explicit_escalate"
    ]

    migrated = RiskClassificationTool().classify(
        RiskClassificationInput(
            observation=_observation(),
            trend_signals=(_trend_signal("escalate", "escalate"),),
        )
    )

    assert migrated.risk_level.value == expected["risk_level"]
    assert migrated.risk_level is RiskClassificationLevel.ESCALATE
    assert list(migrated.source_refs) == expected["evidence_refs"]
    assert migrated.reason_codes == ("multi_signal_escalation",)
    assert migrated.safety_required is True


@pytest.mark.parametrize(
    ("observation_kwargs", "expected_reason"),
    (
        (
            {
                "quality_status": "partial",
                "confidence_label": "low_confidence",
                "abnormal_reading_count": 3,
            },
            "low_quality_vital_signal",
        ),
        (
            {"vital_fluctuation_count": 1},
            "vital_fluctuation_signal",
        ),
    ),
)
def test_watch_quality_and_vital_signals_match_legacy(
    observation_kwargs: dict[str, object],
    expected_reason: str,
) -> None:
    migrated = RiskClassificationTool().classify(
        RiskClassificationInput(observation=_observation(**observation_kwargs))
    )

    expected = load_product_capability_goldens()["risk_classification"][
        "watch_signals"
    ][expected_reason]
    assert migrated.risk_level.value == expected
    assert migrated.risk_level == RiskClassificationLevel.WATCH
    assert migrated.reason_codes == (expected_reason,)


def test_multifactor_input_preserves_deterministic_escalation_and_urgent_flags() -> None:
    result = RiskClassificationTool().classify(
        RiskClassificationInput(
            safety_factors=MultifactorSafetyInput(
                absolute_red_flag=True,
                absolute_red_flag_codes=("fall_with_injury",),
                absolute_red_flag_requires_urgent=True,
                data_quality=OnlineDataQuality.UNUSABLE,
                current_context=CurrentContextRisk.CONCERNING,
                longitudinal_trend=LongitudinalTrend.WORSENING,
                source_refs=("event:1", "quality:1"),
            )
        )
    )

    assert result.risk_level == RiskClassificationLevel.ESCALATE
    assert result.reason_codes == ("absolute_red_flag:fall_with_injury",)
    assert result.source_refs == ("event:1", "quality:1")
    assert result.quality_status == "unusable"
    assert result.safety_required is True
    assert result.urgent_required is True
    assert result.should_stop_sleep_trend_explanation is True


@pytest.mark.parametrize(
    ("snapshot", "expected_level", "expected_quality", "safety_required"),
    (
        (
            DeterministicRiskSnapshot(
                risk_state="reviewed_signal",
                data_sufficiency="sufficient",
                health_escalation_allowed=True,
                reason_codes=("approved_vendor_alert",),
                source_refs=("current_risk:risk-1",),
            ),
            RiskClassificationLevel.ESCALATE,
            "good",
            True,
        ),
        (
            DeterministicRiskSnapshot(
                risk_state="operational_review",
                data_sufficiency="sufficient",
                reason_codes=("device_offline_review",),
                source_refs=("current_risk:risk-2",),
            ),
            RiskClassificationLevel.WATCH,
            "good",
            False,
        ),
        (
            DeterministicRiskSnapshot(
                risk_state="unknown",
                data_sufficiency="data_insufficient",
                reason_codes=("no_exact_revision_risk_summary",),
                source_refs=("revision:3",),
            ),
            RiskClassificationLevel.UNCERTAIN,
            "unusable",
            False,
        ),
        (
            DeterministicRiskSnapshot(
                risk_state="no_reviewed_signal",
                data_sufficiency="sufficient",
                reason_codes=("no_reviewed_signal_in_source_scope",),
                source_refs=("current_risk:risk-4",),
            ),
            RiskClassificationLevel.NORMAL,
            "good",
            False,
        ),
    ),
)
def test_exact_revision_current_risk_is_not_reduced_to_a_scalar_score(
    snapshot: DeterministicRiskSnapshot,
    expected_level: RiskClassificationLevel,
    expected_quality: str,
    safety_required: bool,
) -> None:
    result = RiskClassificationTool().classify(
        RiskClassificationInput(deterministic_snapshot=snapshot)
    )

    assert result.risk_level is expected_level
    assert result.reason_codes == snapshot.reason_codes
    assert result.source_refs == snapshot.source_refs
    assert result.quality_status == expected_quality
    assert result.safety_required is safety_required


def test_risk_input_rejects_competing_fact_modes() -> None:
    with pytest.raises(ValidationError, match="one risk fact mode"):
        RiskClassificationInput(
            deterministic_snapshot=DeterministicRiskSnapshot(
                risk_state="reviewed_signal",
                data_sufficiency="sufficient",
                health_escalation_allowed=True,
            ),
            observation=_observation(),
        )


def test_risk_tool_output_has_no_agent_claim_action_or_confirmation_surface() -> None:
    result = RiskClassificationTool().classify(
        RiskClassificationInput(
            observation=_observation(),
            trend_signals=(
                TrendRiskSignal(
                    risk_level="watch",
                    confidence=0.7,
                    source_refs=("trend:1",),
                ),
            ),
        )
    )
    payload = result.model_dump(mode="json")

    assert set(payload) == {
        "tool_version",
        "policy_version",
        "risk_level",
        "reason_codes",
        "source_refs",
        "quality_status",
        "quality_blocks_escalation",
        "should_stop_sleep_trend_explanation",
        "safety_required",
        "urgent_required",
    }
    assert not (
        {"claims", "candidate_actions", "confirmation_requests"}
        & payload.keys()
    )
    assert not hasattr(result, "agent_name")


def test_canonical_risk_modules_have_no_legacy_agent_or_runtime_dependency() -> None:
    source = "\n".join(
        (
            inspect.getsource(risk_policy_module),
            inspect.getsource(risk_tool_module),
        )
    )
    forbidden = (
        "radar_agent.agents",
        "radar_agent.orchestrator",
        "radar_agent.dynamic",
        "A2AMessage",
        "RadarAgentName",
        "AgentResult",
    )

    assert all(item not in source for item in forbidden)


def _observation(
    *,
    quality_status: str = "good",
    confidence_label: str = "normal",
    health_conclusion_allowed: bool = True,
    abnormal_reading_count: int = 0,
    vital_fluctuation_count: int = 0,
    out_of_bed_count: int = 1,
    movement_count: int = 8,
) -> RiskObservation:
    return RiskObservation(
        quality_status=quality_status,
        confidence_label=confidence_label,
        health_conclusion_allowed=health_conclusion_allowed,
        abnormal_reading_count=abnormal_reading_count,
        vital_fluctuation_count=vital_fluctuation_count,
        out_of_bed_count=out_of_bed_count,
        movement_count=movement_count,
        source_refs=("night-summary:radar-001:2026-07-10",),
    )


def _trend_signal(suffix: str, risk_level: str) -> TrendRiskSignal:
    return TrendRiskSignal(
        risk_level=risk_level,
        confidence=0.72,
        source_refs=(f"trend:{suffix}",),
    )
