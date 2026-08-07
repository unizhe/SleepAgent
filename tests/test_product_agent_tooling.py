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
        source_refs=("range:1",),
        created_at=NOW,
    )


def test_trend_is_deterministic_tool_not_agent() -> None:
    result = ProductToolExecutor().execute(
        "trend.calculate_metrics",
        {"values": [7.0, 6.5, 6.0], "source_refs": ["range:1"]},
        context=ProductToolExecutionContext(
            caller=AgentId.EVIDENCE_REASONING,
            fact_snapshot=snapshot(),
        ),
    )
    assert result.receipt.output["change"] == -1.0
    assert result.context_item.trust_label == TrustLabel.TOOL_OUTPUT_UNTRUSTED


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


def test_artifact_render_does_not_claim_commit_or_export() -> None:
    result = ProductToolExecutor().execute(
        "artifact.render",
        {"content": "draft", "source_refs": ["claim:1"]},
        context=ProductToolExecutionContext(
            caller=AgentId.SLEEP_CARE,
            fact_snapshot=snapshot(),
        ),
    )
    assert result.receipt.output["rendered"]
    assert not result.receipt.output["committed"]


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
