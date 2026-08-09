from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from sleepagent.radar_agent.product_agent import (
    AgentId,
    AuthenticatedBinding,
    FactSnapshot,
    InvocationOutcome,
    ProductToolError,
    ProductToolExecutionContext,
    ProductToolExecutor,
    SourceScope,
    SourceScopeKind,
    TrustLabel,
)
from sleepagent.radar_agent.product_agent.contracts import stable_hash
from sleepagent.radar_agent.schemas import RadarNightSummary


NOW = datetime(2026, 7, 26, 7, 0, tzinfo=timezone.utc)


def snapshot() -> FactSnapshot:
    return FactSnapshot.create(
        fact_snapshot_id="s1",
        binding=AuthenticatedBinding(
            actor_id="a1", subject_id="u1", role="elder"
        ),
        source_scope=SourceScope(
            kind=SourceScopeKind.SEVEN_DAY,
            as_of=NOW,
            timezone_name="Asia/Shanghai",
            date_start=date(2026, 7, 20),
            date_end=date(2026, 7, 26),
            valid_night_count=7,
        ),
        canonical_data_version="v1",
        source_refs=(
            "range:1",
            *tuple(item.source_report_ref for item in trend_summaries()),
        ),
        created_at=NOW,
    )


def trend_summaries() -> list[RadarNightSummary]:
    return [
        RadarNightSummary(
            radar_device_id="radar-1",
            subject_id="u1",
            night_of=date(2026, 7, 24 + offset),
            timezone_name="Asia/Shanghai",
            total_sleep_minutes=value,
            data_coverage_ratio=0.95,
            explainable_metrics={"calibration_state": "known_uncalibrated"},
            source_report_ref=(
                f"night-summary:radar-1:2026-07-{24 + offset:02d}"
            ),
        )
        for offset, value in enumerate((420.0, 390.0, 360.0))
    ]


def trend_arguments() -> dict[str, object]:
    return {
        "night_summaries": [
            item.model_dump(mode="json") for item in trend_summaries()
        ]
    }


def test_trend_is_deterministic_tool_not_agent() -> None:
    result = ProductToolExecutor().execute(
        "trend.calculate_metrics",
        trend_arguments(),
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=snapshot(),
            episode_id="episode-1",
        ),
    )
    assert result.receipt.output["tool_version"].startswith(
        "sleepagent-trend-analysis-tool"
    )
    assert result.context_item.trust_label == TrustLabel.TOOL_OUTPUT_UNTRUSTED


def test_agent_cannot_self_attest_scalar_trend_values() -> None:
    result = ProductToolExecutor().execute(
        "trend.calculate_metrics",
        {"values": [7.0, 6.5, 6.0], "source_refs": ["range:1"]},
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=snapshot(),
            episode_id="episode-1",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_agent_selector_reuses_only_runtime_bound_tool_output() -> None:
    executor = ProductToolExecutor()
    runtime_result = executor.execute(
        "trend.calculate_metrics",
        trend_arguments(),
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=snapshot(),
            episode_id="episode-1",
        ),
    )

    selected = executor.execute(
        "trend.calculate_metrics",
        {
            "bound_tool_invocation_id": (
                runtime_result.receipt.tool_invocation_id
            )
        },
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=snapshot(),
            episode_id="episode-1",
        ),
    )

    assert selected.receipt.outcome is InvocationOutcome.SUCCEEDED
    assert selected.receipt.output == runtime_result.receipt.output


def test_runtime_bound_tool_output_is_episode_scoped_and_releasable() -> None:
    executor = ProductToolExecutor()
    bound = executor.execute(
        "trend.calculate_metrics",
        trend_arguments(),
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=snapshot(),
            episode_id="episode-a",
        ),
    )
    selector = {
        "bound_tool_invocation_id": bound.receipt.tool_invocation_id
    }

    cross_episode = executor.execute(
        "trend.calculate_metrics",
        selector,
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=snapshot(),
            episode_id="episode-b",
        ),
    )
    assert cross_episode.receipt.outcome is InvocationOutcome.FAILED
    assert executor.runtime_binding_count("episode-a") == 1

    executor.release_episode("episode-a")
    released = executor.execute(
        "trend.calculate_metrics",
        selector,
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=snapshot(),
            episode_id="episode-a",
        ),
    )
    assert released.receipt.outcome is InvocationOutcome.FAILED
    assert executor.runtime_binding_count("episode-a") == 0


def test_agent_selector_fails_when_runtime_has_not_bound_tool_output() -> None:
    result = ProductToolExecutor().execute(
        "trend.calculate_metrics",
        {"bound_tool_invocation_id": "tool:trend.calculate_metrics:missing"},
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=snapshot(),
            episode_id="episode-1",
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED
    assert result.receipt.error_code == "ProductToolError"


def test_invalid_runtime_receipt_is_never_available_to_agent_selector() -> None:
    arguments = trend_arguments()

    def invalid_output(values, context):
        return {
            "source_refs": [f"source:{index}" for index in range(51)]
        }

    executor = ProductToolExecutor(
        {"trend.calculate_metrics": invalid_output}
    )
    with pytest.raises(ValueError, match="at most 50"):
        executor.execute(
            "trend.calculate_metrics",
            arguments,
            context=ProductToolExecutionContext(
                caller="runtime",
                fact_snapshot=snapshot(),
                episode_id="episode-1",
            ),
        )

    expected_ref = (
        "tool:trend.calculate_metrics:" + stable_hash(arguments)[:16]
    )
    selected = executor.execute(
        "trend.calculate_metrics",
        {"bound_tool_invocation_id": expected_ref},
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=snapshot(),
            episode_id="episode-1",
        ),
    )

    assert selected.receipt.outcome is InvocationOutcome.FAILED
    assert selected.receipt.error_code == "ProductToolError"


def test_reviewed_knowledge_fails_closed_for_unreviewed_content() -> None:
    result = ProductToolExecutor().execute(
        "knowledge.retrieve_reviewed",
        {"reviewed": False, "passages": ["unsafe"]},
        context=ProductToolExecutionContext(
            caller=AgentId.SLEEP_CARE,
            fact_snapshot=snapshot(),
        ),
    )
    assert result.receipt.outcome == InvocationOutcome.FAILED
    assert result.context_item is None


def test_artifact_render_rejects_untyped_content_compatibility_input() -> None:
    result = ProductToolExecutor().execute(
        "artifact.render",
        {"content": "draft", "source_refs": ["claim:1"]},
        context=ProductToolExecutionContext(
            caller="runtime",
            fact_snapshot=snapshot(),
            episode_id="episode-1",
        ),
    )
    assert result.receipt.outcome is InvocationOutcome.FAILED
    assert result.receipt.error_code == "ValueError"


def test_sleepcare_cannot_self_attest_unbound_artifact_content() -> None:
    result = ProductToolExecutor().execute(
        "artifact.render",
        {"content": "draft", "source_refs": ["claim:1"]},
        context=ProductToolExecutionContext(
            caller=AgentId.SLEEP_CARE,
            fact_snapshot=snapshot(),
        ),
    )

    assert result.receipt.outcome is InvocationOutcome.FAILED


def test_model_agent_cannot_execute_state_change() -> None:
    with pytest.raises(Exception, match="state.commit_care"):
        ProductToolExecutor().execute(
            "state.commit_care",
            {},
            context=ProductToolExecutionContext(
                caller=AgentId.CARE_STRATEGY,
                fact_snapshot=snapshot(),
            ),
        )


def test_missing_handler_fails_explicitly() -> None:
    executor = ProductToolExecutor()
    executor.handlers.pop("policy.read")
    with pytest.raises(ProductToolError, match="no handler"):
        executor.execute(
            "policy.read",
            {},
            context=ProductToolExecutionContext(
                caller=AgentId.SLEEP_CARE,
                fact_snapshot=snapshot(),
            ),
        )
