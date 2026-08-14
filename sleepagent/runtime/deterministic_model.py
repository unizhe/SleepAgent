# 本模块为 replay 与本地开发提供无外部副作用的确定性结构化模型。
"""Deterministic structured model for synthetic replay and local development.

This adapter deliberately has no provider, persistence, fixture, oracle, or Tool
dependencies.  It can only transform the compiled, policy-labelled
``ContextPacket`` supplied by the existing Product Episode runtime into one of
the six registered strict schemas.  Acceptance gates and the Runner therefore
remain on the real 1+2+1 path.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, TypeVar, cast

from pydantic import BaseModel

from sleepagent.runtime.cold_start import (
    ClaimCeiling,
    MetricReadinessDecision,
    ResponseMode,
    degraded_boundary_sentence,
)
from sleepagent.runtime.contracts import (
    AgentId,
    CareActionCandidate,
    CareStrategy,
    CommunicationDraft,
    CommunicationSemanticBinding,
    ContextPacket,
    EvidenceClaim,
    EvidencePacket,
    EvidenceSemantic,
    EvidenceSourceKind,
    SafetyDecision,
    SafetyVerdict,
    SourceScopeKind,
    TrustLabel,
    WorkProductKind,
    WorkProductStatus,
    stable_hash,
)
from sleepagent.runtime.episode import (
    EpisodePlanProposal,
    EvaluationDecision,
    SleepCareEvaluation,
)
from sleepagent.runtime.governance import (
    PRODUCT_SAFETY_POLICY_VERSION,
)
from sleepagent.runtime.invocation import (
    CareStrategyModelOutput,
    EvidenceReasoningModelOutput,
    SafetyReviewModelOutput,
    SleepCareModelOutput,
)
from sleepagent.runtime.registry import EPISODE_DEFINITIONS
from sleepagent.runtime.contracts import (
    ProductEpisodeRunnerPort,
)


DETERMINISTIC_REPLAY_MODEL_VERSION = (
    "deterministic-replay-structured-agent-v1"
)
DETERMINISTIC_REPLAY_PROVIDER = "sleepagent-deterministic-replay"
_NON_PRODUCTION_MODES = frozenset({"test", "development"})
_RESTRICTED_SAFETY_TERMS = (
    "diagnosis",
    "diagnose",
    "确诊",
    "停药",
    "药物调整",
)
_NUMBER_PATTERN = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?%?")

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class DeterministicReplayStructuredAgentModel:
    """A strict, side-effect-free model for replay/test/development only."""

    def __init__(
        self,
        *,
        deployment_mode: str = "test",
        data_mode: str = "replay",
    ) -> None:
        normalized_deployment = _enum_value(deployment_mode)
        normalized_data = _enum_value(data_mode)
        if (
            normalized_deployment not in _NON_PRODUCTION_MODES
            or normalized_data != "replay"
        ):
            raise ValueError(
                "deterministic model requires replay data in a non-production "
                "test/development deployment"
            )
        self._deployment_mode = normalized_deployment
        self._data_mode = normalized_data

    @property
    def provider(self) -> str:
        return DETERMINISTIC_REPLAY_PROVIDER

    @property
    def model_id(self) -> str:
        return DETERMINISTIC_REPLAY_MODEL_VERSION

    @property
    def deployment_mode(self) -> str:
        return self._deployment_mode

    @property
    def data_mode(self) -> str:
        return self._data_mode

    def generate(
        self,
        *,
        messages: list[dict[str, str]],
        schema: type[SchemaT],
        prompt_version: str,
        context_packet_id: str,
    ) -> SchemaT:
        if not prompt_version.strip():
            raise ValueError("prompt_version is required")
        packet = _parse_context_packet(messages, context_packet_id)
        output: BaseModel
        if schema is EpisodePlanProposal:
            _require_agent(packet, AgentId.SLEEP_CARE)
            output = self._plan(packet)
        elif schema is SleepCareEvaluation:
            _require_agent(packet, AgentId.SLEEP_CARE)
            output = self._evaluate(packet)
        elif schema is EvidenceReasoningModelOutput:
            _require_agent(packet, AgentId.EVIDENCE_REASONING)
            output = self._evidence(packet)
        elif schema is CareStrategyModelOutput:
            _require_agent(packet, AgentId.CARE_STRATEGY)
            output = self._care(packet)
        elif schema is SafetyReviewModelOutput:
            _require_agent(packet, AgentId.SAFETY_REVIEW)
            output = self._safety(packet)
        elif schema is SleepCareModelOutput:
            _require_agent(packet, AgentId.SLEEP_CARE)
            output = self._communication(packet, prompt_version=prompt_version)
        else:
            raise ValueError(
                f"unsupported deterministic structured schema: {schema.__name__}"
            )
        if not isinstance(output, schema):
            raise TypeError(
                f"deterministic output is {type(output).__name__}, "
                f"expected {schema.__name__}"
            )
        return output

    @staticmethod
    def _plan(packet: ContextPacket) -> EpisodePlanProposal:
        context = _context_item(packet, "runtime_episode_context")
        episode_type = _required_string(context, "episode_type")
        try:
            definition = EPISODE_DEFINITIONS[
                next(
                    item
                    for item in EPISODE_DEFINITIONS
                    if item.value == episode_type
                )
            ]
        except StopIteration as exc:
            raise ValueError(f"unknown Episode type: {episode_type}") from exc

        required = set(definition.required_work_products)
        required.update(
            WorkProductKind(value)
            for value in _string_list(
                context.get("request_required_work_products", [])
            )
        )
        requested_checkpoints = set(
            _string_list(
                context.get("request_required_safety_checkpoints", [])
            )
        )
        if (
            WorkProductKind.SAFETY_DECISION in required
            and not requested_checkpoints
            and definition.conditional_safety_checkpoints
        ):
            requested_checkpoints.add(
                sorted(definition.conditional_safety_checkpoints)[0]
            )
        exits = sorted(definition.exit_conditions)
        return EpisodePlanProposal(
            objective=_required_string(context, "objective"),
            required_work_products=sorted(required, key=lambda item: item.value),
            conditional_work_products=[],
            safety_checkpoints=sorted(requested_checkpoints),
            exit_conditions=exits[:1],
            expected_agent_calls=min(
                definition.budget.agent_call_limit,
                len(required) + 3,
            ),
            expected_tool_calls=len(definition.required_tools),
        )

    @staticmethod
    def _evaluate(packet: ContextPacket) -> SleepCareEvaluation:
        context = _context_item(packet, "runtime_episode_context")
        failure_code = context.get("failure_code")
        required = {
            WorkProductKind(value)
            for value in _string_list(context.get("required_work_products", []))
        }
        accepted = {
            WorkProductKind(value)
            for value in _string_list(context.get("accepted_work_products", []))
        }
        missing = required - accepted - {WorkProductKind.SLEEPCARE_PLAN}
        if failure_code:
            return SleepCareEvaluation(
                decision=EvaluationDecision.BLOCK,
                summary="当前工作产品失败，确定性回放路径已安全停止。",
                missing_work_products=sorted(
                    missing, key=lambda item: item.value
                ),
            )
        return SleepCareEvaluation(
            decision=(
                EvaluationDecision.FINISH
                if not missing
                else EvaluationDecision.CONTINUE
            ),
            summary=(
                "所需工作产品已全部验收。"
                if not missing
                else "继续生成尚未完成的已注册工作产品。"
            ),
            missing_work_products=sorted(missing, key=lambda item: item.value),
        )

    @staticmethod
    def _evidence(packet: ContextPacket) -> EvidenceReasoningModelOutput:
        readiness = _readiness_decisions(packet)
        claims: list[EvidenceClaim] = []
        unknowns: list[str] = []
        if readiness:
            for decision in readiness:
                claim = _claim_from_readiness(packet, decision)
                claims.append(claim)
                if claim.semantic == EvidenceSemantic.UNKNOWN:
                    unknowns.append(
                        f"readiness:{decision.requirement_id}:insufficient"
                    )
        else:
            evidence_ref, source_kind, semantic = _grounding_source(packet)
            claim_id = _stable_id(
                "claim",
                packet,
                evidence_ref or "no-grounded-source",
            )
            if evidence_ref is None:
                claims.append(
                    EvidenceClaim(
                        claim_id=claim_id,
                        semantic=EvidenceSemantic.UNKNOWN,
                        statement=(
                            "当前授权上下文没有足够的可追溯来源，暂不作个人判断。"
                        ),
                        source_kind=EvidenceSourceKind.DATA_QUALITY,
                        evidence_refs=[],
                        confidence=0,
                        date_start=packet.source_scope.date_start,
                        date_end=packet.source_scope.date_end,
                    )
                )
                unknowns.append("authorized_context_has_no_grounded_source")
            else:
                statement = {
                    EvidenceSemantic.USER_REPORTED: (
                        "用户在当前授权会话中报告了与本次回顾相关的信息。"
                    ),
                    EvidenceSemantic.OBSERVER_REPORTED: (
                        "授权观察者在当前会话中报告了与本次回顾相关的信息。"
                    ),
                    EvidenceSemantic.GROUNDED_KNOWLEDGE: (
                        "已检索到与当前问题相关的审阅知识。"
                    ),
                }.get(
                    semantic,
                    "当前授权范围内的睡眠记录可用于本次回顾。",
                )
                claims.append(
                    EvidenceClaim(
                        claim_id=claim_id,
                        semantic=semantic,
                        statement=statement,
                        source_kind=source_kind,
                        evidence_refs=[evidence_ref],
                        confidence=1,
                        date_start=packet.source_scope.date_start,
                        date_end=packet.source_scope.date_end,
                    )
                )
        habit = _optional_context_item(packet, "personalization:habit_profile")
        if habit is not None:
            facts = habit.get("facts")
            if isinstance(facts, list) and facts:
                fact = facts[0]
                if isinstance(fact, dict) and fact.get("fact_ref"):
                    value = str(fact.get("value") or "未提供")
                    concept_id = str(fact.get("concept_id") or "personal_baseline")
                    claims.append(
                        EvidenceClaim(
                            claim_id=_stable_id(
                                "habit-context-claim",
                                packet,
                                str(fact["fact_ref"]),
                            ),
                            semantic=EvidenceSemantic.USER_REPORTED,
                            statement=(
                                f"已确认的个人习惯基线 {concept_id} 为“{value}”；"
                                "该信息只用于个体上下文，不代表临床正常或诊断结论。"
                            ),
                            source_kind=EvidenceSourceKind.CONFIRMED_HABIT,
                            evidence_refs=[str(fact["fact_ref"])],
                            confidence=1,
                            date_start=packet.source_scope.date_start,
                            date_end=packet.source_scope.date_end,
                        )
                    )
        return EvidenceReasoningModelOutput(
            status=WorkProductStatus.COMPLETED,
            summary="已按授权范围生成可追溯的确定性证据包。",
            output_payload=EvidencePacket(
                packet_id=_stable_id("evidence", packet),
                source_scope=packet.source_scope,
                claims=claims,
                coverage_ratio=_coverage_ratio(packet),
                unknowns=unknowns,
            ),
        )

    @staticmethod
    def _care(packet: ContextPacket) -> CareStrategyModelOutput:
        evidence, evidence_ref = _accepted_payload(
            packet,
            "accepted:evidence_packet",
        )
        if evidence is None or evidence_ref is None:
            raise ValueError("CareStrategy requires accepted Evidence")
        claims = [
            item
            for item in evidence.get("claims", [])
            if isinstance(item, dict)
        ]
        claim_ids = [
            str(item["claim_id"])
            for item in claims
            if item.get("claim_id")
        ]
        has_grounded_claim = any(
            item.get("semantic") != EvidenceSemantic.UNKNOWN.value
            for item in claims
        )
        active_action = _active_care_action(packet)
        if active_action is not None:
            strategy = CareStrategy(
                strategy_id=_stable_id("care", packet),
                disposition="maintain",
                evidence_packet_refs=[evidence_ref],
            )
            summary = "已有照护行动保持不变。"
        elif not has_grounded_claim:
            strategy = CareStrategy(
                strategy_id=_stable_id("care", packet),
                disposition="no_action",
                evidence_packet_refs=[evidence_ref],
            )
            summary = "证据不足，未生成新的照护行动。"
        else:
            habit = _optional_context_item(
                packet, "personalization:habit_profile"
            )
            memory = _optional_context_item(
                packet, "personalization:memory_slice"
            )
            parameters: dict[str, Any] = {"tolerance_minutes": 30}
            title = "保持较稳定的起床安排"
            if habit is not None and isinstance(habit.get("facts"), list):
                habit_facts = habit["facts"]
                if habit_facts and isinstance(habit_facts[0], dict):
                    title = "结合已确认个人习惯，保持较稳定的起床安排"
            if memory is not None and isinstance(memory.get("items"), list):
                memory_items = memory["items"]
                if memory_items and isinstance(memory_items[0], dict):
                    preference = memory_items[0].get("typed_value")
                    title = {
                        "morning_voice": "按早晨语音偏好，保持较稳定的起床安排",
                        "evening_light": "按晚间灯光偏好，保持较稳定的起床安排",
                    }.get(
                        preference,
                        "结合用户偏好上下文，保持较稳定的起床安排",
                    )
            strategy = CareStrategy(
                strategy_id=_stable_id("care", packet),
                disposition="propose",
                evidence_packet_refs=[evidence_ref],
                primary_action=CareActionCandidate.create(
                    candidate_id=_stable_id("care-action", packet),
                    candidate_version=1,
                    care_action_id="consistent-wake-time",
                    care_action_version=1,
                    title=title,
                    rationale_evidence_refs=claim_ids[:20],
                    parameters=parameters,
                    duration_days=5,
                    stop_conditions=["如有不适立即停止并寻求帮助"],
                    confirmation_required=True,
                    activatable=True,
                ),
            )
            summary = "已基于验收证据生成单一、待确认的照护行动。"
        return CareStrategyModelOutput(
            status=WorkProductStatus.COMPLETED,
            summary=summary,
            output_payload=strategy,
        )

    @staticmethod
    def _safety(packet: ContextPacket) -> SafetyReviewModelOutput:
        target = _context_item(packet, "safety_review_target")
        payload = target.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Safety review target payload is missing")
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).lower()
        action = payload.get("primary_action")
        outside_catalog = isinstance(action, dict) and not bool(
            action.get("activatable")
        )
        unsafe = outside_catalog or any(
            term in serialized for term in _RESTRICTED_SAFETY_TERMS
        )
        decision = SafetyDecision(
            verdict=(SafetyVerdict.BLOCK if unsafe else SafetyVerdict.APPROVE),
            review_target_type="work_product",
            review_target_id=_required_string(target, "target_id"),
            review_target_hash=_required_string(target, "target_hash"),
            fact_snapshot_hash=packet.fact_snapshot_hash,
            reviewed_episode_state_revision=_required_int(
                target,
                "episode_state_revision",
            ),
            policy_version=PRODUCT_SAFETY_POLICY_VERSION,
            expires_at=datetime.max.replace(tzinfo=timezone.utc),
            reason_codes=(
                ["deterministic_replay_conservative_block"] if unsafe else []
            ),
            conservative_fallback=(
                "删除受限内容，仅保留已验收事实并建议人工复核。"
                if unsafe
                else None
            ),
        )
        return SafetyReviewModelOutput(
            status=WorkProductStatus.COMPLETED,
            summary=(
                "确定性安全规则已阻止该目标。"
                if unsafe
                else "确定性安全规则已核对并批准该目标。"
            ),
            output_payload=decision,
        )

    @staticmethod
    def _communication(
        packet: ContextPacket,
        *,
        prompt_version: str,
    ) -> SleepCareModelOutput:
        evidence, _ = _accepted_payload(packet, "accepted:evidence_packet")
        care, _ = _accepted_payload(packet, "accepted:care_strategy")
        claim_refs: list[str] = []
        care_refs: list[str] = []
        bindings: list[CommunicationSemanticBinding] = []
        segments: list[str] = []

        if evidence is not None:
            for raw_claim in evidence.get("claims", []):
                if not isinstance(raw_claim, dict) or not raw_claim.get("claim_id"):
                    continue
                claim_id = str(raw_claim["claim_id"])
                rendered = str(
                    raw_claim.get("statement")
                    or "当前证据仍有未知项，暂不作额外推断。"
                )
                claim_refs.append(claim_id)
                segments.append(rendered)
                bindings.append(
                    CommunicationSemanticBinding(
                        binding_id=f"claim-binding:{claim_id}",
                        source_kind="evidence_claim",
                        source_ref=claim_id,
                        rendered_text=rendered,
                        preserved_numbers=_numeric_tokens(rendered),
                    )
                )

        if care is not None:
            raw_action = care.get("primary_action")
            if isinstance(raw_action, dict) and raw_action.get("candidate_id"):
                candidate_id = str(raw_action["candidate_id"])
                rendered = str(
                    raw_action.get("title")
                    or "请先确认建议的单一照护行动。"
                )
                care_refs.append(candidate_id)
                segments.append(rendered)
                bindings.append(
                    CommunicationSemanticBinding(
                        binding_id=f"care-binding:{candidate_id}",
                        source_kind="care_candidate",
                        source_ref=candidate_id,
                        rendered_text=rendered,
                        preserved_numbers=_numeric_tokens(rendered),
                    )
                )

        for boundary in _degraded_boundaries(packet):
            if boundary not in segments:
                segments.append(boundary)
        if not segments:
            segments.append(
                "当前没有可发布的个人结论；可继续提供一般性的睡眠说明。"
            )
        audience = _audience_role(packet)
        return SleepCareModelOutput(
            status=WorkProductStatus.COMPLETED,
            summary="已仅使用验收工作产品形成统一表达。",
            output_payload=CommunicationDraft(
                draft_id=_stable_id("communication", packet),
                audience_role=audience,
                text=" ".join(dict.fromkeys(segments)),
                claim_refs=list(dict.fromkeys(claim_refs)),
                care_candidate_refs=list(dict.fromkeys(care_refs)),
                semantic_bindings=bindings,
                context_notice="内容仅覆盖当前授权的数据范围和已验收结果。",
                artifact_kind=(
                    "doctor_material"
                    if "draft_doctor_material" in prompt_version
                    else None
                ),
            ),
        )


def build_deterministic_replay_product_runner(
    *,
    deployment_mode: str = "test",
    data_mode: str = "replay",
) -> ProductEpisodeRunnerPort:
    """Compose the real four-role Runner with one in-memory replay model."""

    from sleepagent.runtime.factory import (
        build_deterministic_product_runtime_bundle,
    )

    model = DeterministicReplayStructuredAgentModel(
        deployment_mode=deployment_mode,
        data_mode=data_mode,
    )
    return build_deterministic_product_runtime_bundle(model=model).runner


def _parse_context_packet(
    messages: list[dict[str, str]],
    expected_context_packet_id: str,
) -> ContextPacket:
    try:
        content = next(
            item["content"]
            for item in reversed(messages)
            if item.get("role") == "user"
        )
    except StopIteration as exc:
        raise ValueError("compiled messages lack a user ContextPacket") from exc
    try:
        packet = ContextPacket.model_validate_json(content)
    except ValueError as exc:
        raise ValueError("compiled user message is not a valid ContextPacket") from exc
    if packet.context_packet_id != expected_context_packet_id:
        raise ValueError("ContextPacket identity does not match invocation")
    return packet


def _require_agent(packet: ContextPacket, expected: AgentId) -> None:
    if packet.agent_id != expected:
        raise ValueError(
            f"schema/Agent mismatch: {packet.agent_id.value} != {expected.value}"
        )


def _context_item(packet: ContextPacket, key: str) -> dict[str, Any]:
    matches = [item for item in packet.items if item.key == key]
    if len(matches) != 1 or not isinstance(matches[0].value, dict):
        raise ValueError(f"ContextPacket requires exactly one object item {key!r}")
    return cast(dict[str, Any], matches[0].value)


def _optional_context_item(
    packet: ContextPacket,
    key: str,
) -> dict[str, Any] | None:
    matches = [item for item in packet.items if item.key == key]
    if not matches:
        return None
    if len(matches) != 1 or not isinstance(matches[0].value, dict):
        raise ValueError(f"ContextPacket has an invalid object item {key!r}")
    return cast(dict[str, Any], matches[0].value)


def _accepted_payload(
    packet: ContextPacket,
    key: str,
) -> tuple[dict[str, Any] | None, str | None]:
    for item in packet.items:
        if (
            item.key == key
            and item.trust_label == TrustLabel.ACCEPTED_WORK_PRODUCT
            and isinstance(item.value, dict)
            and len(item.source_refs) == 1
        ):
            return cast(dict[str, Any], item.value), item.source_refs[0]
    return None, None


def _readiness_decisions(
    packet: ContextPacket,
) -> tuple[MetricReadinessDecision, ...]:
    for item in packet.items:
        if item.key != "runtime:cold_start_readiness":
            continue
        if not isinstance(item.value, dict):
            raise ValueError("cold-start readiness context must be an object")
        values = item.value.get("decisions", [])
        if not isinstance(values, list):
            raise ValueError("cold-start readiness decisions must be a list")
        return tuple(MetricReadinessDecision.model_validate(value) for value in values)
    return ()


def _claim_from_readiness(
    packet: ContextPacket,
    decision: MetricReadinessDecision,
) -> EvidenceClaim:
    boundary = degraded_boundary_sentence(decision)
    claim_id = _stable_id("claim", packet, decision.decision_ref)
    grounded = bool(
        decision.metric_id
        and decision.measurement_cohort_ref
        and decision.scope_source_refs
        and decision.claim_ceiling != ClaimCeiling.GENERAL_KNOWLEDGE
        and decision.response_mode != ResponseMode.BLOCKED
    )
    if not grounded:
        return EvidenceClaim(
            claim_id=claim_id,
            semantic=EvidenceSemantic.UNKNOWN,
            statement=(
                boundary
                if decision.response_mode != ResponseMode.SUPPORTED
                else "当前准备度结果不足以支持个人判断。"
            ),
            source_kind=EvidenceSourceKind.DATA_QUALITY,
            evidence_refs=[],
            confidence=0,
            date_start=decision.scope_date_start or packet.source_scope.date_start,
            date_end=decision.scope_date_end or packet.source_scope.date_end,
        )
    return EvidenceClaim(
        claim_id=claim_id,
        semantic=EvidenceSemantic.OBSERVED_FACT,
        statement=(
            boundary
            if decision.response_mode != ResponseMode.SUPPORTED
            else "当前授权范围内的记录可支持本次受限描述。"
        ),
        source_kind=EvidenceSourceKind.CANONICAL_OBSERVATION,
        evidence_refs=[decision.scope_source_refs[0]],
        confidence=1,
        date_start=decision.scope_date_start or packet.source_scope.date_start,
        date_end=decision.scope_date_end or packet.source_scope.date_end,
        claim_strength=decision.claim_ceiling.value,
        metric_id=decision.metric_id,
        measurement_cohort_ref=decision.measurement_cohort_ref,
        readiness_decision_ref=decision.decision_ref,
    )


def _grounding_source(
    packet: ContextPacket,
) -> tuple[str | None, EvidenceSourceKind, EvidenceSemantic]:
    preferred = sorted(
        (
            item
            for item in packet.items
            if item.source_refs
            and item.trust_label
            in {
                TrustLabel.CANONICAL_FACT,
                TrustLabel.TOOL_OUTPUT_UNTRUSTED,
                TrustLabel.RETRIEVED_KNOWLEDGE_UNTRUSTED,
            }
        ),
        key=lambda item: (
            0 if item.key.startswith("tool:radar.") else 1,
            0 if item.key == "tool:trend.calculate_metrics" else 1,
            item.key,
        ),
    )
    item = preferred[0] if preferred else None
    if packet.source_scope.kind == SourceScopeKind.GENERAL_KNOWLEDGE:
        if item is None or item.key != "tool:knowledge.retrieve_reviewed":
            return (
                None,
                EvidenceSourceKind.DATA_QUALITY,
                EvidenceSemantic.UNKNOWN,
            )
        return (
            item.source_refs[0],
            EvidenceSourceKind.REVIEWED_KNOWLEDGE,
            EvidenceSemantic.GROUNDED_KNOWLEDGE,
        )
    if item is None:
        for candidate in packet.items:
            if candidate.key != "user_text" or len(candidate.source_refs) != 1:
                continue
            ref = candidate.source_refs[0]
            if ref.startswith("user_report:"):
                return (
                    ref,
                    EvidenceSourceKind.USER_REPORT,
                    EvidenceSemantic.USER_REPORTED,
                )
            if ref.startswith("authorized_observer_report:"):
                return (
                    ref,
                    EvidenceSourceKind.AUTHORIZED_OBSERVER_REPORT,
                    EvidenceSemantic.OBSERVER_REPORTED,
                )
        return (
            None,
            EvidenceSourceKind.DATA_QUALITY,
            EvidenceSemantic.UNKNOWN,
        )
    source_kind = (
        EvidenceSourceKind.TREND_TOOL
        if item.key == "tool:trend.calculate_metrics"
        else EvidenceSourceKind.CANONICAL_OBSERVATION
    )
    return item.source_refs[0], source_kind, EvidenceSemantic.OBSERVED_FACT


def _coverage_ratio(packet: ContextPacket) -> float | None:
    for item in packet.items:
        if item.key != "tool:radar.assess_data_quality":
            continue
        if isinstance(item.value, dict):
            value = item.value.get("coverage_ratio")
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and 0 <= float(value) <= 1
            ):
                return float(value)
    return None


def _active_care_action(packet: ContextPacket) -> dict[str, Any] | None:
    for item in packet.items:
        if item.key != "tool:care.read_state" or not isinstance(item.value, dict):
            continue
        state = item.value.get("state")
        if isinstance(state, dict):
            action = state.get("active_primary_action")
            if isinstance(action, dict):
                return cast(dict[str, Any], action)
    return None


def _degraded_boundaries(packet: ContextPacket) -> list[str]:
    return [
        degraded_boundary_sentence(decision)
        for decision in _readiness_decisions(packet)
        if decision.response_mode == ResponseMode.DEGRADED
    ]


def _audience_role(
    packet: ContextPacket,
) -> Literal["elder", "family", "doctor"]:
    for item in packet.items:
        if item.key == "requested_audience_role":
            value = str(item.value)
            if value not in {"elder", "family", "doctor"}:
                raise ValueError("requested audience role is unsupported")
            return cast(Literal["elder", "family", "doctor"], value)
    return "elder"


def _numeric_tokens(value: str) -> list[str]:
    return sorted(set(_NUMBER_PATTERN.findall(value)))


def _stable_id(
    prefix: str,
    packet: ContextPacket,
    discriminator: str = "",
) -> str:
    return (
        f"{prefix}:"
        f"{stable_hash((packet.context_packet_id, packet.episode_id, discriminator))[:24]}"
    )


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value)).strip().lower()


def _required_string(values: dict[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _required_int(values: dict[str, Any], key: str) -> int:
    value = values.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("context value must be a list of strings")
    return cast(list[str], value)


__all__ = [
    "DETERMINISTIC_REPLAY_MODEL_VERSION",
    "DETERMINISTIC_REPLAY_PROVIDER",
    "DeterministicReplayStructuredAgentModel",
    "build_deterministic_replay_product_runner",
]
