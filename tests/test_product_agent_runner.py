from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone

import sleepagent.radar_agent.product_agent.runner as runner_module

from sleepagent.radar_agent.product_agent import (
    AcceptanceEvidenceKind,
    AcceptanceScenario,
    AgentId,
    AuthenticatedBinding,
    CareActionCandidate,
    CareStrategy,
    CareStrategyModelOutput,
    ClaimKind,
    CommunicationDraft,
    CommunicationSemanticBinding,
    ConfirmationToken,
    CurrentContextRisk,
    CrossAgentRequest,
    CrossAgentRequestType,
    DeterministicCommitController,
    DeploymentControlAttestation,
    EpisodePlanProposal,
    EpisodeStatus,
    EpisodeType,
    EvaluationDecision,
    EvidenceClaim,
    EvidencePacket,
    EvidenceReasoningModelOutput,
    EvidenceSemantic,
    EvidenceSourceKind,
    ExecutionMode,
    ExternalActionExecutionResult,
    ExternalActionTarget,
    FactSnapshot,
    HabitProfileConfirmation,
    InvocationOutcome,
    LongitudinalTrend,
    MemoryChangeCandidate,
    MultifactorSafetyInput,
    MultiSourceConsistency,
    OnlineDataQuality,
    OnlineEventType,
    OnlineReasoningEvent,
    OnlineRiskLevel,
    ProductEpisodeRunRequest,
    ProductEpisodeRunner,
    ProductUserFactResponse,
    SafetyDecision,
    SafetyReviewModelOutput,
    SafetyVerdict,
    SkillRegistry,
    SleepCareEvaluation,
    SleepCareModelOutput,
    SourceScope,
    SourceScopeKind,
    RelativeBaselineDeviation,
    ToolRequest,
    WorkProductKind,
    WorkProductStatus,
    build_unavailable_entry_decisions,
    default_agent_profiles,
    default_skill_packages,
    observation_from_runtime,
    stable_hash,
    snapshot_binding_material,
)
from sleepagent.radar_agent.product_agent.agents import ProductAgentFactory
from sleepagent.radar_agent.product_agent.registry import EPISODE_DEFINITIONS
from sleepagent.radar_agent.questionnaire import (
    HabitAnswerDisposition,
    HabitQuestionAnswer,
    HabitQuestionTrigger,
)


NOW = datetime(2026, 7, 26, 7, 0, tzinfo=timezone.utc)
VALID_UNTIL = datetime.now(timezone.utc) + timedelta(days=30)


def test_episode_request_cannot_inject_question_suppressions() -> None:
    assert "habit_suppressions" not in ProductEpisodeRunRequest.model_fields


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


def snapshot(
    kind: SourceScopeKind = SourceScopeKind.CURRENT_NIGHT,
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
            role="elder",
            authorization_scope=("read_sleep_data", "draft_material"),
        ),
        source_scope=scope,
        canonical_data_version="v1",
        source_refs=("night:1", "range:1"),
        created_at=NOW,
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
                    packet_id=f"evidence:{context['episode_id']}",
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
                    strategy_id=f"care:{context['episode_id']}",
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
                    draft_id=f"draft:{context['episode_id']}",
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
                            "reviewed": True,
                            "passages": ["睡眠解释需结合个人数据质量。"],
                            "citations": ["knowledge:reviewed:1"],
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
                        packet_id=f"evidence:{context['episode_id']}",
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
        if not context["episode_id"].endswith(":query"):
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
                    packet_id=f"memory-evidence:{context['episode_id']}",
                    source_scope=scope,
                    claims=[
                        EvidenceClaim(
                            claim_id=f"memory-claim:{context['episode_id']}",
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
    skill_registry = SkillRegistry(default_skill_packages())
    agent_roster = ProductAgentFactory.create(
        sleepcare_model=model,
        evidence_reasoning_model=model,
        care_strategy_model=model,
        safety_review_model=model,
        skill_registry=skill_registry,
    )
    return (
        ProductEpisodeRunner(
            agent_roster=agent_roster,
            skill_registry=skill_registry,
        ),
        model,
    )


def request(
    episode_type: EpisodeType,
    *,
    personalized=True,
    doctor_material=False,
    external_action=False,
    user_text="昨晚睡得怎么样？",
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
        "care.read_feedback": {},
        "artifact.render": {"content": "draft"},
    }
    return ProductEpisodeRunRequest(
        episode_id=f"episode-{episode_type.value}",
        episode_type=episode_type,
        objective="完成当前睡眠照护任务",
        fact_snapshot=snapshot(kind),
        user_text=user_text,
        tool_inputs=inputs,
        personalized=personalized,
        doctor_material=doctor_material,
        external_action=external_action,
        **extra,
    )


def test_concrete_roster_preserves_phase1_audit_identity_golden() -> None:
    roster_runner, _ = runner(EpisodeType.MORNING_REVIEW)
    legacy_model = ScenarioModel(EpisodeType.MORNING_REVIEW)
    legacy_runner = ProductEpisodeRunner(
        sleepcare_model=legacy_model,
        agent_models={item: legacy_model for item in AgentId},
    )
    episode_request = request(EpisodeType.MORNING_REVIEW)

    roster_result = roster_runner.run(episode_request)
    legacy_result = legacy_runner.run(episode_request)

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
    assert roster_projection == audit_projection(legacy_result)
    # Frozen from local Phase 1 commit 241d9be for this exact Episode input.
    assert stable_hash(roster_projection) == (
        "00329051d0dc6b633b2546a47b7616d8cd57ddc6062cf1898c79f3acf3494bfd"
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


def test_memory_trust_labels_are_allowed_only_for_authorized_agents() -> None:
    profiles = default_agent_profiles()

    assert "episodic_hint_untrusted" in profiles[
        AgentId.EVIDENCE_REASONING
    ].allowed_context_labels
    assert "user_memory_untrusted_data" in profiles[
        AgentId.EVIDENCE_REASONING
    ].allowed_context_labels
    assert "user_memory_untrusted_data" in profiles[
        AgentId.SLEEP_CARE
    ].allowed_context_labels
    assert "episodic_hint_untrusted" not in profiles[
        AgentId.SLEEP_CARE
    ].allowed_context_labels
    for agent_id in (AgentId.CARE_STRATEGY, AgentId.SAFETY_REVIEW):
        assert "episodic_hint_untrusted" not in profiles[
            agent_id
        ].allowed_context_labels
        assert "user_memory_untrusted_data" not in profiles[
            agent_id
        ].allowed_context_labels


def test_confirmed_memory_slice_supports_a_structured_evidence_episode() -> None:
    model = LongitudinalMemoryScenarioModel(
        EpisodeType.MORNING_REVIEW,
        memory_type="governed_memory",
        concept_id="sleep.preferred_wake_time",
    )
    instance = ProductEpisodeRunner(
        sleepcare_model=model,
        agent_models={item: model for item in AgentId},
    )
    episode_request = longitudinal_request("episode:governed-memory:query")
    candidate = MemoryChangeCandidate(
        candidate_id="memory:wake-time",
        operation="create",
        subject_id="subject-1",
        memory_type="routine",
        concept_id="sleep.preferred_wake_time",
        value_schema_id="bounded_string.v1",
        typed_value="07:00",
        provenance_type="elder_confirmed",
        source_ref="user_report:wake-time",
        sensitivity_class="personal",
        allowed_roles=(AgentId.EVIDENCE_REASONING, AgentId.SLEEP_CARE),
        allowed_purposes=(
            "personal_evidence_context",
            "explicit_memory_review",
            "explicit_memory_change",
            "explicit_memory_forget",
        ),
        explicit_user_authorization=True,
        confirmation_required=True,
    )
    instance.commit_controller.memory_store.apply(
        candidate,
        expected_version=0,
        confirmed=True,
        fact_snapshot=episode_request.fact_snapshot,
        confirmation_ref="confirmation:wake-time",
    )

    result = instance.run(episode_request)

    assert (
        result.receipt.status == EpisodeStatus.COMPLETE
    ), result.receipt.failure_codes
    evidence = next(
        item
        for item in result.accepted_work_products
        if item.agent_id == AgentId.EVIDENCE_REASONING
    )
    assert evidence.payload["claims"][0]["source_kind"] == "confirmed_memory"
    memory_receipt = next(
        item for item in result.tool_receipts if item.tool_name == "memory.read"
    )
    assert (
        memory_receipt.output["items"][0]["allowed_current_use"]
        == "confirmed_memory"
    )


def test_terminal_episode_scheduler_and_digest_revalidation_end_to_end() -> None:
    resolver_calls: list[tuple[str, ...]] = []

    def accepted_ledger_resolver(source_refs, query):
        resolver_calls.append(source_refs)
        assert query.subject_id == "subject-1"
        return {
            "source_class": "accepted_ledger",
            "typed_result": {
                "availability": "current",
                "accepted_work_product_ref": source_refs[0],
            },
            "observed_at": query.as_of.isoformat(),
        }

    model = LongitudinalMemoryScenarioModel(
        EpisodeType.MORNING_REVIEW,
        memory_type="episode_digest",
        concept_id="sleep.last_night",
    )
    instance = ProductEpisodeRunner(
        sleepcare_model=model,
        agent_models={item: model for item in AgentId},
        source_resolvers={"accepted_ledger": accepted_ledger_resolver},
    )
    scheduler = runner_module.ProductInductionScheduler(
        lambda: instance,
        interval_seconds=0.01,
        batch_limit=10,
    )
    scheduler.start()
    try:
        source_result = instance.run(
            longitudinal_request("episode:longitudinal:source")
        )
        assert (
            source_result.receipt.status == EpisodeStatus.COMPLETE
        ), source_result.receipt.failure_codes
        deadline = time.monotonic() + 2
        while (
            not instance.result_store.list_active_digests(
                "subject-1",
                as_of=datetime.now(timezone.utc),
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
    finally:
        scheduler.stop()

    assert instance.result_store.list_active_digests(
        "subject-1",
        as_of=datetime.now(timezone.utc),
    )
    assert instance.result_store.digest_read_enabled() is False

    # This attestation only opens the in-memory test route; production remains off.
    instance.result_store.enable_digest_read(
        DeploymentControlAttestation(
            attestation_id="attestation:test-only",
            manifest_encryption_verified=True,
            backup_crypto_expiry_verified=True,
            worker_least_privilege_verified=True,
            publication_journal_verified=True,
            writer_fencing_verified=True,
            orphan_terminal_count=0,
            benchmark_gate_passed=True,
            attested_at=datetime.now(timezone.utc),
        )
    )
    query_result = instance.run(
        longitudinal_request("episode:longitudinal:query")
    )

    assert query_result.receipt.status == EpisodeStatus.COMPLETE
    assert resolver_calls
    assert resolver_calls[0][0].startswith("evidence_reasoning:")
    assert any(
        item.tool_name == "memory.resolve_source"
        and item.outcome == InvocationOutcome.SUCCEEDED
        for item in query_result.tool_receipts
    )
    evidence = next(
        item
        for item in query_result.accepted_work_products
        if item.agent_id == AgentId.EVIDENCE_REASONING
    )
    assert evidence.payload["claims"][0]["source_kind"] == "canonical_observation"


def test_morning_uses_sleepcare_evidence_sleepcare_without_fixed_safety() -> None:
    instance, _ = runner(EpisodeType.MORNING_REVIEW)
    result = instance.run(request(EpisodeType.MORNING_REVIEW))
    assert result.receipt.status == EpisodeStatus.COMPLETE
    assert [item.agent_id for item in result.envelopes] == [
        AgentId.EVIDENCE_REASONING,
        AgentId.SLEEP_CARE,
    ]
    assert result.agent_invocations
    assert {
        item.invocation_id for item in result.agent_invocations
    } == set(result.receipt.agent_invocation_ids)
    assert all(item.provider == "test" for item in result.agent_invocations)


def test_runtime_observation_binds_unique_real_provider_receipts() -> None:
    model = ProviderReceiptScenarioModel(EpisodeType.MORNING_REVIEW)
    instance = ProductEpisodeRunner(
        sleepcare_model=model,
        agent_models={item: model for item in AgentId},
    )
    result = instance.run(request(EpisodeType.MORNING_REVIEW))
    observation = observation_from_runtime(
        result,
        scenario=AcceptanceScenario.NORMAL_MORNING,
        repetition=1,
        role="elder",
        evidence_kind=AcceptanceEvidenceKind.REAL,
        real_provider=True,
        domain_reviewed=False,
    )

    assert observation.provider_receipt is not None
    assert observation.provider_receipt.receipt_ref == result.receipt.trace_ref
    assert len(observation.provider_receipt.provider_request_ids) == len(
        result.agent_invocations
    )
    assert observation.runtime_receipt is not None
    assert observation.runtime_receipt.trace_ref == result.receipt.trace_ref
    assert observation.runtime_receipt.result_hash == stable_hash(
        result.model_dump(mode="json")
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


def test_doctor_material_requires_safety() -> None:
    instance, _ = runner(EpisodeType.ROLE_MATERIAL)
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
    assert result.receipt.status == EpisodeStatus.COMPLETE


def test_elder_role_material_does_not_fixed_call_safety() -> None:
    instance, _ = runner(EpisodeType.ROLE_MATERIAL)
    result = instance.run(request(EpisodeType.ROLE_MATERIAL))
    assert AgentId.SAFETY_REVIEW not in {
        item.agent_id for item in result.envelopes
    }
    assert result.receipt.status == EpisodeStatus.COMPLETE


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


def test_data_quality_recovery_is_deterministic_and_truthful() -> None:
    instance, model = runner(EpisodeType.DATA_QUALITY_RECOVERY)
    result = instance.run(request(EpisodeType.DATA_QUALITY_RECOVERY))
    assert result.receipt.execution_mode == ExecutionMode.DETERMINISTIC_ONLY
    assert result.receipt.status == EpisodeStatus.PARTIAL
    assert not result.envelopes
    assert not model.calls


def test_normal_morning_does_not_ask_to_complete_habit_profile() -> None:
    instance, _ = runner(EpisodeType.MORNING_REVIEW)
    result = instance.run(request(EpisodeType.MORNING_REVIEW))
    assert result.receipt.status == EpisodeStatus.COMPLETE
    assert result.habit_selection is None
    assert "questionnaire.select_profile" not in {
        item.tool_name for item in result.tool_receipts
    }


def test_optional_habit_intake_is_plan_bound_and_does_not_block_answer() -> None:
    instance, _ = runner(EpisodeType.MORNING_REVIEW)
    result = instance.run(
        request(
            EpisodeType.MORNING_REVIEW,
            habit_question_trigger=HabitQuestionTrigger.OPTIONAL_LIGHT_INTAKE,
            habit_candidate_concept_ids=(
                "habit.primary_goal",
                "habit.schedule_constraint",
            ),
            habit_question_max=2,
        )
    )
    assert result.receipt.status == EpisodeStatus.COMPLETE
    assert result.publication_delivered
    assert result.habit_selection is not None
    assert len(result.habit_selection.candidates) == 2
    assert result.habit_selection.plan_id.startswith("plan:")


def test_skipping_all_habit_questions_still_delivers_morning_answer() -> None:
    instance, _ = runner(EpisodeType.MORNING_REVIEW)
    offered = instance.run(
        request(
            EpisodeType.MORNING_REVIEW,
            habit_question_trigger=HabitQuestionTrigger.OPTIONAL_LIGHT_INTAKE,
            habit_candidate_concept_ids=("habit.primary_goal",),
            habit_question_max=1,
        )
    )
    selection = offered.habit_selection
    assert selection is not None
    skipped = instance.run(
        request(
            EpisodeType.MORNING_REVIEW,
            habit_selection=selection,
            habit_answers=(
                HabitQuestionAnswer(
                    concept_id=selection.candidates[0].concept_id,
                    concept_version=selection.candidates[0].concept_version,
                    disposition=HabitAnswerDisposition.SKIPPED,
                ),
            ),
        )
    )
    assert skipped.receipt.status == EpisodeStatus.COMPLETE
    assert skipped.publication_delivered
    assert skipped.habit_capture is not None
    assert not skipped.habit_capture.answers[0].profile_candidate_eligible


def test_answer_builds_pending_atomic_change_set_without_writing_memory() -> None:
    instance, _ = runner(EpisodeType.MORNING_REVIEW)
    offered = instance.run(
        request(
            EpisodeType.MORNING_REVIEW,
            habit_question_trigger=HabitQuestionTrigger.EXPLICIT_HABIT_QUESTION,
            habit_candidate_concept_ids=("habit.nap_pattern",),
            habit_question_max=1,
        )
    )
    selection = offered.habit_selection
    assert selection is not None
    answered = instance.run(
        request(
            EpisodeType.MORNING_REVIEW,
            habit_selection=selection,
            habit_answers=(
                HabitQuestionAnswer(
                    concept_id="habit.nap_pattern",
                    concept_version="1.0.0",
                    disposition=HabitAnswerDisposition.ANSWERED,
                    value="偶尔午睡",
                ),
            ),
        )
    )
    assert answered.publication_delivered
    assert answered.receipt.status == EpisodeStatus.WAITING_CONFIRMATION
    assert answered.habit_change_set is not None
    assert len(answered.habit_change_set.candidates) == 1
    assert instance.habit_runtime.store.get("subject-1").facts == ()


def test_habit_response_safety_signal_preempts_remaining_agent_path() -> None:
    instance, model = runner(EpisodeType.MORNING_REVIEW)
    offered = instance.run(
        request(
            EpisodeType.MORNING_REVIEW,
            habit_question_trigger=HabitQuestionTrigger.EXPLICIT_HABIT_QUESTION,
            habit_candidate_concept_ids=("habit.observed_snoring",),
            habit_question_max=1,
        )
    )
    selection = offered.habit_selection
    assert selection is not None
    calls_before = len(model.calls)
    result = instance.run(
        request(
            EpisodeType.MORNING_REVIEW,
            habit_selection=selection,
            habit_answers=(
                HabitQuestionAnswer(
                    concept_id="habit.observed_snoring",
                    concept_version="1.0.0",
                    disposition=HabitAnswerDisposition.ANSWERED,
                    value="观察到",
                ),
            ),
        )
    )
    assert result.receipt.episode_type == EpisodeType.URGENT_BOUNDARY
    assert result.receipt.execution_mode == ExecutionMode.DETERMINISTIC_ONLY
    assert result.habit_capture is not None
    assert result.habit_capture.stop_remaining_questions
    assert len(model.calls) == calls_before


def test_profile_tool_context_is_user_data_and_not_disclosed_to_care() -> None:
    instance, model = runner(EpisodeType.CARE_PLAN)
    result = instance.run(
        request(
            EpisodeType.CARE_PLAN,
            profile_purpose="care",
            profile_relevant_concept_ids=("habit.schedule_constraint",),
        )
    )
    assert result.receipt.status == EpisodeStatus.WAITING_CONFIRMATION
    contexts = {
        AgentId(item["agent_id"]): item
        for item in model.contexts
        if item["agent_id"] in {member.value for member in AgentId}
    }
    evidence_items = contexts[AgentId.EVIDENCE_REASONING]["items"]
    assert any(
        item["key"] == "tool_receipt:profile.read"
        and item["trust_label"] == "user_data"
        for item in evidence_items
    )
    assert all(
        item["key"] != "tool_receipt:profile.read"
        for item in contexts[AgentId.CARE_STRATEGY]["items"]
    )


def test_unavailable_profile_service_degrades_without_blocking_core_answer() -> None:
    instance, _ = runner(EpisodeType.MORNING_REVIEW)

    def unavailable(arguments, context):
        raise TimeoutError("profile unavailable")

    instance.tool_executor.register_handler("profile.read", unavailable)
    result = instance.run(
        request(
            EpisodeType.MORNING_REVIEW,
            profile_purpose="evidence",
            profile_relevant_concept_ids=("habit.nap_pattern",),
        )
    )
    profile_receipt = next(
        item for item in result.tool_receipts if item.tool_name == "profile.read"
    )
    assert profile_receipt.outcome == InvocationOutcome.FAILED
    assert result.receipt.status == EpisodeStatus.COMPLETE
    assert result.publication_delivered


def test_paired_replay_changes_only_relevant_profile_evidence() -> None:
    seed_runner, _ = runner(EpisodeType.MORNING_REVIEW)
    offered = seed_runner.run(
        request(
            EpisodeType.MORNING_REVIEW,
            habit_question_trigger=HabitQuestionTrigger.EXPLICIT_HABIT_QUESTION,
            habit_candidate_concept_ids=("habit.nap_pattern",),
            habit_question_max=1,
        )
    )
    selection = offered.habit_selection
    assert selection is not None
    pending = seed_runner.run(
        request(
            EpisodeType.MORNING_REVIEW,
            habit_selection=selection,
            habit_answers=(
                HabitQuestionAnswer(
                    concept_id="habit.nap_pattern",
                    concept_version="1.0.0",
                    disposition=HabitAnswerDisposition.ANSWERED,
                    value="偶尔午睡",
                ),
            ),
        )
    )
    changes = pending.habit_change_set
    assert changes is not None
    confirmed_at = datetime.now(timezone.utc)
    base_snapshot = snapshot()
    controller = DeterministicCommitController(
        habit_profile_store=seed_runner.habit_runtime.store
    )
    commit = controller.commit_habit_profile(
        change_set=changes,
        confirmation=HabitProfileConfirmation(
            confirmation_id="paired-confirmation",
            actor_id="actor-1",
            actor_role="elder",
            subject_id="subject-1",
            action_scope="write_habit_profile",
            change_set_id=changes.change_set_id,
            change_set_version=changes.version,
            manifest_hash=changes.manifest_hash,
            expires_at=confirmed_at + timedelta(minutes=20),
        ),
        fact_snapshot=base_snapshot,
        idempotency_key="paired-profile-commit",
        now=confirmed_at,
    )
    assert commit.outcome == InvocationOutcome.SUCCEEDED
    versioned_snapshot = FactSnapshot.create(
        fact_snapshot_id="snapshot-profile-v1",
        binding=base_snapshot.binding,
        source_scope=base_snapshot.source_scope,
        canonical_data_version=base_snapshot.canonical_data_version,
        entry_ledger_version=base_snapshot.entry_ledger_version,
        care_context_version=base_snapshot.care_context_version,
        memory_context_version=1,
        source_refs=base_snapshot.source_refs,
        created_at=base_snapshot.created_at,
    )
    model = ProfileAwareScenarioModel(EpisodeType.MORNING_REVIEW)
    paired_runner = ProductEpisodeRunner(
        sleepcare_model=model,
        agent_models={item: model for item in AgentId},
        habit_runtime=seed_runner.habit_runtime,
    )

    no_profile_request = request(EpisodeType.MORNING_REVIEW).model_copy(
        update={
            "episode_id": "paired-no-profile",
            "fact_snapshot": versioned_snapshot,
        }
    )
    no_profile = paired_runner.run(no_profile_request)
    relevant = paired_runner.run(
        no_profile_request.model_copy(
            update={
                "episode_id": "paired-relevant-profile",
                "profile_purpose": "evidence",
                "profile_relevant_concept_ids": ("habit.nap_pattern",),
            }
        )
    )
    unrelated = paired_runner.run(
        no_profile_request.model_copy(
            update={
                "episode_id": "paired-unrelated-profile",
                "profile_purpose": "evidence",
                "profile_relevant_concept_ids": (
                    "habit.environment_preference",
                ),
            }
        )
    )

    def statement(result):
        evidence = next(
            item
            for item in result.accepted_work_products
            if item.agent_id == AgentId.EVIDENCE_REASONING
        )
        return evidence.payload["claims"][0]["statement"]

    assert statement(no_profile) == statement(unrelated)
    assert statement(relevant) == "habit.nap_pattern=偶尔午睡"
    assert "导致" not in statement(relevant)


def test_family_observation_requires_later_elder_owned_change_set() -> None:
    instance, _ = runner(EpisodeType.MORNING_REVIEW)
    elder_snapshot = snapshot()
    family_snapshot = FactSnapshot.create(
        fact_snapshot_id="snapshot-family-observation",
        binding=AuthenticatedBinding(
            actor_id="family-1",
            subject_id="subject-1",
            role="family",
            authorization_scope=("report_observation",),
        ),
        source_scope=elder_snapshot.source_scope,
        canonical_data_version=elder_snapshot.canonical_data_version,
        source_refs=elder_snapshot.source_refs,
        created_at=elder_snapshot.created_at,
    )
    family_offer_request = request(EpisodeType.MORNING_REVIEW).model_copy(
        update={
            "episode_id": "family-observation",
            "fact_snapshot": family_snapshot,
            "habit_question_trigger": (
                HabitQuestionTrigger.EXPLICIT_HABIT_QUESTION
            ),
            "habit_candidate_concept_ids": ("habit.nap_pattern",),
            "habit_question_max": 1,
        }
    )
    offered = instance.run(family_offer_request)
    selection = offered.habit_selection
    assert selection is not None
    current_only = instance.run(
        family_offer_request.model_copy(
            update={
                "habit_question_trigger": None,
                "habit_candidate_concept_ids": (),
                "habit_selection": selection,
                "habit_answers": (
                    HabitQuestionAnswer(
                        concept_id="habit.nap_pattern",
                        concept_version="1.0.0",
                        disposition=HabitAnswerDisposition.ANSWERED,
                        value="多数天午睡",
                    ),
                ),
            }
        )
    )
    assert current_only.habit_capture is not None
    assert current_only.habit_change_set is None
    assert current_only.receipt.status == EpisodeStatus.COMPLETE

    elder_review = instance.run(
        request(EpisodeType.MORNING_REVIEW).model_copy(
            update={
                "episode_id": "elder-reviews-family-observation",
                "habit_profile_candidate_answers": (
                    current_only.habit_capture.answers[0],
                ),
                "habit_profile_update_requested": True,
            }
        )
    )
    changes = elder_review.habit_change_set
    assert changes is not None
    assert elder_review.receipt.status == EpisodeStatus.WAITING_CONFIRMATION
    assert changes.candidates[0].origin_semantic == "family_observation"

    committed_at = datetime.now(timezone.utc)
    commit = DeterministicCommitController(
        habit_profile_store=instance.habit_runtime.store
    ).commit_habit_profile(
        change_set=changes,
        confirmation=HabitProfileConfirmation(
            confirmation_id="elder-confirms-family-observation",
            actor_id="actor-1",
            actor_role="elder",
            subject_id="subject-1",
            action_scope="write_habit_profile",
            change_set_id=changes.change_set_id,
            change_set_version=changes.version,
            manifest_hash=changes.manifest_hash,
            expires_at=committed_at + timedelta(minutes=20),
        ),
        fact_snapshot=elder_snapshot,
        idempotency_key="family-observation-elder-commit",
        now=committed_at,
    )
    assert commit.outcome == InvocationOutcome.SUCCEEDED
    assert (
        instance.habit_runtime.store.get("subject-1").facts[0].origin_semantic
        == "family_observation"
    )


def test_agent_tool_request_executes_receipt_and_reinvokes_with_feedback() -> None:
    model = ToolFeedbackScenarioModel(EpisodeType.MORNING_REVIEW)
    instance = ProductEpisodeRunner(
        sleepcare_model=model,
        agent_models={item: model for item in AgentId},
    )

    result = instance.run(request(EpisodeType.MORNING_REVIEW))

    assert result.receipt.status == EpisodeStatus.COMPLETE
    evidence_invocations = [
        item
        for item in result.agent_invocations
        if item.agent_id == AgentId.EVIDENCE_REASONING
    ]
    assert len(evidence_invocations) == 2
    feedback_receipt = next(
        item
        for item in result.tool_receipts
        if item.tool_name == "knowledge.retrieve_reviewed"
    )
    assert feedback_receipt.caller == AgentId.EVIDENCE_REASONING.value
    evidence_contexts = [
        item
        for item in model.contexts
        if item["agent_id"] == AgentId.EVIDENCE_REASONING.value
    ]
    assert any(
        context_item["key"] == "tool:knowledge.retrieve_reviewed"
        for context_item in evidence_contexts[-1]["items"]
    )


def test_care_evidence_request_is_centrally_routed_and_bounded() -> None:
    model = CollaborationFeedbackScenarioModel(EpisodeType.CARE_PLAN)
    instance = ProductEpisodeRunner(
        sleepcare_model=model,
        agent_models={item: model for item in AgentId},
    )

    result = instance.run(request(EpisodeType.CARE_PLAN))

    assert result.receipt.status == EpisodeStatus.WAITING_CONFIRMATION
    assert len(
        [
            item
            for item in result.agent_invocations
            if item.agent_id == AgentId.EVIDENCE_REASONING
        ]
    ) == 2
    assert len(
        [
            item
            for item in result.agent_invocations
            if item.agent_id == AgentId.CARE_STRATEGY
        ]
    ) == 2
    assert model.care_rounds == 2


def test_user_fact_request_waits_with_exact_request_and_resumes_with_feedback() -> None:
    model = UserFactFeedbackScenarioModel(EpisodeType.MORNING_REVIEW)
    instance = ProductEpisodeRunner(
        sleepcare_model=model,
        agent_models={item: model for item in AgentId},
    )
    initial = request(EpisodeType.MORNING_REVIEW)

    first = instance.run(initial)

    assert first.receipt.status == EpisodeStatus.WAITING_USER
    assert first.pending_user_input is not None
    assert first.pending_user_input.request_id == "user-fact-bedtime"
    assert first.pending_user_input.source_agent == AgentId.EVIDENCE_REASONING

    resumed = instance.run(
        initial.model_copy(
            update={
                "user_fact_responses": (
                    ProductUserFactResponse(
                        request_id=first.pending_user_input.request_id,
                        answer="是，比平时晚约一小时。",
                        actor_id="actor-1",
                        actor_role="elder",
                        subject_id="subject-1",
                        observed_at=NOW,
                    ),
                )
            }
        )
    )

    assert resumed.receipt.status == EpisodeStatus.COMPLETE
    assert resumed.pending_user_input is None
    evidence_contexts = [
        item
        for item in model.contexts
        if item["agent_id"] == AgentId.EVIDENCE_REASONING.value
    ]
    assert any(
        item["key"] == "user_fact_response:user-fact-bedtime"
        for item in evidence_contexts[-1]["items"]
    )


def test_every_model_invocation_is_profile_skill_and_prompt_locked() -> None:
    instance, _ = runner(EpisodeType.MORNING_REVIEW)

    result = instance.run(request(EpisodeType.MORNING_REVIEW))

    assert result.agent_invocations
    for invocation in result.agent_invocations:
        assert invocation.profile_hash != "0" * 64
        assert invocation.skill_package_hash != "0" * 64
        assert invocation.skill_lock_hash != "0" * 64
        assert invocation.prompt_bundle_hash != "0" * 64
        assert invocation.skill_version == "1.0.0"


def test_context_assembly_hides_raw_radar_receipts_from_sleepcare_and_care() -> None:
    instance, model = runner(EpisodeType.CARE_PLAN)

    result = instance.run(request(EpisodeType.CARE_PLAN))

    assert result.receipt.status == EpisodeStatus.WAITING_CONFIRMATION
    for context in model.contexts:
        agent_id = AgentId(context["agent_id"])
        keys = {item["key"] for item in context["items"]}
        if agent_id in {AgentId.SLEEP_CARE, AgentId.CARE_STRATEGY}:
            assert not any(key.startswith("tool:radar.") for key in keys)


def test_memory_candidate_requires_bound_confirmation_then_commits() -> None:
    model = MemoryScenarioModel(EpisodeType.MORNING_REVIEW)
    instance = ProductEpisodeRunner(
        sleepcare_model=model,
        agent_models={item: model for item in AgentId},
    )
    user_text = "请记住我周末希望晚起半小时"
    first = instance.run(
        request(EpisodeType.MORNING_REVIEW, user_text=user_text)
    )
    candidate = first.publication.memory_change_candidates[0]
    assert first.receipt.status == EpisodeStatus.WAITING_CONFIRMATION
    assert first.pending_confirmations[0].candidate_id == candidate.candidate_id
    assert (
        first.pending_confirmations[0].candidate_hash
        == candidate.candidate_hash
    )
    assert first.pending_confirmations[0].action_scope == "commit_memory"
    assert instance.commit_controller.memory_store.get("subject-1").version == 0

    token = ConfirmationToken(
        token_id="memory-confirmation",
        candidate_id=candidate.candidate_id,
        candidate_hash=str(candidate.candidate_hash),
        actor_id="actor-1",
        actor_role="elder",
        subject_id="subject-1",
        action_scope="commit_memory",
        expires_at=VALID_UNTIL,
    )
    second = instance.run(
        request(
            EpisodeType.MORNING_REVIEW,
            user_text=user_text,
            memory_confirmations={candidate.candidate_id: token},
            idempotency_key="memory-episode",
        )
    )

    assert second.receipt.status == EpisodeStatus.COMPLETE
    assert second.committed_memory_candidate_ids == [candidate.candidate_id]
    assert instance.commit_controller.memory_store.get("subject-1").version == 1


def test_care_candidate_confirmation_activates_only_through_commit_controller() -> None:
    instance, _ = runner(EpisodeType.CARE_PLAN)
    first = instance.run(request(EpisodeType.CARE_PLAN))
    care = next(
        item
        for item in first.accepted_work_products
        if item.agent_id == AgentId.CARE_STRATEGY
    )
    action = CareActionCandidate.model_validate(care.payload["primary_action"])
    assert first.pending_confirmations[0].candidate_id == action.candidate_id
    assert first.pending_confirmations[0].candidate_hash == action.candidate_hash
    assert first.pending_confirmations[0].action_scope == "activate_care"
    token = ConfirmationToken(
        token_id="care-confirmation",
        candidate_id=action.candidate_id,
        candidate_hash=action.candidate_hash,
        actor_id="actor-1",
        actor_role="elder",
        subject_id="subject-1",
        action_scope="activate_care",
        expires_at=VALID_UNTIL,
    )

    second = instance.run(
        request(
            EpisodeType.CARE_PLAN,
            care_confirmation=token,
            idempotency_key="care-episode",
        )
    )

    assert second.receipt.status == EpisodeStatus.COMPLETE
    assert second.committed_care_candidate_id == action.candidate_id
    state = instance.commit_controller.care_store.get("subject-1")
    assert state.version == 1
    assert state.active_primary_action["candidate_id"] == action.candidate_id


def test_external_action_has_separate_safety_confirmation_and_commit_target() -> None:
    calls: list[dict] = []
    model = ScenarioModel(EpisodeType.GROUNDED_DIALOGUE)
    instance = ProductEpisodeRunner(
        sleepcare_model=model,
        agent_models={item: model for item in AgentId},
        external_executor=lambda target: (
            calls.append(target.payload)
            or ExternalActionExecutionResult(
                provider="test-gateway",
                provider_request_id="external-request-1",
                delivery_status="delivered",
                executed_at=NOW,
            )
        ),
    )
    target = ExternalActionTarget(
        target_id="share-summary-1",
        tool_name="external.share",
        actor_id="actor-1",
        subject_id="subject-1",
        action_scope="share_sleep_summary",
        payload={"recipient": "doctor-1", "artifact_ref": "draft-1"},
        expires_at=VALID_UNTIL,
    )
    base_request = request(
        EpisodeType.GROUNDED_DIALOGUE,
        personalized=False,
        external_action=True,
        external_action_target=target,
        idempotency_key="external-episode",
    )
    first = instance.run(base_request)
    external_review = next(
        item.output_payload
        for item in reversed(first.envelopes)
        if item.agent_id == AgentId.SAFETY_REVIEW
        and item.output_payload.review_target_id == target.target_id
    )
    assert first.receipt.status == EpisodeStatus.WAITING_CONFIRMATION
    assert first.pending_confirmations[0].candidate_id == target.target_id
    assert (
        first.pending_confirmations[0].candidate_hash
        == external_review.review_target_hash
    )
    assert (
        first.pending_confirmations[0].action_scope
        == target.action_scope
    )
    assert not calls
    token = ConfirmationToken(
        token_id="external-confirmation",
        candidate_id=target.target_id,
        candidate_hash=external_review.review_target_hash,
        actor_id="actor-1",
        actor_role="elder",
        subject_id="subject-1",
        action_scope=target.action_scope,
        expires_at=VALID_UNTIL,
    )

    second = instance.run(
        base_request.model_copy(update={"external_confirmation": token})
    )

    assert second.receipt.status == EpisodeStatus.COMPLETE
    assert second.external_action_receipt_id
    assert second.external_action_delivery_status == "delivered"
    assert calls == [target.payload]
    observation = observation_from_runtime(
        second,
        scenario=AcceptanceScenario.EXTERNAL_ACTION,
        repetition=1,
        role="elder",
        evidence_kind=AcceptanceEvidenceKind.SIMULATED,
        real_provider=False,
        domain_reviewed=False,
    )
    assert observation.external_action_receipt is not None
    assert (
        observation.external_action_receipt.provider_request_id
        == "external-request-1"
    )
    assert (
        observation.external_action_receipt.target_hash
        == external_review.review_target_hash
    )

    declined = instance.run(
        base_request.model_copy(
            update={
                "episode_id": "external-declined",
                "declined_confirmation_ids": (target.target_id,),
            }
        )
    )
    assert declined.receipt.status == EpisodeStatus.COMPLETE
    assert declined.external_action_receipt_id is None
    assert calls == [target.payload]


def online_night_event(
    *,
    absolute_red_flag: bool = False,
    urgent: bool = False,
    significant_deviation: bool = False,
) -> OnlineReasoningEvent:
    return OnlineReasoningEvent(
        event_id="event:night-out-of-bed:runner",
        event_type=OnlineEventType.NIGHT_OUT_OF_BED,
        occurred_at=NOW,
        current_signals={
            "out_of_bed_started_at": "02:00",
            "out_of_bed_duration_minutes": 18,
            "respiratory_rate_per_minute": 24,
        },
        current_signal_refs=("night:1",),
        quality_refs=("quality:1",),
        trend_refs=("range:1",),
        clinical_context_refs=("clinical-context:1",),
        safety_factors=MultifactorSafetyInput(
            absolute_red_flag=absolute_red_flag,
            absolute_red_flag_codes=(
                ("severe_respiratory_abnormality",)
                if absolute_red_flag
                else ()
            ),
            absolute_red_flag_requires_urgent=urgent,
            relative_baseline_deviation=(
                RelativeBaselineDeviation.SIGNIFICANT
                if significant_deviation
                else RelativeBaselineDeviation.WITHIN_BASELINE
            ),
            multi_source_consistency=MultiSourceConsistency.CONSISTENT,
            data_quality=OnlineDataQuality.USABLE,
            current_context=(
                CurrentContextRisk.CONCERNING
                if significant_deviation
                else CurrentContextRisk.ROUTINE
            ),
            longitudinal_trend=(
                LongitudinalTrend.WORSENING
                if significant_deviation
                else LongitudinalTrend.STABLE
            ),
            source_refs=("night:1", "quality:1", "range:1"),
        ),
    )


def test_online_event_auto_resolves_profile_and_baseline_without_manual_concepts() -> None:
    instance, model = runner(EpisodeType.MORNING_REVIEW)
    run_request = request(
        EpisodeType.MORNING_REVIEW,
        online_events=(online_night_event(),),
    )
    assert run_request.profile_relevant_concept_ids == ()
    assert run_request.profile_purpose is None

    result = instance.run(run_request)

    receipt_names = [item.tool_name for item in result.tool_receipts]
    assert "reasoning.resolve_event_context" in receipt_names
    assert "profile.read" in receipt_names
    assert "baseline.read" in receipt_names
    resolution_receipt = next(
        item
        for item in result.tool_receipts
        if item.tool_name == "reasoning.resolve_event_context"
    )
    assert (
        resolution_receipt.output["resolution"]["evidence_gap_code"]
        == "E-NIGHT-OBSERVATION"
    )
    baseline_receipt = next(
        item
        for item in result.tool_receipts
        if item.tool_name == "baseline.read"
    )
    assert baseline_receipt.output["missing_metric_ids"] == [
        "baseline.night_out_of_bed"
    ]
    evidence_context = next(
        item
        for item in model.contexts
        if item["agent_id"] == AgentId.EVIDENCE_REASONING.value
    )
    evidence_keys = {item["key"] for item in evidence_context["items"]}
    assert "tool:reasoning.resolve_event_context" in evidence_keys
    assert "tool_receipt:profile.read" in evidence_keys
    assert "tool:baseline.read" in evidence_keys


def test_online_risk_escalate_deterministically_invokes_safety() -> None:
    instance, model = runner(EpisodeType.MORNING_REVIEW)

    result = instance.run(
        request(
            EpisodeType.MORNING_REVIEW,
            online_events=(
                online_night_event(significant_deviation=True),
            ),
        )
    )

    risk_receipt = next(
        item
        for item in result.tool_receipts
        if item.tool_name == "risk.classify_signal"
    )
    assert risk_receipt.output["risk_level"] == OnlineRiskLevel.ESCALATE.value
    assert SafetyReviewModelOutput.__name__ in model.calls
    assert any(
        item.agent_id == AgentId.SAFETY_REVIEW
        for item in result.accepted_work_products
    )


def test_online_care_path_auto_reads_delivery_preferences_and_policies() -> None:
    instance, _ = runner(EpisodeType.CARE_PLAN)

    result = instance.run(
        request(
            EpisodeType.CARE_PLAN,
            online_events=(online_night_event(),),
        )
    )

    profile_receipt = next(
        item
        for item in result.tool_receipts
        if item.tool_name == "profile.read"
    )
    assert set(profile_receipt.output["requested_concept_ids"]) >= {
        "habit.night_toileting_pattern",
        "habit.night_activity_assistance_need",
        "habit.delivery_timing_preference",
        "habit.delivery_modality_preference",
        "habit.interruption_burden",
        "habit.family_notification_preference",
        "habit.quiet_hours",
        "habit.voice_volume_preference",
    }
    assert {
        "device.read_delivery_policy",
        "coordination.read_policy",
    }.issubset({item.tool_name for item in result.tool_receipts})


def test_online_urgent_red_flag_preempts_all_model_agents() -> None:
    instance, model = runner(EpisodeType.MORNING_REVIEW)

    result = instance.run(
        request(
            EpisodeType.MORNING_REVIEW,
            online_events=(
                online_night_event(
                    absolute_red_flag=True,
                    urgent=True,
                ),
            ),
        )
    )

    assert result.receipt.episode_type == EpisodeType.URGENT_BOUNDARY
    assert result.receipt.execution_mode == ExecutionMode.DETERMINISTIC_ONLY
    assert model.calls == []
    risk_receipt = result.tool_receipts[0]
    assert risk_receipt.tool_name == "risk.classify_signal"
    assert risk_receipt.output["risk_level"] == OnlineRiskLevel.ESCALATE.value
    assert risk_receipt.output["personalization_effect"] == "explanation_only"
