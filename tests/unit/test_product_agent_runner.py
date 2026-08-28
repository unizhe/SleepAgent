from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import sleepagent.runtime.runner as runner_module
from sleepagent.domain.habit import HabitFact, HabitOperation
from sleepagent.domain.contracts import DataMode
from sleepagent.runtime.agents import ProductAgentFactory
from sleepagent.runtime.agents import (
    EpisodePlanProposal,
    EvaluationDecision,
    SleepCareEvaluation,
    _CareStrategyPlan,
    _CareStrategySelectedAction,
    _SleepCareContentPlan,
)
from sleepagent.runtime.cold_start import (
    ClaimKind,
    build_unavailable_entry_decisions,
    snapshot_binding_material,
)
from sleepagent.runtime.contracts import (
    AgentId,
    AuthenticatedBinding,
    CareActionCandidate,
    CareStrategy,
    CommunicationDraft,
    CommunicationSemanticBinding,
    CurrentContextRisk,
    CrossAgentRequest,
    CrossAgentRequestType,
    EpisodeStatus,
    EpisodeType,
    EvidenceClaim,
    EvidencePacket,
    EvidenceSemantic,
    EvidenceSourceKind,
    ExecutionMode,
    ExternalActionTarget,
    FactSnapshot,
    InvocationOutcome,
    LongitudinalTrend,
    MemoryChangeCandidate,
    MultifactorSafetyInput,
    MultiSourceConsistency,
    OnlineDataQuality,
    OnlineEventType,
    OnlineReasoningEvent,
    OnlineRiskLevel,
    SafetyDecision,
    SafetyVerdict,
    SourceScope,
    SourceScopeKind,
    RelativeBaselineDeviation,
    ToolEffect,
    ToolRequest,
    TrustLabel,
    WorkProductKind,
    WorkProductStatus,
    stable_hash,
)
from sleepagent.runtime.governance import (
    AcceptanceError,
    DeterministicCommitController,
)
from sleepagent.runtime.hitl import (
    HITL_POLICY_VERSION,
    ActionProposal,
    DecisionExplanation,
    HumanDecisionChoice,
    HumanDecisionService,
)
from sleepagent.runtime.invocation import (
    CareStrategyModelOutput,
    EvidenceReasoningModelOutput,
    SafetyReviewModelOutput,
    SleepCareModelOutput,
)
from sleepagent.runtime.memory import (
    MemoryHandle,
    MemoryItemStatus,
    MemoryPurpose,
    MemoryReadReceipt,
    MemorySliceItem,
    ProvenanceType,
)
from sleepagent.runtime.registry import EPISODE_DEFINITIONS
from sleepagent.runtime.runner import ProductEpisodeRunner
from sleepagent.runtime.results import (
    PinnedPersonalizationContext,
    ProductEpisodeRunRequest,
    ProductUserFactResponse,
    bind_product_episode_checkpoint,
    product_episode_request_hash,
)
from sleepagent.runtime.factory import (
    ProductRuntimeBundle,
    build_deterministic_product_runtime_bundle,
    build_product_runtime_bundle,
)
from sleepagent.runtime.deterministic_model import (
    DeterministicReplayStructuredAgentModel,
)
from sleepagent.runtime.registry import (
    SkillRegistry,
    default_agent_profiles,
    default_skill_packages,
)
from sleepagent.runtime.reports import (
    ElderNarrativeRequest,
    ElderNarrativeState,
    ReportRole,
    RoleProjectionState,
    SharedAnalysisRunRequest,
    SharedAnalysisSourceV1,
    build_shared_role_projections,
)
from sleepagent.runtime.schemas import RadarNightSummary
from sleepagent.persistence.uow import UowScope
from sleepagent.workers.product import (
    _consumed_context_sha256,
    _desired_analysis_sha256,
    _fact_snapshot_for_shared,
)


NOW = datetime(2026, 7, 26, 7, 0, tzinfo=timezone.utc)
VALID_UNTIL = datetime.now(timezone.utc) + timedelta(days=30)


def test_episode_request_cannot_inject_question_suppressions() -> None:
    assert "habit_suppressions" not in ProductEpisodeRunRequest.model_fields
    assert (
        "habit_suppression_confirmation_ref"
        not in ProductEpisodeRunRequest.model_fields
    )
    assert {
        "care_confirmation",
        "memory_confirmations",
        "external_confirmation",
        "declined_confirmation_ids",
    }.isdisjoint(ProductEpisodeRunRequest.model_fields)


def test_legacy_entry_without_exact_cohort_publishes_reviewed_boundary() -> None:
    instance, _ = runner(EpisodeType.MORNING_REVIEW)
    base = request(EpisodeType.MORNING_REVIEW)
    decisions = build_unavailable_entry_decisions(
        decision_namespace="runner-entry",
        claim_kind=ClaimKind.DESCRIBE_CURRENT_NIGHT,
    )
    old_snapshot = base.fact_snapshot
    guarded_snapshot = FactSnapshot.create(
        fact_snapshot_id="snapshot-cold-start-entry",
        binding=old_snapshot.binding,
        source_scope=old_snapshot.source_scope.model_copy(
            update={"valid_night_count": 0}
        ),
        canonical_data_version=old_snapshot.canonical_data_version,
        care_context_version=old_snapshot.care_context_version,
        memory_context_version=old_snapshot.memory_context_version,
        source_refs=old_snapshot.source_refs,
        **snapshot_binding_material(decisions=decisions),
        created_at=NOW,
    )
    guarded = ProductEpisodeRunRequest.model_validate(
        base.model_copy(
            update={
                "fact_snapshot": guarded_snapshot,
                "runtime_readiness_decisions": decisions,
            }
        ).model_dump(mode="python")
    )

    result = instance.run(guarded)

    assert result.receipt.status == EpisodeStatus.PARTIAL
    assert result.publication is not None
    assert "还没有可用于这项判断的个人记录" in result.publication.text


def test_deterministic_assembly_preserves_cold_start_degraded_boundary() -> None:
    model = DeterministicReplayStructuredAgentModel()
    instance = build_deterministic_product_runtime_bundle(model=model).runner
    base = request(EpisodeType.MORNING_REVIEW)
    decisions = build_unavailable_entry_decisions(
        decision_namespace="deterministic-runner-entry",
        claim_kind=ClaimKind.DESCRIBE_CURRENT_NIGHT,
    )
    old_snapshot = base.fact_snapshot
    guarded_snapshot = FactSnapshot.create(
        fact_snapshot_id="snapshot-deterministic-cold-start-entry",
        binding=old_snapshot.binding,
        source_scope=old_snapshot.source_scope.model_copy(
            update={"valid_night_count": 0}
        ),
        canonical_data_version=old_snapshot.canonical_data_version,
        care_context_version=old_snapshot.care_context_version,
        memory_context_version=old_snapshot.memory_context_version,
        source_refs=old_snapshot.source_refs,
        **snapshot_binding_material(decisions=decisions),
        created_at=NOW,
    )
    guarded = ProductEpisodeRunRequest.model_validate(
        base.model_copy(
            update={
                "fact_snapshot": guarded_snapshot,
                "runtime_readiness_decisions": decisions,
            }
        ).model_dump(mode="python")
    )

    result = instance.run(guarded)

    assert result.receipt.status is EpisodeStatus.COMPLETE
    assert result.publication is not None
    assert "还没有可用于这项判断的个人记录" in result.publication.text
    assert all(
        binding.rendered_text in result.publication.text
        for binding in result.publication.semantic_bindings
    )


def snapshot(
    kind: SourceScopeKind = SourceScopeKind.CURRENT_NIGHT,
    *,
    role: str = "elder",
    authorization_scope: tuple[str, ...] | None = None,
) -> FactSnapshot:
    if kind == SourceScopeKind.GENERAL_KNOWLEDGE:
        scope = SourceScope(
            kind=kind, as_of=NOW, timezone_name="Asia/Shanghai"
        )
    else:
        days = 30 if kind == SourceScopeKind.THIRTY_DAY else 1
        scope = SourceScope(
            kind=kind,
            as_of=NOW,
            timezone_name="Asia/Shanghai",
            date_start=date(2026, 7, 26) - timedelta(days=days - 1),
            date_end=date(2026, 7, 26),
            valid_night_count=days,
        )
    return FactSnapshot.create(
        fact_snapshot_id=f"snapshot-{kind.value}",
        binding=AuthenticatedBinding(
            actor_id="actor-1",
            subject_id="subject-1",
            role=role,
            authorization_scope=(
                authorization_scope
                if authorization_scope is not None
                else ("read_sleep_data", "draft_material")
            ),
        ),
        source_scope=scope,
        canonical_data_version="v1",
        source_refs=(
            "night:1",
            "range:1",
            *(
                tuple(
                    f"night-summary:radar-1:2026-07-{day:02d}"
                    for day in range(20, 27)
                )
                if kind == SourceScopeKind.THIRTY_DAY
                else ()
            ),
        ),
        created_at=NOW,
    )


def authorize_target(
    service: HumanDecisionService,
    *,
    episode_id: str,
    fact_snapshot: FactSnapshot,
    target_kind: str,
    target_id: str,
    target_hash: str,
    action_scope: str,
    expires_at: datetime,
    idempotency_key: str | None = None,
    approve: bool = True,
):
    created_at = expires_at - timedelta(minutes=10)
    proposal = ActionProposal(
        proposal_id=f"proposal:{episode_id}:{target_kind}:{target_id}",
        episode_id=episode_id,
        subject_id=fact_snapshot.binding.subject_id,
        proposer_actor_id=fact_snapshot.binding.actor_id,
        action_kind=target_kind,
        action_scope=action_scope,
        target_id=target_id,
        target_hash=target_hash,
        fact_snapshot_id=fact_snapshot.fact_snapshot_id,
        fact_snapshot_hash=fact_snapshot.fact_snapshot_hash,
        policy_version=HITL_POLICY_VERSION,
        payload={"target_id": target_id, "target_hash": target_hash},
        explanation=DecisionExplanation(
            what_will_change="Commit the exact frozen test target.",
            why_now="The Product Episode is waiting for accountable approval.",
            who_will_receive_or_be_affected="The authenticated subject.",
            duration_or_frequency="This exact target version only.",
            how_to_revoke="Revoke through HumanDecisionService before execution.",
        ),
        created_at=created_at,
        expires_at=expires_at,
    )
    decision = service.create(proposal)
    actor_id = (
        fact_snapshot.binding.actor_id
        if fact_snapshot.binding.role == "elder"
        else "elder-owner"
    )
    decision = service.decide(
        decision.decision_id,
        actor_id=actor_id,
        actor_role="elder",
        choice=(
            HumanDecisionChoice.APPROVE
            if approve
            else HumanDecisionChoice.REJECT
        ),
        target_hash=target_hash,
        now=created_at + timedelta(seconds=1),
    )
    if idempotency_key is None or not approve:
        return decision
    return service.acquire_verified_capability(
        decision.decision_id,
        expected_proposal_id=proposal.proposal_id,
        expected_subject_id=proposal.subject_id,
        expected_target_id=target_id,
        expected_target_hash=target_hash,
        expected_action_scope=action_scope,
        expected_fact_snapshot_id=fact_snapshot.fact_snapshot_id,
        expected_fact_snapshot_hash=fact_snapshot.fact_snapshot_hash,
        expected_policy_version=HITL_POLICY_VERSION,
        idempotency_key=idempotency_key,
        now=created_at + timedelta(seconds=2),
    )


def bind_frozen_target(
    result,
    target,
    decision,
):
    bound = target.model_copy(
        update={
            "decision_id": decision.decision_id,
            "proposal_id": decision.proposal.proposal_id,
        }
    )
    return bind_product_episode_checkpoint(
        result.model_copy(
            update={
                "pending_confirmations": [
                    bound
                    if item.confirmation_id == target.confirmation_id
                    else item
                    for item in result.pending_confirmations
                ]
            }
        )
    )


class ScenarioModel:
    provider = "test"
    model_id = "scenario-model"

    def __init__(
        self,
        episode_type: EpisodeType,
        *,
        low_confidence: bool = False,
        safety_verdicts: list[SafetyVerdict] | None = None,
        fail_agent: AgentId | None = None,
    ) -> None:
        self.episode_type = episode_type
        self.low_confidence = low_confidence
        self.safety_verdicts = list(safety_verdicts or [SafetyVerdict.APPROVE])
        self.fail_agent = fail_agent
        self.calls: list[str] = []
        self.contexts: list[dict] = []

    def generate(self, *, messages, schema, prompt_version, context_packet_id):
        self.calls.append(schema.__name__)
        if schema is EpisodePlanProposal:
            packet = json.loads(messages[-1]["content"])
            context = next(
                item["value"]
                for item in packet["items"]
                if item["key"] == "runtime_episode_context"
            )
            definition = EPISODE_DEFINITIONS[self.episode_type]
            required = set(definition.required_work_products) | {
                WorkProductKind(item)
                for item in context["request_required_work_products"]
            }
            checkpoints = list(context["request_required_safety_checkpoints"])
            return schema(
                objective=context["objective"],
                required_work_products=sorted(
                    required, key=lambda item: item.value
                ),
                safety_checkpoints=checkpoints,
                exit_conditions=[sorted(definition.exit_conditions)[0]],
                expected_agent_calls=min(
                    definition.budget.agent_call_limit,
                    len(required) + 3,
                ),
                expected_tool_calls=len(definition.required_tools),
            )
        if schema is SleepCareEvaluation:
            return schema(
                decision=EvaluationDecision.CONTINUE,
                summary="继续最小路径",
            )

        context = json.loads(messages[-1]["content"])
        self.contexts.append(context)
        agent = AgentId(context["agent_id"])
        if self.fail_agent == agent:
            raise TimeoutError(agent.value)
        scope = SourceScope.model_validate(context["source_scope"])
        accepted_items = [
            item
            for item in context["items"]
            if item["trust_label"] == "accepted_work_product"
        ]
        accepted = {
            item["key"]: item["value"]
            for item in accepted_items
        }
        if schema is EvidenceReasoningModelOutput:
            semantic = (
                EvidenceSemantic.INFERENCE
                if self.low_confidence
                else EvidenceSemantic.OBSERVED_FACT
            )
            return schema(
                status=WorkProductStatus.COMPLETED,
                summary="完成证据判断",
                output_payload=EvidencePacket(
                    packet_id=f"evidence:{context_packet_id}",
                    source_scope=scope,
                    claims=[
                        EvidenceClaim(
                            claim_id="claim-1",
                            semantic=semantic,
                            statement="记录显示当前睡眠信息可解释",
                            source_kind=EvidenceSourceKind.CANONICAL_OBSERVATION,
                            evidence_refs=["night:1"],
                            confidence=0.6 if self.low_confidence else 0.9,
                            alternative_explanations=(
                                ["设备差异"] if self.low_confidence else []
                            ),
                            date_start=scope.date_start,
                            date_end=scope.date_end,
                        )
                    ],
                ),
            )
        if schema is _CareStrategyPlan:
            return schema(
                disposition="propose",
                summary="形成单一行动",
                selected_action=_CareStrategySelectedAction(
                    care_action_id="consistent-wake-time",
                    version=1,
                    title="连续五天固定起床时间",
                    rationale_evidence_refs=["claim-1"],
                    parameters={"tolerance_minutes": 30},
                    duration_days=5,
                    stop_conditions=["不适时停止"],
                    activatable=True,
                ),
            )
        if schema is CareStrategyModelOutput:
            evidence_ref = next(
                item["source_refs"][0]
                for item in accepted_items
                if item["value"].get("claims")
            )
            return schema(
                status=WorkProductStatus.COMPLETED,
                summary="形成单一行动",
                output_payload=CareStrategy(
                    strategy_id=f"care:{context_packet_id}",
                    disposition="propose",
                    evidence_packet_refs=[evidence_ref],
                    primary_action=CareActionCandidate.create(
                        candidate_id="care-1",
                        candidate_version=1,
                        care_action_id="consistent-wake-time",
                        care_action_version=1,
                        title="连续五天固定起床时间",
                        rationale_evidence_refs=["claim-1"],
                        parameters={"tolerance_minutes": 30},
                        duration_days=5,
                        stop_conditions=["不适时停止"],
                        activatable=True,
                    ),
                ),
            )
        if schema is SafetyReviewModelOutput:
            target = accepted["safety_review_target"]
            verdict = (
                self.safety_verdicts.pop(0)
                if self.safety_verdicts
                else SafetyVerdict.APPROVE
            )
            return schema(
                status=WorkProductStatus.COMPLETED,
                summary=f"Safety {verdict.value}",
                output_payload=SafetyDecision(
                    verdict=verdict,
                    review_target_type="work_product",
                    review_target_id=target["target_id"],
                    review_target_hash=target["target_hash"],
                    fact_snapshot_hash=context["fact_snapshot_hash"],
                    reviewed_episode_state_revision=target[
                        "episode_state_revision"
                    ],
                    policy_version="product-safety.v3",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    reason_codes=(
                        ["remove_uncertain_cause"]
                        if verdict != SafetyVerdict.APPROVE
                        else []
                    ),
                    responsible_agent=(
                        AgentId.EVIDENCE_REASONING
                        if verdict == SafetyVerdict.REVISE
                        else None
                    ),
                    issue_locations=(
                        ["claims[0]"]
                        if verdict == SafetyVerdict.REVISE
                        else []
                    ),
                    conservative_fallback=(
                        "删除争议内容并明确暂时无法判断"
                        if verdict == SafetyVerdict.BLOCK
                        else None
                    ),
                ),
            )
        if schema is SleepCareModelOutput:
            claims = []
            care_refs = []
            for value in accepted.values():
                claims.extend(item["claim_id"] for item in value.get("claims", []))
                action = value.get("primary_action")
                if action:
                    care_refs.append(action["candidate_id"])
            rendered = "这是基于已验收结果的说明。"
            audience_role = next(
                (
                    item["value"]
                    for item in context["items"]
                    if item["key"] == "requested_audience_role"
                ),
                "elder",
            )
            semantic_bindings = [
                CommunicationSemanticBinding(
                    binding_id=f"claim-binding:{ref}",
                    source_kind="evidence_claim",
                    source_ref=ref,
                    rendered_text=rendered,
                )
                for ref in sorted(set(claims))
            ] + [
                CommunicationSemanticBinding(
                    binding_id=f"care-binding:{ref}",
                    source_kind="care_candidate",
                    source_ref=ref,
                    rendered_text=rendered,
                )
                for ref in sorted(set(care_refs))
            ]
            return schema(
                status=WorkProductStatus.COMPLETED,
                summary="发布统一表达",
                output_payload=CommunicationDraft(
                    draft_id=f"draft:{context_packet_id}",
                    audience_role=audience_role,
                    text=rendered,
                    claim_refs=sorted(set(claims)),
                    care_candidate_refs=sorted(set(care_refs)),
                    semantic_bindings=semantic_bindings,
                    context_notice="已标明当前数据范围与未知项。",
                    artifact_kind=(
                        "doctor_material"
                        if self.episode_type == EpisodeType.ROLE_MATERIAL
                        else None
                    ),
                ),
            )
        raise AssertionError(schema)


class ProviderReceiptScenarioModel(ScenarioModel):
    def __init__(self, episode_type: EpisodeType) -> None:
        super().__init__(episode_type)
        self._provider_request_counter = 0
        self.last_provider_request_id: str | None = None

    def generate(self, **kwargs):
        result = super().generate(**kwargs)
        self._provider_request_counter += 1
        self.last_provider_request_id = (
            f"provider-request:{self._provider_request_counter}"
        )
        return result


class CommunicationAcceptanceRepairScenarioModel(ScenarioModel):
    def __init__(self, episode_type: EpisodeType) -> None:
        super().__init__(episode_type)
        self.communication_calls = 0

    def generate(self, **kwargs):
        result = super().generate(**kwargs)
        if kwargs["schema"] is not SleepCareModelOutput:
            return result
        self.communication_calls += 1
        if self.communication_calls != 1:
            return result
        draft = result.output_payload
        assert isinstance(draft, CommunicationDraft)
        claim_ref = draft.claim_refs[0]
        rendered = "今晚增加99分钟。"
        return result.model_copy(
            update={
                "output_payload": CommunicationDraft(
                    draft_id=draft.draft_id,
                    audience_role=draft.audience_role,
                    text=rendered,
                    claim_refs=[claim_ref],
                    semantic_bindings=[
                        CommunicationSemanticBinding(
                            binding_id="invented-number",
                            source_kind="evidence_claim",
                            source_ref=claim_ref,
                            rendered_text=rendered,
                        )
                    ],
                    context_notice=draft.context_notice,
                )
            }
        )


class ProvenanceAcceptanceRepairScenarioModel(ScenarioModel):
    def __init__(self, episode_type: EpisodeType) -> None:
        super().__init__(episode_type)
        self.evidence_calls = 0
        self.safety_calls = 0
        self.evidence_revision_reasons: list[str] = []

    def generate(self, **kwargs):
        result = super().generate(**kwargs)
        if kwargs["schema"] is EvidenceReasoningModelOutput:
            self.evidence_calls += 1
            context = json.loads(kwargs["messages"][-1]["content"])
            self.evidence_revision_reasons.extend(
                str(item["value"])
                for item in context["items"]
                if item["key"] == "revision_reason"
            )
            if self.evidence_calls == 1:
                packet = result.output_payload
                claim = packet.claims[0]
                return result.model_copy(
                    update={
                        "output_payload": packet.model_copy(
                            update={
                                "claims": [
                                    claim.model_copy(
                                        update={
                                            "date_start": (
                                                claim.date_start
                                                - timedelta(days=1)
                                            )
                                        }
                                    ),
                                    *packet.claims[1:],
                                ]
                            }
                        )
                    }
                )
        if kwargs["schema"] is SafetyReviewModelOutput:
            self.safety_calls += 1
            if self.safety_calls == 1:
                decision = result.output_payload
                return result.model_copy(
                    update={
                        "output_payload": decision.model_copy(
                            update={
                                "reviewed_episode_state_revision": (
                                    decision.reviewed_episode_state_revision + 1
                                )
                            }
                        )
                    }
                )
        return result


class ToolFeedbackScenarioModel(ScenarioModel):
    def generate(self, **kwargs):
        result = super().generate(**kwargs)
        if kwargs["schema"] is not EvidenceReasoningModelOutput:
            return result
        context = json.loads(kwargs["messages"][-1]["content"])
        has_feedback = any(
            item["key"] == "tool:knowledge.retrieve_reviewed"
            for item in context["items"]
        )
        if has_feedback:
            return result
        return result.model_copy(
            update={
                "tool_requests": [
                    ToolRequest(
                        request_id="knowledge-for-evidence",
                        tool_name="knowledge.retrieve_reviewed",
                        arguments={
                            "query": "睡眠解释需结合个人数据质量",
                            "roles": ["elder"],
                            "limit": 8,
                        },
                    )
                ]
            }
        )


class CollaborationFeedbackScenarioModel(ScenarioModel):
    def __init__(self, episode_type: EpisodeType) -> None:
        super().__init__(episode_type)
        self.care_rounds = 0

    def generate(self, **kwargs):
        result = super().generate(**kwargs)
        if kwargs["schema"] is not CareStrategyModelOutput:
            return result
        self.care_rounds += 1
        if self.care_rounds > 1:
            return result
        context = json.loads(kwargs["messages"][-1]["content"])
        return result.model_copy(
            update={
                "collaboration_requests": [
                    CrossAgentRequest(
                        request_id="care-needs-evidence-recheck",
                        sender=AgentId.CARE_STRATEGY,
                        receiver=AgentId.EVIDENCE_REASONING,
                        request_type=CrossAgentRequestType.EVIDENCE,
                        objective="复核当前行动所需的关键证据",
                        source_scope=SourceScope.model_validate(
                            context["source_scope"]
                        ),
                        reason_codes=["care_evidence_gap"],
                    )
                ]
            }
        )


class UserFactFeedbackScenarioModel(ScenarioModel):
    def generate(self, **kwargs):
        result = super().generate(**kwargs)
        if kwargs["schema"] is not EvidenceReasoningModelOutput:
            return result
        context = json.loads(kwargs["messages"][-1]["content"])
        if any(
            item["key"].startswith("user_fact_response:")
            for item in context["items"]
        ):
            return result
        return result.model_copy(
            update={
                "collaboration_requests": [
                    CrossAgentRequest(
                        request_id="user-fact-bedtime",
                        sender=AgentId.EVIDENCE_REASONING,
                        receiver=AgentId.SLEEP_CARE,
                        request_type=CrossAgentRequestType.USER_FACT,
                        objective="昨晚是否比平时更晚入睡？",
                        source_scope=SourceScope.model_validate(
                            context["source_scope"]
                        ),
                        target_type="evidence_gap",
                        target_id="bedtime-context",
                        reason_codes=["missing_observable_context"],
                    )
                ]
            }
        )


class MemoryScenarioModel(ScenarioModel):
    def generate(self, **kwargs):
        result = super().generate(**kwargs)
        if kwargs["schema"] is not SleepCareModelOutput:
            return result
        context = json.loads(kwargs["messages"][-1]["content"])
        user_text = next(
            item["value"]
            for item in context["items"]
            if item["key"] == "user_text"
        )
        candidate = MemoryChangeCandidate(
            candidate_id="memory-weekend-wake",
            operation="create",
            subject_id="subject-1",
            memory_type="preference",
            concept_id="sleep.weekend_wake_preference",
            value_schema_id="bounded_string.v1",
            typed_value="周末希望晚起半小时",
            provenance_type="elder_confirmed",
            source_ref=f"user_report:{stable_hash(user_text)[:16]}",
            sensitivity_class="personal",
            allowed_roles=(AgentId.SLEEP_CARE, AgentId.EVIDENCE_REASONING),
            allowed_purposes=(
                "personal_evidence_context",
                "explicit_memory_review",
                "explicit_memory_change",
                "explicit_memory_forget",
            ),
            explicit_user_authorization=True,
            confirmation_required=True,
        )
        return result.model_copy(
            update={
                "output_payload": result.output_payload.model_copy(
                    update={"memory_change_candidates": [candidate]}
                )
            }
        )


class ProfileAwareScenarioModel(ScenarioModel):
    def generate(self, *, messages, schema, prompt_version, context_packet_id):
        if schema is EvidenceReasoningModelOutput:
            context = json.loads(messages[-1]["content"])
            profile_items = [
                item
                for item in context["items"]
                if item["key"] == "tool_receipt:profile.read"
            ]
            facts = (
                profile_items[0]["value"]["output"]["profile"]["facts"]
                if profile_items
                else []
            )
            if facts:
                self.calls.append(schema.__name__)
                self.contexts.append(context)
                fact = facts[0]
                return schema(
                    status=WorkProductStatus.COMPLETED,
                    summary="只使用当前问题相关的已确认画像",
                    output_payload=EvidencePacket(
                        packet_id=f"evidence:{context_packet_id}",
                        source_scope=SourceScope.model_validate(
                            context["source_scope"]
                        ),
                        claims=[
                            EvidenceClaim(
                                claim_id="habit-claim",
                                semantic=EvidenceSemantic.USER_REPORTED,
                                statement=(
                                    f"{fact['concept_id']}={fact['value']}"
                                ),
                                source_kind=EvidenceSourceKind.CONFIRMED_MEMORY,
                                evidence_refs=[fact["fact_ref"]],
                                confidence=1,
                            )
                        ],
                    ),
                )
        return super().generate(
            messages=messages,
            schema=schema,
            prompt_version=prompt_version,
            context_packet_id=context_packet_id,
        )


class LongitudinalMemoryScenarioModel(ScenarioModel):
    """Structured model that exercises read feedback before emitting Evidence."""

    def __init__(
        self,
        episode_type: EpisodeType,
        *,
        memory_type: str,
        concept_id: str,
    ) -> None:
        super().__init__(episode_type)
        self.memory_type = memory_type
        self.concept_id = concept_id

    def generate(self, **kwargs):
        result = super().generate(**kwargs)
        if kwargs["schema"] is not EvidenceReasoningModelOutput:
            return result
        context = json.loads(kwargs["messages"][-1]["content"])
        if not kwargs["context_packet_id"].endswith(":query"):
            return result
        memory_read = next(
            (
                item
                for item in context["items"]
                if item["key"] == "tool:memory.read"
            ),
            None,
        )
        if memory_read is None:
            return result.model_copy(
                update={
                    "tool_requests": [
                        ToolRequest(
                            request_id="read-longitudinal-memory",
                            tool_name="memory.read",
                            arguments={
                                "purpose": "personal_evidence_context",
                                "memory_types": [self.memory_type],
                                "selector_kind": "concept_ids",
                                "requested_concept_ids": [self.concept_id],
                                "requested_time_scope": "current_night",
                            },
                        )
                    ]
                }
            )
        items = memory_read["value"]["items"]
        assert len(items) == 1
        if self.memory_type == "episode_digest":
            resolved = next(
                (
                    item
                    for item in context["items"]
                    if item["key"] == "tool:memory.resolve_source"
                ),
                None,
            )
            if resolved is None:
                return result.model_copy(
                    update={
                        "tool_requests": [
                            ToolRequest(
                                request_id="resolve-longitudinal-source",
                                tool_name="memory.resolve_source",
                                arguments={
                                    "retrieval_handle": items[0][
                                        "retrieval_handle"
                                    ]
                                },
                            )
                        ]
                    }
                )
            evidence_ref = resolved["value"]["canonical_source_handle"]
            source_kind = EvidenceSourceKind.CANONICAL_OBSERVATION
        else:
            evidence_ref = items[0]["retrieval_handle"]
            source_kind = EvidenceSourceKind.CONFIRMED_MEMORY
        scope = SourceScope.model_validate(context["source_scope"])
        return result.model_copy(
            update={
                "tool_requests": [],
                "output_payload": EvidencePacket(
                    packet_id=f"memory-evidence:{kwargs['context_packet_id']}",
                    source_scope=scope,
                    claims=[
                        EvidenceClaim(
                            claim_id=f"memory-claim:{kwargs['context_packet_id']}",
                            semantic=EvidenceSemantic.OBSERVED_FACT,
                            statement="当前授权来源支持这项最小个人上下文。",
                            source_kind=source_kind,
                            evidence_refs=[evidence_ref],
                            confidence=0.9,
                            date_start=scope.date_start,
                            date_end=scope.date_end,
                        )
                    ],
                ),
            }
        )


def runner(
    episode_type: EpisodeType,
    **model_options,
) -> tuple[ProductEpisodeRunner, ScenarioModel]:
    model = ScenarioModel(episode_type, **model_options)
    return product_runner(model), model


def runtime_bundle(
    model,
    *,
    source_resolvers=None,
) -> ProductRuntimeBundle:
    return build_product_runtime_bundle(
        sleepcare_model=model,
        evidence_reasoning_model=model,
        care_strategy_model=model,
        safety_review_model=model,
        sleepcare_planning_model=model,
        source_resolvers=source_resolvers,
    )


def product_runner(model, **options) -> ProductEpisodeRunner:
    return runtime_bundle(model, **options).runner


def request(
    episode_type: EpisodeType,
    *,
    personalized=True,
    doctor_material=False,
    user_text="昨晚睡得怎么样？",
    binding_role="elder",
    binding_authorization_scope=None,
    **extra,
) -> ProductEpisodeRunRequest:
    kind = (
        SourceScopeKind.THIRTY_DAY
        if episode_type == EpisodeType.TREND_REVIEW
        else (
            SourceScopeKind.GENERAL_KNOWLEDGE
            if not personalized
            else SourceScopeKind.CURRENT_NIGHT
        )
    )
    inputs = {
        "radar.get_night_evidence": {"source_refs": ["night:1"]},
        "radar.get_range_evidence": {"source_refs": ["range:1"]},
        "radar.assess_data_quality": {
            "coverage_ratio": 0.9,
            "source_refs": ["night:1", "range:1"],
        },
        "trend.calculate_metrics": {
            "values": [7.0, 6.8, 6.5],
            "source_refs": ["range:1"],
        },
        "care.read_catalog": {},
        "care.read_state": {},
        "artifact.render": {"content": "draft"},
    }
    if episode_type == EpisodeType.TREND_REVIEW:
        inputs["trend.calculate_metrics"] = {
            "night_summaries": [
                RadarNightSummary(
                    radar_device_id="radar-1",
                    subject_id="subject-1",
                    night_of=date(2026, 7, day),
                    timezone_name="Asia/Shanghai",
                    total_sleep_minutes=360 + (day - 20) * 5,
                    data_coverage_ratio=0.95,
                    explainable_metrics={
                        "calibration_state": "known_uncalibrated"
                    },
                    source_report_ref=(
                        f"night-summary:radar-1:2026-07-{day:02d}"
                    ),
                ).model_dump(mode="json")
                for day in range(20, 27)
            ]
        }
    return ProductEpisodeRunRequest(
        episode_id=f"episode-{episode_type.value}",
        episode_type=episode_type,
        objective="完成当前睡眠照护任务",
        fact_snapshot=snapshot(
            kind,
            role=binding_role,
            authorization_scope=binding_authorization_scope,
        ),
        user_text=user_text,
        tool_inputs=inputs,
        personalized=personalized,
        doctor_material=doctor_material,
        **extra,
    )


def test_concrete_roster_preserves_phase_c_tool_contract_audit_identity() -> None:
    roster_runner, _ = runner(EpisodeType.MORNING_REVIEW)
    episode_request = request(EpisodeType.MORNING_REVIEW)

    roster_result = roster_runner.run(episode_request)

    def audit_projection(result):
        return [
            {
                "invocation_id": item.invocation_id,
                "agent_id": item.agent_id,
                "agent_version": item.agent_version,
                "profile_version": item.profile_version,
                "profile_hash": item.profile_hash,
                "skill_id": item.skill_id,
                "skill_version": item.skill_version,
                "skill_package_hash": item.skill_package_hash,
                "skill_lock_hash": item.skill_lock_hash,
                "prompt_bundle_hash": item.prompt_bundle_hash,
                "context_packet_id": item.context_packet_id,
                "context_hash": item.context_hash,
                "target_hash": item.target_hash,
            }
            for item in result.agent_invocations
        ]

    roster_projection = audit_projection(roster_result)
    # Freeze invocation identity, including the runtime-bound Care receipt and
    # the reviewed Habit/Memory grounding instructions in the SkillLock and
    # the identifier-free provider Context projection.
    assert stable_hash(roster_projection) == (
        "435fed6856df4e2098fc4ecfa6866e1dfbffebda2c36c552409e4e626ec7f0b6"
    )


def test_live_episode_budget_allows_schema_correction_latency() -> None:
    for episode_type in EpisodeType:
        assert (
            EPISODE_DEFINITIONS[episode_type].budget.soft_deadline_seconds
            == 180
        )


def test_communication_gets_one_live_acceptance_repair_turn() -> None:
    model = CommunicationAcceptanceRepairScenarioModel(
        EpisodeType.MORNING_REVIEW
    )

    result = product_runner(model).run(request(EpisodeType.MORNING_REVIEW))

    assert result.receipt.status is EpisodeStatus.COMPLETE
    assert model.communication_calls == 2


def test_evidence_and_safety_get_one_provenance_repair_turn_each() -> None:
    model = ProvenanceAcceptanceRepairScenarioModel(
        EpisodeType.ROLE_MATERIAL
    )

    result = product_runner(model).run(
        request(
            EpisodeType.ROLE_MATERIAL,
            doctor_material=True,
            binding_role="doctor",
        )
    )

    assert result.receipt.status is EpisodeStatus.COMPLETE
    assert model.evidence_calls == 2
    assert model.safety_calls >= 2
    assert any(
        "fact_ref" in reason and "retrieval_handle" in reason
        for reason in model.evidence_revision_reasons
    )


def longitudinal_request(
    episode_id: str,
    *,
    source_ref: str = "night:1",
) -> ProductEpisodeRunRequest:
    now = datetime.now(timezone.utc)
    scope = SourceScope(
        kind=SourceScopeKind.CURRENT_NIGHT,
        as_of=now,
        timezone_name="UTC",
        date_start=now.date(),
        date_end=now.date(),
        valid_night_count=1,
    )
    fact_snapshot = FactSnapshot.create(
        fact_snapshot_id=f"snapshot:{episode_id}",
        binding=AuthenticatedBinding(
            actor_id="actor-1",
            subject_id="subject-1",
            role="elder",
            authorization_scope=(
                "read_sleep_data",
                "draft_material",
                "personal_memory:read",
            ),
        ),
        source_scope=scope,
        canonical_data_version=f"canonical:{episode_id}",
        source_refs=(source_ref,),
        created_at=now,
    )
    return ProductEpisodeRunRequest(
        episode_id=episode_id,
        episode_type=EpisodeType.MORNING_REVIEW,
        objective="完成带纵向记忆治理的结构化睡眠回顾",
        fact_snapshot=fact_snapshot,
        user_text="请结合当前授权信息进行睡眠回顾。",
        tool_inputs={
            "radar.get_night_evidence": {
                "data": {"sleep_duration_hours": 7.2},
                "source_refs": [source_ref],
            },
            "radar.assess_data_quality": {
                "coverage_ratio": 0.95,
                "source_refs": [source_ref],
            },
        },
        personalized=True,
    )


def test_morning_uses_sleepcare_evidence_sleepcare_without_fixed_safety() -> None:
    instance, _ = runner(EpisodeType.MORNING_REVIEW)
    result = instance.run(request(EpisodeType.MORNING_REVIEW))
    assert result.receipt.status == EpisodeStatus.COMPLETE
    assert [item.agent_id for item in result.envelopes] == [
        AgentId.EVIDENCE_REASONING,
        AgentId.SLEEP_CARE,
    ]
    assert AgentId.CARE_STRATEGY not in {
        item.agent_id for item in result.accepted_work_products
    }
    coordination = next(
        item
        for item in result.tool_receipts
        if item.tool_name == "coordination.read_policy"
    )
    assert coordination.output["routing"]["candidate_intents"] == []
    assert result.agent_invocations
    assert {
        item.invocation_id for item in result.agent_invocations
    } == set(result.receipt.agent_invocation_ids)
    assert all(item.provider == "test" for item in result.agent_invocations)


def test_deterministic_runtime_uses_shared_content_plan_and_omits_normal_care() -> None:
    class CapturingDeterministicModel(DeterministicReplayStructuredAgentModel):
        def __init__(self) -> None:
            super().__init__()
            self.schemas: list[type] = []

        def generate(self, **kwargs):
            self.schemas.append(kwargs["schema"])
            return super().generate(**kwargs)

    model = CapturingDeterministicModel()
    instance = build_deterministic_product_runtime_bundle(model=model).runner

    result = instance.run(request(EpisodeType.MORNING_REVIEW))

    assert result.receipt.status is EpisodeStatus.COMPLETE
    assert _SleepCareContentPlan in model.schemas
    assert SleepCareModelOutput not in model.schemas
    assert CareStrategyModelOutput not in model.schemas
    communication = next(
        item
        for item in result.envelopes
        if item.agent_id is AgentId.SLEEP_CARE
    ).output_payload
    assert isinstance(communication, CommunicationDraft)
    assert all(
        binding.rendered_text in communication.text
        for binding in communication.semantic_bindings
    )
    record = next(
        item
        for item in result.agent_invocations
        if item.agent_id is AgentId.SLEEP_CARE
        and item.schema_version == "SleepCareModelOutput.v1"
    )
    assert record.schema_version == "SleepCareModelOutput.v1"


def test_deterministic_urgent_path_has_zero_model_and_zero_care_calls() -> None:
    class CapturingDeterministicModel(DeterministicReplayStructuredAgentModel):
        def __init__(self) -> None:
            super().__init__()
            self.schemas: list[type] = []

        def generate(self, **kwargs):
            self.schemas.append(kwargs["schema"])
            return super().generate(**kwargs)

    model = CapturingDeterministicModel()
    instance = build_deterministic_product_runtime_bundle(model=model).runner

    result = instance.run(
        request(
            EpisodeType.MORNING_REVIEW,
            user_text="我现在胸痛并且呼吸困难",
        )
    )

    assert result.receipt.execution_mode is ExecutionMode.DETERMINISTIC_ONLY
    assert result.receipt.episode_type is EpisodeType.URGENT_BOUNDARY
    assert model.schemas == []
    assert result.agent_invocations == []
    assert not any(
        item.agent_id is AgentId.CARE_STRATEGY
        for item in result.accepted_work_products
    )


def test_trend_is_tool_inside_evidence_path() -> None:
    instance, _ = runner(EpisodeType.TREND_REVIEW)
    result = instance.run(request(EpisodeType.TREND_REVIEW))
    assert AgentId.EVIDENCE_REASONING in {
        item.agent_id for item in result.envelopes
    }
    assert "trend.calculate_metrics" in {
        item.tool_name for item in result.tool_receipts
    }
    assert result.receipt.terminal
    assert instance.tool_executor.runtime_binding_count(
        result.receipt.episode_id
    ) == 0
    assert len({item.agent_id for item in result.envelopes}) == 2


def test_general_knowledge_uses_only_sleepcare_agent() -> None:
    instance, _ = runner(EpisodeType.GROUNDED_DIALOGUE)
    result = instance.run(
        request(EpisodeType.GROUNDED_DIALOGUE, personalized=False)
    )
    assert result.receipt.status == EpisodeStatus.COMPLETE
    assert {item.agent_id for item in result.envelopes} == {
        AgentId.SLEEP_CARE
    }


def test_personalized_dialogue_adds_evidence() -> None:
    instance, _ = runner(EpisodeType.GROUNDED_DIALOGUE)
    result = instance.run(
        request(EpisodeType.GROUNDED_DIALOGUE, personalized=True)
    )
    assert {item.agent_id for item in result.envelopes} == {
        AgentId.SLEEP_CARE,
        AgentId.EVIDENCE_REASONING,
    }


def test_care_plan_uses_evidence_then_care_and_waits_confirmation() -> None:
    instance, _ = runner(EpisodeType.CARE_PLAN)
    result = instance.run(request(EpisodeType.CARE_PLAN))
    assert result.receipt.status == EpisodeStatus.WAITING_CONFIRMATION
    assert not result.receipt.terminal
    assert {item.agent_id for item in result.envelopes} == {
        AgentId.EVIDENCE_REASONING,
        AgentId.CARE_STRATEGY,
        AgentId.SLEEP_CARE,
    }


def test_low_confidence_evidence_forces_safety_even_if_plan_did_not() -> None:
    instance, _ = runner(
        EpisodeType.MORNING_REVIEW, low_confidence=True
    )
    result = instance.run(request(EpisodeType.MORNING_REVIEW))
    assert AgentId.SAFETY_REVIEW in {
        item.agent_id for item in result.envelopes
    }
    assert result.receipt.safety_decision_refs


def test_safety_revision_returns_to_responsible_agent_and_rechecks() -> None:
    instance, _ = runner(
        EpisodeType.MORNING_REVIEW,
        low_confidence=True,
        safety_verdicts=[SafetyVerdict.REVISE, SafetyVerdict.APPROVE],
    )
    result = instance.run(request(EpisodeType.MORNING_REVIEW))
    assert result.receipt.status == EpisodeStatus.COMPLETE
    assert [item.agent_id for item in result.envelopes].count(
        AgentId.EVIDENCE_REASONING
    ) == 2
    assert [item.agent_id for item in result.envelopes].count(
        AgentId.SAFETY_REVIEW
    ) == 2


def test_doctor_material_requires_safety_and_normalizes_implicit_audience() -> None:
    instance, model = runner(EpisodeType.ROLE_MATERIAL)
    result = instance.run(
        request(
            EpisodeType.ROLE_MATERIAL,
            doctor_material=True,
        )
    )
    assert AgentId.SAFETY_REVIEW in {
        item.agent_id for item in result.envelopes
    }
    safety = next(
        item.output_payload
        for item in result.envelopes
        if item.agent_id == AgentId.SAFETY_REVIEW
    )
    communication = next(
        item
        for item in result.accepted_work_products
        if item.agent_id == AgentId.SLEEP_CARE
    )
    assert safety.review_target_hash == communication.target_hash
    assert result.publication is not None
    assert result.publication.audience_role == "doctor"
    sleepcare_record = next(
        item
        for item in result.agent_invocations
        if item.agent_id is AgentId.SLEEP_CARE
        and item.skill_id == "draft_doctor_material"
    )
    assert sleepcare_record.skill_id == "draft_doctor_material"
    sleepcare_context = next(
        item
        for item in model.contexts
        if item["agent_id"] == AgentId.SLEEP_CARE.value
    )
    artifact_item = next(
        item
        for item in sleepcare_context["items"]
        if item["key"] == "tool:artifact.render"
    )
    assert artifact_item["value"]["audience_role"] == "doctor"
    assert result.receipt.status == EpisodeStatus.COMPLETE


def test_explicit_doctor_audience_cannot_bypass_doctor_safety_semantics() -> None:
    instance, _ = runner(EpisodeType.ROLE_MATERIAL)

    result = instance.run(
        request(EpisodeType.ROLE_MATERIAL, audience_role="doctor")
    )

    assert result.publication is not None
    assert result.publication.audience_role == "doctor"
    assert AgentId.SAFETY_REVIEW in {
        item.agent_id for item in result.envelopes
    }
    sleepcare_record = next(
        item
        for item in result.agent_invocations
        if item.agent_id is AgentId.SLEEP_CARE
        and item.skill_id == "draft_doctor_material"
    )
    assert sleepcare_record.skill_id == "draft_doctor_material"


def test_authenticated_doctor_binding_cannot_bypass_role_material_safety() -> None:
    instance, _ = runner(EpisodeType.ROLE_MATERIAL)

    result = instance.run(
        request(EpisodeType.ROLE_MATERIAL, binding_role="doctor")
    )

    assert result.publication is not None
    assert result.publication.audience_role == "doctor"
    assert AgentId.SAFETY_REVIEW in {
        item.agent_id for item in result.envelopes
    }
    assert any(
        item.agent_id is AgentId.SLEEP_CARE
        and item.skill_id == "draft_doctor_material"
        for item in result.agent_invocations
    )


def test_non_role_material_doctor_audience_requires_explicit_safety_semantics() -> None:
    with pytest.raises(ValueError, match="requires doctor_material"):
        request(EpisodeType.MORNING_REVIEW, audience_role="doctor")


def test_non_role_doctor_binding_requires_explicit_safety_semantics() -> None:
    with pytest.raises(ValueError, match="requires doctor_material"):
        request(EpisodeType.MORNING_REVIEW, binding_role="doctor")


def test_compatibility_doctor_material_uses_doctor_skill_and_safety() -> None:
    instance, _ = runner(EpisodeType.MORNING_REVIEW)

    result = instance.run(
        request(
            EpisodeType.MORNING_REVIEW,
            audience_role="doctor",
            doctor_material=True,
        )
    )

    assert result.receipt.status == EpisodeStatus.COMPLETE
    assert result.publication is not None
    assert result.publication.audience_role == "doctor"
    assert AgentId.SAFETY_REVIEW in {
        item.agent_id for item in result.envelopes
    }
    assert any(
        item.agent_id is AgentId.SLEEP_CARE
        and item.skill_id == "draft_doctor_material"
        for item in result.agent_invocations
    )


def test_doctor_material_rejects_explicit_non_doctor_audience() -> None:
    with pytest.raises(ValueError, match="cannot target"):
        request(
            EpisodeType.ROLE_MATERIAL,
            doctor_material=True,
            audience_role="family",
        )


def test_doctor_material_requires_draft_material_authorization() -> None:
    with pytest.raises(ValueError, match="draft_material authorization"):
        request(
            EpisodeType.ROLE_MATERIAL,
            audience_role="doctor",
            doctor_material=True,
            binding_authorization_scope=("read_sleep_data",),
        )


def test_elder_role_material_does_not_fixed_call_safety() -> None:
    instance, model = runner(EpisodeType.ROLE_MATERIAL)
    result = instance.run(request(EpisodeType.ROLE_MATERIAL))
    assert AgentId.SAFETY_REVIEW not in {
        item.agent_id for item in result.envelopes
    }
    assert result.receipt.status == EpisodeStatus.COMPLETE
    evidence = next(
        item
        for item in result.accepted_work_products
        if item.agent_id is AgentId.EVIDENCE_REASONING
    )
    artifact = next(
        item
        for item in result.tool_receipts
        if item.tool_name == "artifact.render"
    )
    assert artifact.output["schema_version"] == "product_artifact_basis.v1"
    assert artifact.output["accepted_evidence_ref"] == evidence.work_product_ref
    assert artifact.output["accepted_evidence_hash"] == evidence.target_hash
    assert artifact.output["basis_prepared"] is True
    assert artifact.output["rendered"] is False
    assert artifact.output["committed"] is False
    assert artifact.output["exported"] is False
    assert "compatibility_mode" not in artifact.output
    assert sum(
        item.tool_name == "artifact.render"
        for item in result.tool_receipts
    ) == 1
    sleepcare_context = next(
        item
        for item in model.contexts
        if item["agent_id"] == AgentId.SLEEP_CARE.value
    )
    sleepcare_artifact = next(
        item
        for item in sleepcare_context["items"]
        if item["key"] == "tool:artifact.render"
    )
    assert sleepcare_artifact["source_refs"][0] == (
        artifact.tool_invocation_id
    )
    assert "episode_id" not in sleepcare_artifact["value"]
    assert sleepcare_artifact["value"] == {
        key: value
        for key, value in artifact.output.items()
        if key != "episode_id"
    }


def test_role_material_basis_binds_safety_revised_evidence() -> None:
    instance, _ = runner(
        EpisodeType.ROLE_MATERIAL,
        low_confidence=True,
        safety_verdicts=[SafetyVerdict.REVISE, SafetyVerdict.APPROVE],
    )

    result = instance.run(request(EpisodeType.ROLE_MATERIAL))

    assert result.receipt.status == EpisodeStatus.COMPLETE
    evidence = next(
        item
        for item in result.accepted_work_products
        if item.agent_id is AgentId.EVIDENCE_REASONING
    )
    artifact = next(
        item
        for item in result.tool_receipts
        if item.tool_name == "artifact.render"
    )
    assert artifact.output["accepted_evidence_ref"] == evidence.work_product_ref
    assert artifact.output["accepted_evidence_hash"] == evidence.target_hash
    assert [item.agent_id for item in result.envelopes].count(
        AgentId.EVIDENCE_REASONING
    ) == 2


def test_required_safety_failure_blocks_role_material() -> None:
    instance, _ = runner(
        EpisodeType.ROLE_MATERIAL,
        fail_agent=AgentId.SAFETY_REVIEW,
    )
    result = instance.run(
        request(EpisodeType.ROLE_MATERIAL, doctor_material=True)
    )
    assert result.receipt.status == EpisodeStatus.BLOCKED
    assert result.publication is None


def test_doctor_artifact_failure_blocks_before_sleepcare_and_publication() -> None:
    instance, model = runner(EpisodeType.ROLE_MATERIAL)

    def unavailable(_arguments, _context):
        raise RuntimeError("artifact renderer unavailable")

    instance.tool_executor.register_handler("artifact.render", unavailable)
    result = instance.run(
        request(EpisodeType.ROLE_MATERIAL, doctor_material=True)
    )

    assert result.receipt.status == EpisodeStatus.BLOCKED
    assert result.publication is None
    assert not any(
        item.agent_id is AgentId.SLEEP_CARE
        and item.skill_id == "draft_doctor_material"
        for item in result.agent_invocations
    )
    assert SleepCareModelOutput.__name__ not in model.calls


def test_evidence_failure_degrades_without_care_takeover() -> None:
    instance, _ = runner(
        EpisodeType.CARE_PLAN,
        fail_agent=AgentId.EVIDENCE_REASONING,
    )
    result = instance.run(request(EpisodeType.CARE_PLAN))
    assert result.receipt.execution_mode == ExecutionMode.SAFE_DEGRADED
    assert AgentId.CARE_STRATEGY not in {
        item.agent_id for item in result.envelopes
    }


def test_urgent_preempts_all_model_agents() -> None:
    instance, model = runner(EpisodeType.MORNING_REVIEW)
    result = instance.run(
        request(
            EpisodeType.MORNING_REVIEW,
            user_text="我现在胸痛并且呼吸困难",
        )
    )
    assert result.receipt.execution_mode == ExecutionMode.DETERMINISTIC_ONLY
    assert result.receipt.episode_type == EpisodeType.URGENT_BOUNDARY
    assert not result.envelopes
    assert not model.calls
    assert not result.accepted_work_products
    assert not any(
        item.tool_name == "coordination.read_policy"
        for item in result.tool_receipts
    )


def test_failed_urgent_text_preflight_blocks_before_model_agents() -> None:
    instance, model = runner(EpisodeType.MORNING_REVIEW)

    def unavailable(arguments, context):
        raise RuntimeError("urgent boundary unavailable")

    instance.tool_executor.register_handler(
        "risk.match_urgent_boundary",
        unavailable,
    )

    result = instance.run(request(EpisodeType.MORNING_REVIEW))

    assert result.receipt.status is EpisodeStatus.BLOCKED
    assert result.receipt.execution_mode is ExecutionMode.SAFE_DEGRADED
    assert result.receipt.failure_codes == [
        "required_tool_failed:risk.match_urgent_boundary"
    ]
    assert model.calls == []
    assert result.tool_receipts[0].outcome is InvocationOutcome.FAILED


def test_authenticated_user_fact_urgent_answer_preempts_all_model_agents() -> None:
    instance, model = runner(EpisodeType.MORNING_REVIEW)
    run_request = request(EpisodeType.MORNING_REVIEW).model_copy(
        update={
            "user_fact_responses": (
                ProductUserFactResponse(
                    request_id="urgent-followup-answer",
                    answer="现在胸痛，而且呼吸困难。",
                    actor_id="actor-1",
                    actor_role="elder",
                    subject_id="subject-1",
                    observed_at=NOW,
                ),
            )
        }
    )

    result = instance.run(run_request)

    assert result.receipt.episode_type is EpisodeType.URGENT_BOUNDARY
    assert result.receipt.execution_mode is ExecutionMode.DETERMINISTIC_ONLY
    assert result.publication_delivered is True
    assert model.calls == []


def test_data_quality_recovery_is_deterministic_and_truthful() -> None:
    instance, model = runner(EpisodeType.DATA_QUALITY_RECOVERY)
    result = instance.run(request(EpisodeType.DATA_QUALITY_RECOVERY))
    assert result.receipt.execution_mode == ExecutionMode.DETERMINISTIC_ONLY
    assert result.receipt.status == EpisodeStatus.PARTIAL
    assert not result.envelopes
    assert not model.calls


def test_doctor_data_quality_recovery_preserves_agentless_deterministic_view() -> None:
    instance, model = runner(EpisodeType.DATA_QUALITY_RECOVERY)

    result = instance.run(
        request(
            EpisodeType.DATA_QUALITY_RECOVERY,
            audience_role="doctor",
            binding_role="doctor",
            doctor_material=True,
        )
    )

    assert result.receipt.execution_mode == ExecutionMode.DETERMINISTIC_ONLY
    assert result.receipt.status == EpisodeStatus.PARTIAL
    assert not result.envelopes
    assert not model.calls



def test_exact_revision_risk_escalate_routes_care_and_retains_result() -> None:
    instance, model = runner(EpisodeType.MORNING_REVIEW)
    run_request = request(EpisodeType.MORNING_REVIEW)
    tool_inputs = dict(run_request.tool_inputs)
    tool_inputs["risk.classify_signal"] = {
        "data": {
            "risk_state": "reviewed_signal",
            "data_sufficiency": "sufficient",
            "health_escalation_allowed": True,
            "reason_codes": ["approved_vendor_alert"],
        },
        "source_refs": list(run_request.fact_snapshot.source_refs),
    }

    result = instance.run(
        run_request.model_copy(update={"tool_inputs": tool_inputs})
    )

    risk_receipt = next(
        item
        for item in result.tool_receipts
        if item.tool_name == "risk.classify_signal"
    )
    assert risk_receipt.output["risk_level"] == "escalate"
    assert risk_receipt.output["safety_required"] is True
    coordination = next(
        item
        for item in result.tool_receipts
        if item.tool_name == "coordination.read_policy"
    )
    assert coordination.output["routing"]["candidate_intents"]
    assert _CareStrategyPlan.__name__ in model.calls
    assert SafetyReviewModelOutput.__name__ in model.calls
    care = next(
        item
        for item in result.accepted_work_products
        if item.agent_id == AgentId.CARE_STRATEGY
    )
    care_candidate = care.payload["primary_action"]["candidate_id"]
    assert result.publication is not None
    assert care_candidate in result.publication.care_candidate_refs
    assert any(
        item.agent_id == AgentId.SAFETY_REVIEW
        for item in result.accepted_work_products
    )


def test_longitudinal_watch_routes_care_without_forcing_safety() -> None:
    instance, model = runner(EpisodeType.MORNING_REVIEW)
    base = request(EpisodeType.MORNING_REVIEW)
    trend_refs = tuple(
        f"night_episode_revision:replay:{day}" for day in range(24, 27)
    )
    trend_snapshot = FactSnapshot.create(
        fact_snapshot_id="snapshot-morning-longitudinal-watch",
        binding=base.fact_snapshot.binding,
        source_scope=SourceScope(
            kind=SourceScopeKind.HISTORICAL_RANGE,
            as_of=NOW,
            timezone_name="Asia/Shanghai",
            date_start=date(2026, 7, 24),
            date_end=date(2026, 7, 26),
            valid_night_count=3,
        ),
        canonical_data_version=base.fact_snapshot.canonical_data_version,
        source_refs=(*base.fact_snapshot.source_refs, *trend_refs),
        created_at=NOW,
    )
    tool_inputs = dict(base.tool_inputs)
    tool_inputs["risk.classify_signal"] = {
        "data": {
            "risk_state": "no_reviewed_signal",
            "data_sufficiency": "sufficient",
            "health_escalation_allowed": False,
            "reason_codes": ["no_reviewed_signal_in_source_scope"],
        },
        "source_refs": list(trend_snapshot.source_refs),
        "trend_signals": [
            {
                "risk_level": "watch",
                "confidence": 0.75,
                "source_refs": list(trend_refs),
            }
        ],
        "trend_observation": {
            "quality_status": "good",
            "confidence_label": "normal",
            "health_conclusion_allowed": True,
            "source_refs": list(trend_refs),
        },
    }

    result = instance.run(
        base.model_copy(
            update={
                "fact_snapshot": trend_snapshot,
                "tool_inputs": tool_inputs,
            }
        )
    )

    risk_receipts = [
        item
        for item in result.tool_receipts
        if item.tool_name == "risk.classify_signal"
    ]
    assert [item.output["risk_level"] for item in risk_receipts] == [
        "normal",
        "watch",
    ]
    coordination = next(
        item
        for item in result.tool_receipts
        if item.tool_name == "coordination.read_policy"
    )
    assert coordination.output["routing"]["risk_level"] == "watch"
    assert coordination.output["routing"]["candidate_intents"]
    assert _CareStrategyPlan.__name__ in model.calls
    assert SafetyReviewModelOutput.__name__ not in model.calls
    assert any(
        item.agent_id == AgentId.CARE_STRATEGY
        for item in result.accepted_work_products
    )


def test_failed_required_evidence_tool_degrades_before_evidence_agent() -> None:
    instance, _ = runner(EpisodeType.MORNING_REVIEW)
    run_request = request(EpisodeType.MORNING_REVIEW)
    tool_inputs = dict(run_request.tool_inputs)
    tool_inputs["radar.get_night_evidence"] = {
        "data": {
            "schema_version": "product_revision_facts.v1",
            "subject_id": "another-subject",
            "canonical_data_version": (
                run_request.fact_snapshot.canonical_data_version
            ),
        },
        "source_refs": list(run_request.fact_snapshot.source_refs),
    }

    result = instance.run(
        run_request.model_copy(update={"tool_inputs": tool_inputs})
    )

    failed = next(
        item
        for item in result.tool_receipts
        if item.tool_name == "radar.get_night_evidence"
    )
    assert failed.outcome is InvocationOutcome.FAILED
    assert result.receipt.status is EpisodeStatus.PARTIAL
    assert result.receipt.goal_achieved is False
    assert "required_tool_failed:radar.get_night_evidence" in (
        result.receipt.failure_codes
    )
    assert not any(
        item.agent_id == AgentId.EVIDENCE_REASONING
        for item in result.accepted_work_products
    )


def _shared_analysis_request(*, partial: bool = False) -> SharedAnalysisRunRequest:
    base = request(
        EpisodeType.MORNING_REVIEW,
        user_text="",
        binding_role="system",
    )
    original = base.fact_snapshot
    revision_id = "night-revision-shared-1"
    shared_snapshot = FactSnapshot.create(
        fact_snapshot_id="snapshot-shared-analysis",
        binding=original.binding,
        source_scope=original.source_scope,
        canonical_data_version=original.canonical_data_version,
        care_context_version=original.care_context_version,
        memory_context_version=original.memory_context_version,
        source_refs=original.source_refs,
        created_at=NOW,
    )
    runtime_request = base.model_copy(
        update={
            "episode_id": "product-shared-analysis-1",
            "fact_snapshot": shared_snapshot,
        }
    )
    return SharedAnalysisRunRequest(
        source=SharedAnalysisSourceV1(
            night_episode_id="night-episode-shared-1",
            night_episode_revision_id=revision_id,
            night_episode_revision_number=1,
            wake_date=date(2026, 7, 26),
            observation_set_sha256=stable_hash(("observation-1",)),
            canonical_data_version=shared_snapshot.canonical_data_version,
            desired_analysis_sha256=stable_hash("desired-analysis"),
            consumed_context_sha256=stable_hash("consumed-context"),
            runtime_manifest_sha256=stable_hash("runtime-manifest"),
            data_sufficiency="partial" if partial else "sufficient",
            quality_state="partial" if partial else "good",
            risk_state="unknown" if partial else "no_reviewed_signal",
            quality_reason_codes=("coverage_limited",) if partial else (),
            limitations=("设备观察不能替代临床评估。",),
            partial_caveat=(
                "昨夜记录覆盖不完整，结论仅反映已观测时段。"
                if partial
                else None
            ),
        ),
        runtime_request=runtime_request,
    )


class _CapturingDeterministicModel(DeterministicReplayStructuredAgentModel):
    def __init__(self) -> None:
        super().__init__()
        self.schemas: list[type] = []
        self.message_payloads: list[str] = []

    def generate(self, **kwargs):
        self.schemas.append(kwargs["schema"])
        self.message_payloads.append(
            json.dumps(kwargs["messages"], ensure_ascii=False, sort_keys=True)
        )
        return super().generate(**kwargs)


def _selected_personalization(
    *,
    profile_version: int,
    memory_state_version: int,
    volatile_suffix: str,
    memory_value: str = "quiet_room",
) -> PinnedPersonalizationContext:
    habit = HabitFact(
        fact_id="habit-fact:stable-selected",
        fact_hash="a" * 64,
        revision=1,
        operation=HabitOperation.REMEMBER,
        subject_id="subject-1",
        concept_id="sleep.context.night_routine",
        concept_version="1.0.0",
        value="consistent bedtime",
        value_hash="b" * 64,
        confirmed_at=NOW,
        valid_until=NOW + timedelta(days=90),
        confirmation_ref="confirmation:habit:stable",
        change_id="habit-change:stable",
    )
    item_value_hash = stable_hash(
        {
            "concept_id": "sleep.context.environment",
            "value_schema_id": "enum.v1",
            "value_schema_version": "1",
            "typed_value": memory_value,
        }
    )
    item = MemorySliceItem(
        revision_ref="memory:environment:v1",
        concept_id="sleep.context.environment",
        value_schema_id="enum.v1",
        value_schema_version="1",
        typed_value=memory_value,
        value_hash=item_value_hash,
        provenance_type=ProvenanceType.ELDER_CONFIRMED,
        source_ref="user-report:environment",
        source_scope_kind=SourceScopeKind.HISTORICAL_RANGE,
        status=MemoryItemStatus.ACTIVE,
        valid_from=NOW - timedelta(days=30),
        valid_until=NOW + timedelta(days=30),
        trust_label=TrustLabel.USER_MEMORY_UNTRUSTED_DATA,
        verified_evidence=False,
        verified_medical_fact=False,
    )
    query_hash = stable_hash(f"query:{volatile_suffix}")
    result_hash = stable_hash(
        {
            "query_hash": query_hash,
            "items": [item.model_dump(mode="json")],
        }
    )
    handle = MemoryHandle(
        handle_id="mh_" + stable_hash(f"handle:{volatile_suffix}")[:48],
        subject_id="subject-1",
        actor_id="workload:worker",
        requesting_agent=AgentId.EVIDENCE_REASONING,
        purpose=MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT,
        invocation_id=f"invocation:{volatile_suffix}",
        query_id=f"memory-query:{volatile_suffix}",
        query_hash=query_hash,
        result_hash=result_hash,
        revision_ref=item.revision_ref,
        privacy_epoch=1,
        authorization_epoch=1,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
    )
    receipt = MemoryReadReceipt(
        receipt_id=(
            "memory-read:" + stable_hash(f"receipt:{volatile_suffix}")[:32]
        ),
        query_id=f"memory-query:{volatile_suffix}",
        query_hash=query_hash,
        invocation_id=f"invocation:{volatile_suffix}",
        subject_id="subject-1",
        requesting_agent=AgentId.EVIDENCE_REASONING,
        purpose=MemoryPurpose.PERSONAL_EVIDENCE_CONTEXT,
        result_hash=result_hash,
        items=(item,),
        handles=(handle,),
        candidate_count=1,
        filter_reason_codes=(),
        actual_tokens=12,
        privacy_epoch=1,
        authorization_epoch=1,
        completed_at=NOW + timedelta(minutes=len(volatile_suffix)),
    )
    profile_hash = stable_hash(
        {
            "subject_id": "subject-1",
            "profile_version": profile_version,
            "fact_hashes": [habit.fact_hash],
        }
    )
    return PinnedPersonalizationContext(
        subject_id="subject-1",
        habit_profile_version=profile_version,
        habit_profile_hash=profile_hash,
        habit_facts=(habit,),
        memory_state_version=memory_state_version,
        memory_read_receipts=(receipt,),
    )


def _stable_shared_request(
    personalization: PinnedPersonalizationContext,
) -> SharedAnalysisRunRequest:
    command = _shared_analysis_request()
    base = command.runtime_request
    selected_habit_hash = stable_hash(
        {
            "schema_version": "selected_habit_facts.v1",
            "facts": [
                {
                    "fact_id": personalization.habit_facts[0].fact_id,
                    "fact_hash": personalization.habit_facts[0].fact_hash,
                }
            ],
        }
    )
    snapshot = FactSnapshot.create(
        fact_snapshot_id="snapshot-selected-stable",
        binding=base.fact_snapshot.binding,
        source_scope=base.fact_snapshot.source_scope,
        canonical_data_version=base.fact_snapshot.canonical_data_version,
        habit_profile_version=1,
        habit_profile_hash=selected_habit_hash,
        memory_context_version=0,
        source_refs=(
            *base.fact_snapshot.source_refs,
            personalization.habit_facts[0].fact_id,
            "consumed-context:stable",
        ),
        created_at=NOW,
    )
    runtime_request = ProductEpisodeRunRequest(
        episode_id=base.episode_id,
        episode_type=base.episode_type,
        objective=base.objective,
        fact_snapshot=snapshot,
        tool_inputs=base.tool_inputs,
        personalized=True,
        personalization=personalization,
        personalization_projection_version="selected_stable.v1",
    )
    return command.model_copy(update={"runtime_request": runtime_request})


def _desired_identity_for_selected(
    personalization: PinnedPersonalizationContext,
) -> tuple[str, str]:
    consumed = _consumed_context_sha256(
        personalization,
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
    )
    scope = UowScope(
        namespace_id="replay:test",
        data_mode="replay",
        process_role="worker",
        purpose="worker",
        service_principal_id="worker-test",
        namespace_generation=1,
        run_id="run-test",
        arm_id="arm-test",
        subject_id="subject-1",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-test",
    )
    source = SimpleNamespace(
        night_episode_id="night-1",
        night_episode_revision_id="revision-1",
        night_episode_revision_number=1,
        observation_set_sha256=stable_hash("observations"),
        policy_versions={"quality": "v1", "risk": "v1"},
        facts=SimpleNamespace(
            canonical_data_version="canonical-v1",
            provider_quality_summary=lambda: {
                "quality_state": "good",
                "data_sufficiency": "sufficient",
            },
            provider_risk_summary=lambda: {
                "risk_state": "no_reviewed_signal",
                "health_escalation_allowed": False,
            },
        ),
    )
    return consumed, _desired_analysis_sha256(
        scope=scope,
        source=source,
        consumed_context_sha256=consumed,
        runtime_manifest_sha256=stable_hash("runtime-manifest"),
    )


def test_selected_context_ignores_volatile_l2_identity_in_every_provider_hash() -> None:
    first_personalization = _selected_personalization(
            profile_version=2,
            memory_state_version=3,
            volatile_suffix="first",
    )
    second_personalization = _selected_personalization(
            profile_version=200,
            memory_state_version=300,
            volatile_suffix="second",
    )
    first_request = _stable_shared_request(first_personalization)
    second_request = _stable_shared_request(second_personalization)
    first_model = _CapturingDeterministicModel()
    second_model = _CapturingDeterministicModel()
    first_runner = build_deterministic_product_runtime_bundle(
        model=first_model
    ).runner
    second_runner = build_deterministic_product_runtime_bundle(
        model=second_model
    ).runner

    first_result = first_runner.analyze_shared(first_request)
    second_result = second_runner.analyze_shared(second_request)

    assert product_episode_request_hash(
        first_request.runtime_request
    ) == product_episode_request_hash(second_request.runtime_request)
    assert _desired_identity_for_selected(
        first_personalization
    ) == _desired_identity_for_selected(second_personalization)
    assert first_model.message_payloads == second_model.message_payloads
    assert [item.context_hash for item in first_result.agent_invocations] == [
        item.context_hash for item in second_result.agent_invocations
    ]
    provider_input = "\n".join(first_model.message_payloads)
    assert "memory-read:" not in provider_input
    assert "memory-query:" not in provider_input
    assert "invocation:first" not in provider_input
    assert "habit-fact:stable-selected" not in provider_input
    assert "memory:environment:v1" not in provider_input


def test_relevant_selected_memory_changes_request_and_provider_hashes() -> None:
    first_personalization = _selected_personalization(
            profile_version=2,
            memory_state_version=3,
            volatile_suffix="first",
            memory_value="quiet_room",
    )
    changed_personalization = _selected_personalization(
            profile_version=2,
            memory_state_version=4,
            volatile_suffix="changed",
            memory_value="white_noise",
    )
    first_request = _stable_shared_request(first_personalization)
    changed_request = _stable_shared_request(changed_personalization)
    first_model = _CapturingDeterministicModel()
    changed_model = _CapturingDeterministicModel()

    build_deterministic_product_runtime_bundle(
        model=first_model
    ).runner.analyze_shared(first_request)
    build_deterministic_product_runtime_bundle(
        model=changed_model
    ).runner.analyze_shared(changed_request)

    assert product_episode_request_hash(
        first_request.runtime_request
    ) != product_episode_request_hash(changed_request.runtime_request)
    assert _desired_identity_for_selected(
        first_personalization
    ) != _desired_identity_for_selected(changed_personalization)
    assert first_model.message_payloads != changed_model.message_payloads


def test_shared_fact_snapshot_does_not_promote_memory_to_evidence_sources() -> None:
    personalization = _selected_personalization(
        profile_version=2,
        memory_state_version=3,
        volatile_suffix="evidence-boundary",
    )
    consumed, _desired = _desired_identity_for_selected(personalization)
    scope = UowScope(
        namespace_id="replay:test",
        data_mode="replay",
        process_role="worker",
        purpose="worker",
        service_principal_id="worker-test",
        namespace_generation=1,
        run_id="run-test",
        arm_id="arm-test",
        subject_id="subject-1",
        authorization_epoch=1,
        privacy_epoch=1,
        retrieval_policy_epoch=1,
        worker_instance="worker-test",
    )
    source = SimpleNamespace(
        subject_id="subject-1",
        episode=SimpleNamespace(
            wake_at=NOW,
            deterministic_close_deadline_at=NOW + timedelta(hours=1),
        ),
        facts=SimpleNamespace(
            data_mode=DataMode.REPLAY,
            local_sleep_date=NOW.date().isoformat(),
            timezone_name="Asia/Shanghai",
            longitudinal_risk_context=None,
            canonical_data_version="canonical-v1",
            deterministic_quality={"reason_codes": []},
            deterministic_risk={"reason_codes": []},
            agent_source_refs=lambda: ("canonical-night:sha256:safe",),
        ),
    )

    snapshot = _fact_snapshot_for_shared(
        scope=scope,
        source=source,
        fact_snapshot_id="snapshot-evidence-boundary",
        created_at=NOW + timedelta(days=1),
        consumed_context_sha256=consumed,
        personalization=personalization,
    )

    memory_item = personalization.memory_read_receipts[0].items[0]
    assert memory_item.revision_ref not in snapshot.source_refs
    assert personalization.memory_read_receipts[0].receipt_id not in (
        snapshot.source_refs
    )
    assert personalization.memory_read_receipts[0].handles[0].handle_id not in (
        snapshot.source_refs
    )
    assert personalization.habit_facts[0].fact_id in snapshot.source_refs
    assert snapshot.created_at == NOW


def test_shared_analysis_stops_before_communication_and_result_store() -> None:
    model = _CapturingDeterministicModel()
    instance = build_deterministic_product_runtime_bundle(model=model).runner
    command = _shared_analysis_request()

    shared = instance.analyze_shared(command)

    assert type(shared).model_validate(shared.model_dump(mode="json")) == shared
    assert shared.evidence.agent_id is AgentId.EVIDENCE_REASONING
    assert shared.doctor_projection_allowed is True
    assert shared.safety is not None
    assert shared.safety.agent_id is AgentId.SAFETY_REVIEW
    assert WorkProductKind.COMMUNICATION not in shared.accepted_products()
    assert SleepCareModelOutput not in model.schemas
    assert _SleepCareContentPlan not in model.schemas
    assert instance.result_store.history(command.episode_id) == []
    assert all(item.agent_id is not AgentId.SLEEP_CARE for item in shared.envelopes)
    provider_input = "\n".join(model.message_payloads)
    assert command.source.night_episode_id not in provider_input
    assert command.source.night_episode_revision_id not in provider_input


def test_shared_role_projections_are_deterministic_and_keep_partial_caveat() -> None:
    model = _CapturingDeterministicModel()
    instance = build_deterministic_product_runtime_bundle(model=model).runner
    shared = instance.analyze_shared(_shared_analysis_request(partial=True))

    first = build_shared_role_projections(shared)
    second = build_shared_role_projections(shared)

    assert first == second
    assert tuple(item.role for item in first) == (
        ReportRole.ELDER,
        ReportRole.FAMILY,
        ReportRole.DOCTOR,
    )
    assert {item.source_shared_analysis_sha256 for item in first} == {
        shared.shared_analysis_sha256
    }
    assert all(item.state is RoleProjectionState.READY for item in first)
    assert all(
        shared.source.partial_caveat in item.caveats
        and shared.source.partial_caveat in (item.text or "")
        for item in first
    )
    assert len({item.projection_sha256 for item in first}) == 3
    assert tuple(
        type(item).model_validate(item.model_dump(mode="json")) for item in first
    ) == first


def _policy_routed_care_shared_request() -> SharedAnalysisRunRequest:
    command = _shared_analysis_request()
    tool_inputs = dict(command.runtime_request.tool_inputs)
    refs = list(command.runtime_request.fact_snapshot.source_refs)
    tool_inputs["risk.classify_signal"] = {
        "data": {
            "risk_state": "no_reviewed_signal",
            "data_sufficiency": "sufficient",
            "health_escalation_allowed": False,
            "reason_codes": ["no_reviewed_signal_in_source_scope"],
        },
        "source_refs": refs,
        "trend_signals": [
            {
                "risk_level": "watch",
                "confidence": 0.75,
                "source_refs": refs,
            }
        ],
        "trend_observation": {
            "quality_status": "good",
            "confidence_label": "normal",
            "health_conclusion_allowed": True,
            "source_refs": refs,
        },
    }
    return command.model_copy(
        update={
            "runtime_request": command.runtime_request.model_copy(
                update={"tool_inputs": tool_inputs}
            )
        }
    )


def test_shared_analysis_runs_care_only_when_deterministic_policy_routes_it() -> None:
    instance, model = runner(EpisodeType.MORNING_REVIEW)

    shared = instance.analyze_shared(_policy_routed_care_shared_request())

    assert shared.care is not None
    strategy = CareStrategy.model_validate(shared.care.payload)
    assert strategy.primary_action is not None
    assert strategy.primary_action.title in shared.summary_lines
    assert model.calls.count(_CareStrategyPlan.__name__) == 1


def test_shared_safety_failure_blocks_doctor_without_losing_elder_family() -> None:
    instance, _ = runner(
        EpisodeType.MORNING_REVIEW,
        fail_agent=AgentId.SAFETY_REVIEW,
    )

    shared = instance.analyze_shared(_policy_routed_care_shared_request())
    elder, family, doctor = build_shared_role_projections(shared)

    assert shared.evidence is not None
    assert shared.care is not None
    assert shared.safety is None
    assert shared.doctor_failure_codes == ("DOCTOR_SAFETY_UNAVAILABLE",)
    assert elder.state is RoleProjectionState.READY
    assert family.state is RoleProjectionState.READY
    assert doctor.state is RoleProjectionState.POLICY_BLOCKED


def test_doctor_projection_blocks_without_approved_safety() -> None:
    instance, _ = runner(
        EpisodeType.MORNING_REVIEW,
        fail_agent=AgentId.SAFETY_REVIEW,
    )

    shared = instance.analyze_shared(_shared_analysis_request())
    elder, family, doctor = build_shared_role_projections(shared)

    assert shared.doctor_projection_allowed is False
    assert shared.safety is None
    assert shared.doctor_failure_codes
    assert elder.state is RoleProjectionState.READY
    assert family.state is RoleProjectionState.READY
    assert doctor.state is RoleProjectionState.POLICY_BLOCKED
    assert doctor.text is None


def test_doctor_projection_blocks_but_retains_accepted_nonapprove_safety() -> None:
    instance, _ = runner(
        EpisodeType.MORNING_REVIEW,
        safety_verdicts=[SafetyVerdict.BLOCK],
    )

    shared = instance.analyze_shared(_shared_analysis_request())
    elder, family, doctor = build_shared_role_projections(shared)

    assert shared.safety is not None
    decision = SafetyDecision.model_validate(shared.safety.payload)
    assert decision.verdict is SafetyVerdict.BLOCK
    assert shared.doctor_projection_allowed is False
    assert shared.doctor_failure_codes == ("DOCTOR_SAFETY_NOT_APPROVED",)
    assert elder.state is RoleProjectionState.READY
    assert family.state is RoleProjectionState.READY
    assert doctor.state is RoleProjectionState.POLICY_BLOCKED


def _elder_narrative_request(
    shared,
    *,
    episode_id: str = "product-elder-narrative-1",
) -> ElderNarrativeRequest:
    elder = build_shared_role_projections(shared)[0]
    runtime_request = _shared_analysis_request().runtime_request.model_copy(
        update={
            "episode_id": episode_id,
            "fact_snapshot": _shared_analysis_request().runtime_request.fact_snapshot,
            "audience_role": "elder",
        }
    )
    assert runtime_request.fact_snapshot.fact_snapshot_hash == shared.fact_snapshot_hash
    return ElderNarrativeRequest.create(
        runtime_request=runtime_request,
        shared_analysis=shared,
        elder_projection=elder,
        render_manifest_sha256=stable_hash("elder-render-manifest"),
    )


def test_elder_narrative_uses_exactly_one_content_plan_call() -> None:
    model = _CapturingDeterministicModel()
    instance = build_deterministic_product_runtime_bundle(model=model).runner
    shared = instance.analyze_shared(_shared_analysis_request())
    schemas_before = list(model.schemas)
    command = _elder_narrative_request(shared)

    narrative = instance.render_elder_narrative(command)

    narrative_schemas = model.schemas[len(schemas_before) :]
    assert narrative.state is ElderNarrativeState.READY
    assert type(narrative).model_validate(
        narrative.model_dump(mode="json")
    ) == narrative
    assert narrative.communication is not None
    assert narrative.text == narrative.communication.text
    assert narrative_schemas == [_SleepCareContentPlan]
    assert instance.result_store.history(command.episode_id) == []
    assert all(
        binding.rendered_text in narrative.text
        for binding in narrative.communication.semantic_bindings
    )


class _RequestingNarrativeModel(_CapturingDeterministicModel):
    def generate(self, **kwargs):
        if kwargs["schema"] is _SleepCareContentPlan:
            self.schemas.append(kwargs["schema"])
            return _SleepCareContentPlan(
                status=WorkProductStatus.NEEDS_INPUT,
                tool_requests=[
                    ToolRequest(
                        request_id="narrative-tool-request",
                        tool_name="knowledge.retrieve_reviewed",
                        arguments={"query": "more context"},
                    )
                ],
            )
        return super().generate(**kwargs)


def test_elder_narrative_rejects_requests_and_returns_exact_fallback() -> None:
    model = _RequestingNarrativeModel()
    instance = build_deterministic_product_runtime_bundle(model=model).runner
    shared = instance.analyze_shared(_shared_analysis_request())
    command = _elder_narrative_request(
        shared,
        episode_id="product-elder-narrative-fallback",
    )

    narrative = instance.render_elder_narrative(command)

    assert narrative.state is ElderNarrativeState.FALLBACK
    assert narrative.text == command.elder_projection.text
    assert narrative.communication is None
    assert narrative.invocation is not None
    assert narrative.failure_codes == (
        "ELDER_NARRATIVE_REQUEST_REJECTED",
    )
