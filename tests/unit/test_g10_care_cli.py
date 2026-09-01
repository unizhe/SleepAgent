from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sleepagent.application.care_outcomes import CareOutcomeView
from sleepagent.care_cli import (
    _outcome_payload,
    _render_outcome,
    build_parser,
)
from sleepagent.domain.care_actions import CareActionType
from sleepagent.domain.care_execution import CarePlanEntry
from sleepagent.domain.care_outcomes import (
    CareExecutionEvidence,
    OutcomeLifecycleState,
    evaluate_care_outcome,
)
from tests.unit.test_g10_care_outcomes import _wake_evidence


pytestmark = pytest.mark.unit
UTC = timezone.utc
COMPLETED = datetime(2026, 8, 10, 8, tzinfo=UTC)


def _plan() -> CarePlanEntry:
    return CarePlanEntry.model_construct(
        care_plan_id="care-plan:1",
        action_type=CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME,
        structured_action_parameters={"tolerance_minutes": 30},
    )


def _waiting_view() -> CareOutcomeView:
    return CareOutcomeView(
        plan=_plan(),
        execution_state="completed",
        completed_at=COMPLETED,
        lifecycle_state=OutcomeLifecycleState.WAITING_FOR_FOLLOWUP,
        observation_window_start=COMPLETED,
        observation_window_end=COMPLETED + timedelta(days=14),
        reason_code="eligible_hard_finalized_followup_not_available",
        outcome=None,
        personalization_receipt=None,
    )


def _evaluated_view() -> CareOutcomeView:
    execution = CareExecutionEvidence(
        care_plan_id="care-plan:1",
        care_execution_event_id="event-1",
        subject_id="subject-a",
        action_type=CareActionType.RECOMMEND_CONSISTENT_WAKE_TIME,
        state="completed",
        completed_at=COMPLETED,
        execution_authority="human_attested",
        source_analysis_revision_id="analysis-1",
        authority_valid_at_completion=True,
    )
    decision = evaluate_care_outcome(
        execution,
        _wake_evidence((360, 420), (390, 400)),
        evaluated_at=COMPLETED + timedelta(days=3),
    )
    assert decision.outcome is not None
    return CareOutcomeView(
        plan=_plan(),
        execution_state="completed",
        completed_at=COMPLETED,
        lifecycle_state=decision.lifecycle_state,
        observation_window_start=COMPLETED,
        observation_window_end=COMPLETED + timedelta(days=14),
        reason_code=decision.reason_code,
        outcome=decision.outcome,
        personalization_receipt=decision.personalization_receipt,
    )


def test_cli_exposes_outcome_commands() -> None:
    help_text = build_parser().format_help()
    assert "outcome" in help_text
    assert "outcomes" in help_text


def test_waiting_view_says_waiting_not_no_improvement() -> None:
    rendered = _render_outcome(_waiting_view())
    assert "等待后续睡眠数据" in rendered
    assert "后续夜晚尚未 HARD_FINALIZED" in rendered
    assert "无改善" not in rendered


def test_evaluated_view_is_zh_cn_noncausal_and_trace_is_bounded() -> None:
    view = _evaluated_view()
    rendered = _render_outcome(view)
    assert "照护计划" in rendered
    assert "执行状态" in rendered
    assert "执行时间" in rendered
    assert "观察窗口" in rendered
    assert "执行前" in rendered
    assert "执行后" in rendered
    assert "观察结果" in rendered
    assert "证据质量" in rendered
    assert "不代表已经证明" in rendered
    payload = _outcome_payload(view, trace=True)
    assert payload["causal_claim"] is False
    assert payload["outcome_category"] == "improved"
    assert set(payload["trace"]) == {
        "baseline_revision_ids",
        "baseline_episode_revision_ids",
        "followup_revision_ids",
        "followup_episode_revision_ids",
        "evaluation_policy_version",
        "evaluation_policy_hash",
        "outcome_hash",
        "personalization_receipt_id",
    }
    encoded = str(payload)
    assert "subject-a" not in encoded
    assert "raw_payload" not in encoded
