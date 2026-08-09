from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from sleepagent.radar_agent.product_agent.agents.sleepcare import (
    EpisodePlanProposal,
    EvaluationDecision,
    SleepCareEvaluation,
)
from sleepagent.radar_agent.product_agent.contracts import (
    AgentId,
    AuthenticatedBinding,
    EpisodeStatus,
    EpisodeType,
    FactSnapshot,
    SourceScope,
    SourceScopeKind,
    WorkProductKind,
)
from sleepagent.radar_agent.product_agent.episode import (
    EpisodeStateConflict,
    ProductEpisodeRuntime,
    ResumeRequiresReplan,
)
from sleepagent.radar_agent.product_agent.governance import AcceptedWorkProduct


NOW = datetime(2026, 7, 26, 7, 0, tzinfo=timezone.utc)


def fact_snapshot(*, actor_id="actor-1", version=0) -> FactSnapshot:
    scope = SourceScope(
        kind=SourceScopeKind.CURRENT_NIGHT,
        as_of=NOW,
        timezone_name="Asia/Shanghai",
        date_start=date(2026, 7, 26),
        date_end=date(2026, 7, 26),
        valid_night_count=1,
    )
    return FactSnapshot.create(
        fact_snapshot_id="snapshot-1",
        binding=AuthenticatedBinding(
            actor_id=actor_id,
            subject_id="subject-1",
            role="elder",
        ),
        source_scope=scope,
        canonical_data_version=f"v{version}",
        created_at=NOW,
    )


class SleepCareModel:
    provider = "test"
    model_id = "sleepcare"

    def __init__(self, *, evaluation=EvaluationDecision.CONTINUE):
        self.evaluation = evaluation
        self.calls = []

    def generate(self, *, messages, schema, prompt_version, context_packet_id):
        self.calls.append(schema)
        if schema is EpisodePlanProposal:
            return schema(
                objective="解释昨夜",
                required_work_products=[
                    WorkProductKind.SLEEPCARE_PLAN,
                    WorkProductKind.EVIDENCE_PACKET,
                    WorkProductKind.COMMUNICATION,
                ],
                exit_conditions=["communication_published"],
                expected_agent_calls=5,
                expected_tool_calls=5,
            )
        if schema is SleepCareEvaluation:
            return schema(
                decision=self.evaluation,
                summary="继续受控路径",
                replan_reason=(
                    "new_evidence"
                    if self.evaluation == EvaluationDecision.REPLAN
                    else None
                ),
            )
        raise AssertionError(schema)


def accepted(kind: WorkProductKind, snapshot: FactSnapshot) -> AcceptedWorkProduct:
    agent = {
        WorkProductKind.EVIDENCE_PACKET: AgentId.EVIDENCE_REASONING,
        WorkProductKind.CARE_STRATEGY: AgentId.CARE_STRATEGY,
        WorkProductKind.SAFETY_DECISION: AgentId.SAFETY_REVIEW,
        WorkProductKind.COMMUNICATION: AgentId.SLEEP_CARE,
    }[kind]
    return AcceptedWorkProduct(
        work_product_ref=f"{kind.value}:1",
        agent_id=agent,
        target_id=f"{kind.value}:1",
        target_hash="a" * 64,
        fact_snapshot_hash=snapshot.fact_snapshot_hash,
        episode_state_revision=1,
        payload={},
        accepted_at=NOW,
    )


def test_runtime_sleepcare_plans_but_registry_owns_minimum_path() -> None:
    model = SleepCareModel()
    runtime = ProductEpisodeRuntime(
        episode_id="episode-1",
        fact_snapshot=fact_snapshot(),
        sleepcare_model=model,
    )
    plan = runtime.create_plan(
        episode_type=EpisodeType.MORNING_REVIEW,
        objective="解释昨夜",
    )
    assert WorkProductKind.EVIDENCE_PACKET in plan.required_work_products
    assert runtime.invocation_records[0].agent_id == AgentId.SLEEP_CARE


def test_runtime_accepts_revisioned_work_and_evaluates_afterward() -> None:
    model = SleepCareModel()
    snap = fact_snapshot()
    runtime = ProductEpisodeRuntime(
        episode_id="episode-1",
        fact_snapshot=snap,
        sleepcare_model=model,
    )
    runtime.create_plan(
        episode_type=EpisodeType.MORNING_REVIEW,
        objective="解释昨夜",
    )
    product = accepted(WorkProductKind.EVIDENCE_PACKET, snap)
    runtime.accept(WorkProductKind.EVIDENCE_PACKET, product)
    decision = runtime.evaluate(
        latest_kind=WorkProductKind.EVIDENCE_PACKET,
        latest=product,
    )
    assert decision.decision == EvaluationDecision.CONTINUE
    assert runtime.episode_state_revision == 2


def test_runtime_does_not_allow_sleepcare_to_finish_missing_work() -> None:
    model = SleepCareModel(evaluation=EvaluationDecision.FINISH)
    snap = fact_snapshot()
    runtime = ProductEpisodeRuntime(
        episode_id="episode-1",
        fact_snapshot=snap,
        sleepcare_model=model,
    )
    runtime.create_plan(
        episode_type=EpisodeType.MORNING_REVIEW,
        objective="解释昨夜",
    )
    product = accepted(WorkProductKind.EVIDENCE_PACKET, snap)
    runtime.accept(WorkProductKind.EVIDENCE_PACKET, product)
    with pytest.raises(EpisodeStateConflict, match="required work"):
        runtime.evaluate(
            latest_kind=WorkProductKind.EVIDENCE_PACKET,
            latest=product,
        )


def test_waiting_checkpoint_resume_revalidates_identity_and_snapshot() -> None:
    model = SleepCareModel()
    snap = fact_snapshot()
    runtime = ProductEpisodeRuntime(
        episode_id="episode-1",
        fact_snapshot=snap,
        sleepcare_model=model,
    )
    runtime.create_plan(
        episode_type=EpisodeType.MORNING_REVIEW,
        objective="解释昨夜",
    )
    runtime.checkpoint(EpisodeStatus.WAITING_USER)
    with pytest.raises(ResumeRequiresReplan, match="binding"):
        runtime.resume(snapshot=fact_snapshot(actor_id="other"))
    with pytest.raises(ResumeRequiresReplan, match="FactSnapshot"):
        runtime.resume(snapshot=fact_snapshot(version=2))
    runtime.resume(snapshot=snap)
    assert runtime.status is None


def test_snapshot_restore_is_strict_and_preserves_counters() -> None:
    model = SleepCareModel()
    runtime = ProductEpisodeRuntime(
        episode_id="episode-1",
        fact_snapshot=fact_snapshot(),
        sleepcare_model=model,
    )
    runtime.create_plan(
        episode_type=EpisodeType.MORNING_REVIEW,
        objective="解释昨夜",
    )
    restored = ProductEpisodeRuntime.restore(
        runtime.snapshot(),
        sleepcare_model=model,
    )
    assert restored.plan == runtime.plan
    assert restored.counters == runtime.counters
    tampered = runtime.snapshot().model_copy(
        update={
            "counters": runtime.counters.model_copy(
                update={"agent_calls": 99}
            )
        }
    )
    with pytest.raises(EpisodeStateConflict, match="counters"):
        ProductEpisodeRuntime.restore(tampered, sleepcare_model=model)


def test_receipt_truth_distinguishes_waiting_and_terminal() -> None:
    runtime = ProductEpisodeRuntime(
        episode_id="episode-1",
        fact_snapshot=fact_snapshot(),
        sleepcare_model=SleepCareModel(),
    )
    runtime.create_plan(
        episode_type=EpisodeType.MORNING_REVIEW,
        objective="解释昨夜",
    )
    receipt = runtime.finish(status=EpisodeStatus.WAITING_USER)
    assert not receipt.terminal
    assert receipt.status == EpisodeStatus.WAITING_USER
