from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from sleepagent.product_runtime.agent_invocation_coordinator import (
    ProviderInputBudgetLedger,
)
from sleepagent.product_runtime.agents import (
    CareStrategyAgent,
    CareStrategyInput,
    CareStrategyOutput,
    EpisodePlanProposal,
    EvaluationDecision,
    EvidenceReasoningAgent,
    EvidenceReasoningInput,
    EvidenceReasoningOutput,
    ReviewTargetBinding,
    RuntimeAgentPort,
    RuntimeRoleInvocation,
    SafetyReviewAgent,
    SafetyReviewInput,
    SafetyReviewOutput,
    SleepCareAgent,
    SleepCareEvaluation,
    SleepCareEvaluationContext,
    SleepCareEvaluationInput,
    SleepCareInvocationInput,
    SleepCareInvocationOutput,
    SleepCarePlanContext,
    SleepCarePlanInput,
)
from sleepagent.product_runtime.contracts import (
    AgentId,
    AuthenticatedBinding,
    CareStrategy,
    CommunicationDraft,
    ContextPacket,
    EpisodeBudget,
    EpisodeType,
    EvidencePacket,
    FactSnapshot,
    SafetyDecision,
    SafetyVerdict,
    SourceScope,
    SourceScopeKind,
    TrustLabel,
    TrustedContextItem,
    WorkProductKind,
    WorkProductStatus,
)
from sleepagent.product_runtime.runtime_factory import (
    build_product_runtime_bundle,
)


NOW = datetime(2026, 8, 7, 7, 0, tzinfo=timezone.utc)
HASH = "a" * 64


def scope() -> SourceScope:
    return SourceScope(
        kind=SourceScopeKind.CURRENT_NIGHT,
        as_of=NOW,
        timezone_name="Asia/Shanghai",
        date_start=date(2026, 8, 7),
        date_end=date(2026, 8, 7),
        valid_night_count=1,
    )


def snapshot() -> FactSnapshot:
    return FactSnapshot.create(
        fact_snapshot_id="snapshot-1",
        binding=AuthenticatedBinding(
            actor_id="actor-1",
            subject_id="subject-1",
            role="elder",
        ),
        source_scope=scope(),
        canonical_data_version="v1",
        created_at=NOW,
    )


def context(
    agent_id: AgentId,
    *items: TrustedContextItem,
) -> ContextPacket:
    return ContextPacket(
        context_packet_id=f"context:{agent_id.value}",
        episode_id="episode-1",
        invocation_id=f"invoke:{agent_id.value}",
        agent_id=agent_id,
        objective="完成当前角色责任",
        fact_snapshot_id="snapshot-1",
        fact_snapshot_hash=HASH,
        source_scope=scope(),
        items=items,
    )


def runtime_invocation(
    agent,
    *,
    role_context: ContextPacket,
    skill_id: str,
    accepted_evidence_ref: str | None = None,
    review_target: ReviewTargetBinding | None = None,
    audience_role: str | None = None,
) -> RuntimeRoleInvocation:
    return RuntimeRoleInvocation(
        context=role_context,
        episode_type=EpisodeType.MORNING_REVIEW,
        subject_id="subject-1",
        target_id=f"target:{agent.agent_id.value}",
        target_hash_material={"role": agent.agent_id.value},
        skill_id=skill_id,
        skill_version="1.0.0",
        prompt_version=f"{skill_id}.prompt.1.0.0",
        policy_version="product-safety.v3",
        profile_version=agent.boundary.profile_version,
        profile_hash=agent.boundary.profile_hash,
        skill_package_hash="b" * 64,
        skill_lock_hash="c" * 64,
        prompt_bundle_hash="d" * 64,
        compiled_messages=(
            {"role": "system", "content": "typed role test"},
        ),
        accepted_evidence_ref=accepted_evidence_ref,
        review_target=review_target,
        audience_role=audience_role,
    )


class RoleModel:
    provider = "test"
    model_id = "typed-role-model"
    last_provider_request_id = "provider-request-1"

    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls: list[type] = []

    def generate(self, *, messages, schema, prompt_version, context_packet_id):
        self.calls.append(schema)
        return schema(
            status=WorkProductStatus.COMPLETED,
            summary="typed role completed",
            output_payload=self.payload,
        )


class SleepCareDecisionModel:
    provider = "test"
    model_id = "sleepcare-decision-model"
    last_provider_request_id = "decision-request-1"

    def generate(self, *, messages, schema, prompt_version, context_packet_id):
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
                decision=EvaluationDecision.CONTINUE,
                summary="继续受控路径",
            )
        raise AssertionError(schema)


def test_sleepcare_agent_has_typed_communication_plan_and_evaluation_ports() -> None:
    communication_model = RoleModel(
        CommunicationDraft(
            draft_id="draft-1",
            audience_role="elder",
            text="这是经过绑定的沟通草稿。",
            context_notice="仅覆盖当前授权范围。",
        )
    )
    decision_model = SleepCareDecisionModel()
    bundle = build_product_runtime_bundle(
        sleepcare_model=communication_model,
        evidence_reasoning_model=communication_model,
        care_strategy_model=communication_model,
        safety_review_model=communication_model,
        sleepcare_planning_model=decision_model,
    )
    agent = bundle.roster.sleepcare
    assert all(
        not hasattr(role, "skill_resolver")
        and not hasattr(role, "prompt_compiler")
        for role in bundle.roster
    )
    audience = TrustedContextItem(
        key="requested_audience_role",
        trust_label=TrustLabel.SYSTEM_POLICY,
        value="elder",
    )
    command = agent.bind(
        runtime_invocation(
            agent,
            role_context=context(AgentId.SLEEP_CARE, audience),
            skill_id="explain_for_elder",
            audience_role="elder",
        )
    )
    assert type(command) is SleepCareInvocationInput
    communication = agent.invoke(command)
    assert type(communication) is SleepCareInvocationOutput
    assert communication.payload.audience_role == "elder"

    fact_snapshot = snapshot()
    plan = agent.plan(
        SleepCarePlanInput(
            episode_id="episode-1",
            episode_type=EpisodeType.MORNING_REVIEW,
            fact_snapshot=fact_snapshot,
            episode_state_revision=0,
            runtime_context=SleepCarePlanContext(
                episode_id="episode-1",
                episode_type=EpisodeType.MORNING_REVIEW,
                objective="解释昨夜",
                fact_snapshot_id=fact_snapshot.fact_snapshot_id,
                fact_snapshot_hash=fact_snapshot.fact_snapshot_hash,
                source_scope=fact_snapshot.source_scope,
                registry_required_work_products=(
                    WorkProductKind.SLEEPCARE_PLAN,
                    WorkProductKind.EVIDENCE_PACKET,
                    WorkProductKind.COMMUNICATION,
                ),
                request_required_work_products=(),
                allowed_work_products=(
                    WorkProductKind.SLEEPCARE_PLAN,
                    WorkProductKind.EVIDENCE_PACKET,
                    WorkProductKind.COMMUNICATION,
                ),
                required_tools=(),
                allowed_agents=(
                    AgentId.SLEEP_CARE,
                    AgentId.EVIDENCE_REASONING,
                ),
                available_safety_checkpoints=(),
                request_required_safety_checkpoints=(),
                exit_conditions=("communication_published",),
                budget=EpisodeBudget(
                    agent_call_limit=8,
                    total_model_call_limit=10,
                    tool_call_limit=8,
                    soft_deadline_seconds=30,
                ),
            ),
            invocation_ordinal=1,
            repair_attempt=0,
        )
    )
    assert plan.proposal.objective == "解释昨夜"
    planned_tokens = bundle.provider_input_ledger.episode_total(
        "episode-1", AgentId.SLEEP_CARE
    )
    assert planned_tokens > 0
    assert plan.record.provider_input_tokens == planned_tokens
    evaluation = agent.evaluate(
        SleepCareEvaluationInput(
            episode_id="episode-1",
            episode_type=EpisodeType.MORNING_REVIEW,
            fact_snapshot=fact_snapshot,
            episode_state_revision=1,
            runtime_context=SleepCareEvaluationContext(
                episode_id="episode-1",
                episode_state_revision=1,
                latest_kind=WorkProductKind.EVIDENCE_PACKET,
                latest_ref="evidence:1",
                accepted_work_products=(WorkProductKind.EVIDENCE_PACKET,),
                required_work_products=(WorkProductKind.EVIDENCE_PACKET,),
                remaining_agent_calls=5,
                remaining_replans=2,
            ),
            invocation_ordinal=2,
            repair_attempt=0,
        )
    )
    assert evaluation.evaluation.decision is EvaluationDecision.CONTINUE
    assert (
        bundle.provider_input_ledger.episode_total(
            "episode-1", AgentId.SLEEP_CARE
        )
        > planned_tokens
    )
    assert evaluation.record.provider_input_tokens > 0
    restarted_ledger = ProviderInputBudgetLedger()
    restarted_ledger.restore_from_invocations(
        "episode-1",
        (plan.record, evaluation.record),
    )
    expected_total = (
        plan.record.provider_input_tokens
        + evaluation.record.provider_input_tokens
    )
    assert restarted_ledger.episode_total(
        "episode-1", AgentId.SLEEP_CARE
    ) == expected_total
    legacy_payload = plan.record.model_dump(mode="python")
    legacy_payload.pop("provider_input_tokens")
    legacy_record = type(plan.record).model_validate(legacy_payload)
    assert legacy_record.provider_input_tokens is None
    legacy_ledger = ProviderInputBudgetLedger()
    legacy_ledger.restore_from_invocations(
        "episode-1",
        (legacy_record,),
    )
    assert legacy_ledger.episode_total(
        "episode-1", AgentId.SLEEP_CARE
    ) == legacy_ledger.per_call_limit
    restarted_ledger.restore_from_invocations(
        "episode-1",
        (plan.record, evaluation.record),
    )
    assert restarted_ledger.episode_total(
        "episode-1", AgentId.SLEEP_CARE
    ) == expected_total


def test_terminal_provider_budget_cleanup_releases_all_process_participants() -> None:
    episode_id = "episode-provider-cross-bundle-cleanup"
    first = ProviderInputBudgetLedger()
    second = ProviderInputBudgetLedger()
    first.reserve(episode_id, AgentId.SLEEP_CARE, 100)
    second.reserve(episode_id, AgentId.SLEEP_CARE, 200)

    second.release_episode(episode_id)

    assert first.episode_total(episode_id) == 0
    assert second.episode_total(episode_id) == 0


def test_four_concrete_agents_nominally_implement_runtime_agent_port() -> None:
    implementations = (
        SleepCareAgent,
        EvidenceReasoningAgent,
        CareStrategyAgent,
        SafetyReviewAgent,
    )
    assert all(
        RuntimeAgentPort in implementation.__mro__
        for implementation in implementations
    )


def test_evidence_reasoning_agent_owns_typed_evidence_contract() -> None:
    agent = EvidenceReasoningAgent(
        RoleModel(EvidencePacket(packet_id="packet-1", source_scope=scope()))
    )
    command = agent.bind(
        runtime_invocation(
            agent,
            role_context=context(AgentId.EVIDENCE_REASONING),
            skill_id="interpret_scoped_evidence",
        )
    )
    assert type(command) is EvidenceReasoningInput
    output = agent.invoke(command)
    assert type(output) is EvidenceReasoningOutput
    assert output.payload.packet_id == "packet-1"
    with pytest.raises(ValueError, match="Evidence Context"):
        agent.bind(
            runtime_invocation(
                agent,
                role_context=context(AgentId.CARE_STRATEGY),
                skill_id="interpret_scoped_evidence",
            )
        )


def test_care_strategy_agent_requires_exact_accepted_evidence() -> None:
    agent = CareStrategyAgent(
        RoleModel(CareStrategy(strategy_id="care-1", disposition="no_action"))
    )
    evidence_item = TrustedContextItem(
        key="accepted:evidence_packet",
        trust_label=TrustLabel.ACCEPTED_WORK_PRODUCT,
        value={"packet_id": "packet-1"},
        source_refs=("evidence:1",),
    )
    command = agent.bind(
        runtime_invocation(
            agent,
            role_context=context(AgentId.CARE_STRATEGY, evidence_item),
            skill_id="propose_single_care_action",
            accepted_evidence_ref="evidence:1",
        )
    )
    assert type(command) is CareStrategyInput
    output = agent.invoke(command)
    assert type(output) is CareStrategyOutput
    assert output.payload.disposition == "no_action"
    with pytest.raises(ValueError, match="accepted Evidence"):
        agent.bind(
            runtime_invocation(
                agent,
                role_context=context(AgentId.CARE_STRATEGY),
                skill_id="propose_single_care_action",
            )
        )


def test_safety_review_agent_requires_one_exact_hash_bound_target() -> None:
    review_target = ReviewTargetBinding(
        work_product_ref="care:1",
        target_id="care-target-1",
        target_hash="e" * 64,
        episode_state_revision=3,
    )
    safety_item = TrustedContextItem(
        key="safety_review_target",
        trust_label=TrustLabel.ACCEPTED_WORK_PRODUCT,
        value={
            "target_id": review_target.target_id,
            "target_hash": review_target.target_hash,
            "episode_state_revision": review_target.episode_state_revision,
            "payload": {},
        },
        source_refs=(review_target.work_product_ref,),
    )
    agent = SafetyReviewAgent(
        RoleModel(
            SafetyDecision(
                verdict=SafetyVerdict.APPROVE,
                review_target_type="care_strategy",
                review_target_id=review_target.target_id,
                review_target_hash=review_target.target_hash,
                fact_snapshot_hash=HASH,
                reviewed_episode_state_revision=3,
                policy_version="product-safety.v3",
                expires_at=NOW + timedelta(minutes=10),
            )
        )
    )
    command = agent.bind(
        runtime_invocation(
            agent,
            role_context=context(AgentId.SAFETY_REVIEW, safety_item),
            skill_id="review_action_and_publication",
            review_target=review_target,
        )
    )
    assert type(command) is SafetyReviewInput
    output = agent.invoke(command)
    assert type(output) is SafetyReviewOutput
    assert output.payload.verdict is SafetyVerdict.APPROVE

    mismatched = review_target.model_copy(update={"target_hash": "f" * 64})
    with pytest.raises(ValueError, match="binding"):
        agent.bind(
            runtime_invocation(
                agent,
                role_context=context(AgentId.SAFETY_REVIEW, safety_item),
                skill_id="review_action_and_publication",
                review_target=mismatched,
            )
        )


def test_role_rechecks_prompt_binding_before_calling_model() -> None:
    model = RoleModel(EvidencePacket(packet_id="packet-2", source_scope=scope()))
    agent = EvidenceReasoningAgent(model)
    command = agent.bind(
        runtime_invocation(
            agent,
            role_context=context(AgentId.EVIDENCE_REASONING),
            skill_id="interpret_scoped_evidence",
        )
    )
    tampered = command.model_copy(
        update={
            "invocation": command.invocation.model_copy(
                update={
                    "compiled_messages": (
                        {"role": "system", "content": "bypass role boundary"},
                    )
                }
            )
        }
    )
    with pytest.raises(ValueError, match="prompt binding"):
        agent.invoke(tampered)
    assert model.calls == []


def test_care_context_rejects_raw_user_data_and_string_agent_alias() -> None:
    agent = CareStrategyAgent(
        RoleModel(CareStrategy(strategy_id="care-2", disposition="no_action"))
    )
    evidence_item = TrustedContextItem(
        key="accepted:evidence_packet",
        trust_label=TrustLabel.ACCEPTED_WORK_PRODUCT,
        value={"packet_id": "packet-2"},
        source_refs=("evidence:2",),
    )
    raw_user_item = TrustedContextItem(
        key="user_text",
        trust_label=TrustLabel.USER_TEXT_UNTRUSTED,
        value="不可越权传入 Care",
        source_refs=("user_report:2",),
    )
    with pytest.raises(ValueError, match="Context trust label"):
        agent.bind(
            runtime_invocation(
                agent,
                role_context=context(
                    AgentId.CARE_STRATEGY,
                    evidence_item,
                    raw_user_item,
                ),
                skill_id="propose_single_care_action",
                accepted_evidence_ref="evidence:2",
            )
        )
    assert not agent.can_view_work_product("evidence_reasoning")  # type: ignore[arg-type]


def test_sleepcare_plan_rejects_untyped_runtime_context() -> None:
    with pytest.raises(ValueError):
        SleepCarePlanInput(
            episode_id="episode-1",
            episode_type=EpisodeType.MORNING_REVIEW,
            fact_snapshot=snapshot(),
            episode_state_revision=0,
            runtime_context={"objective": "untyped"},
            invocation_ordinal=1,
            repair_attempt=0,
        )
