from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import Field

from sleepagent.radar_agent.llm import ModelInvocationMetadata, ModelRouter
from sleepagent.radar_agent.schemas import (
    AgentResult,
    ContextPacket,
    EvidenceLedger,
    QuestionnaireBank,
    QuestionnairePolicy,
    RadarAgentName,
    RadarAgentSchema,
    RiskLevel,
)
from sleepagent.radar_agent.questionnaire import (
    QuestionnaireCandidate,
    QuestionnaireSelection,
    QuestionnaireService,
    ToneRewriter,
    infer_questionnaire_triggers,
)
from sleepagent.radar_agent.rag import grounded_citation_refs


class DialogueResponse(RadarAgentSchema):
    task_id: str
    role: str
    answer: str
    evidence_refs: list[str] = Field(default_factory=list)
    questionnaire_ids: list[str] = Field(default_factory=list, max_length=3)
    questionnaire_candidates: list[QuestionnaireCandidate] = Field(
        default_factory=list, max_length=3
    )
    boundary_action: str | None = None
    generation_mode: str = "template"


class DialogueLLMDraft(RadarAgentSchema):
    tone: Literal["warm", "concise", "clinical"]
    evidence_refs: list[str] = Field(default_factory=list)


class DialogueAgent:
    name = RadarAgentName.DIALOGUE

    def __init__(
        self,
        *,
        questionnaire_service: QuestionnaireService | None = None,
        tone_rewriter: ToneRewriter | None = None,
        model_router: ModelRouter | None = None,
    ) -> None:
        self.questionnaire_service = questionnaire_service or QuestionnaireService(
            tone_rewriter=tone_rewriter
        )
        self.model_router = model_router

    def run(self, context: ContextPacket) -> AgentResult:
        ledger = _require_ledger(context)
        refs = grounded_citation_refs(
            context,
            ledger,
            role=_questionnaire_role(context.task_context.role),
        )
        risk = str(ledger.derived_metrics.get("risk_level", RiskLevel.INFO.value))
        question = str(context.evidence_packet.data_quality.get("user_question", "请解释当前报告。"))
        if risk == RiskLevel.URGENT_BOUNDARY.value:
            answer = "当前描述涉及急症边界，请停止睡眠趋势解释并及时寻求线下医疗或急救评估。"
            boundary = "offline_medical_or_emergency_evaluation"
            generation_mode = "template"
            llm_status = None
        else:
            facts = [claim.text for claim in ledger.claims[:3]]
            template_answer = _role_prefix(context.task_context.role) + " ".join(
                facts or ["现有数据不足以进一步解释。"]
            )
            answer, refs, generation_mode, llm_status = self._answer_with_fallback(
                context,
                template_answer,
                refs,
                facts,
            )
            boundary = None
        selection = self._questionnaire_selection(context, ledger, risk)
        questionnaire_ids = [item.question_id for item in selection.candidates]
        response = DialogueResponse(
            task_id=context.task_context.task_id,
            role=context.task_context.role,
            answer=f"关于“{question}”：{answer}",
            evidence_refs=refs,
            questionnaire_ids=questionnaire_ids,
            questionnaire_candidates=selection.candidates,
            boundary_action=boundary,
            generation_mode=generation_mode,
        )
        safety_flags = ["expression_only", "ledger_grounded", "questionnaire_bank_only"]
        if llm_status and llm_status.get("fallback_reason") in {
            "CloudLLMNotConfiguredError",
            "CloudLLMProviderError",
            "CloudLLMRateLimitError",
            "CloudLLMTimeoutError",
        }:
            safety_flags.append("agent_unavailable")
        return AgentResult(
            agent_name=self.name,
            evidence_refs=refs,
            confidence=ledger.confidence,
            uncertainties=[ledger.uncertainty] if ledger.uncertainty else [],
            safety_flags=safety_flags,
            candidate_actions=["offer_micro_questionnaire"] if questionnaire_ids else [],
            output_payload={
                "dialogue": response.model_dump(mode="json"),
                "llm_invocation": llm_status,
            },
        )

    def _answer_with_fallback(
        self,
        context: ContextPacket,
        template_answer: str,
        allowed_refs: list[str],
        facts: list[str],
    ) -> tuple[str, list[str], str, dict[str, str | bool | None] | None]:
        if self.model_router is None:
            return template_answer, allowed_refs, "template", None
        fallback = DialogueLLMDraft(
            tone="concise",
            evidence_refs=allowed_refs,
        )
        result = self.model_router.generate_structured(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "只能选择 warm/concise/clinical 语气，并原样返回给定"
                        " evidence_refs。回答正文由系统根据 Evidence Ledger 与 reviewed"
                        " RAG 确定性组装；不得生成事实、诊断或用药建议。"
                    ),
                },
                {"role": "user", "content": "请用当前角色可理解的方式简短解释。"},
            ],
            schema=DialogueLLMDraft,
            metadata=ModelInvocationMetadata(
                model_id=self.model_router.client.config.model_id,
                prompt_version="ledger-grounded-dialogue.v1",
                context_packet_id=context.context_packet_id,
            ),
            context_packet=context,
            fallback=lambda: fallback,
            fallback_summary="ledger_chat_short_answer",
            result_validator=_dialogue_validator(
                allowed_refs=allowed_refs,
            ),
        )
        answer = (
            template_answer + " 详细解释服务暂时不可用。"
            if result.metadata.fallback_used
            else _compose_grounded_answer(
                role=context.task_context.role,
                tone=result.value.tone,
                facts=facts,
                rag_snippets=context.rag_context.snippets,
            )
        )
        return (
            answer,
            result.value.evidence_refs,
            "fallback" if result.metadata.fallback_used else "llm",
            {
                "status": result.metadata.output_schema_status,
                "fallback_used": result.metadata.fallback_used,
                "fallback_reason": result.metadata.fallback_reason,
            },
        )

    def _questionnaire_selection(
        self,
        context: ContextPacket,
        ledger: EvidenceLedger,
        risk: str,
    ) -> QuestionnaireSelection:
        subject_id = _subject_id(context)
        if subject_id is None or risk == RiskLevel.URGENT_BOUNDARY.value:
            return QuestionnaireSelection(
                subject_id=subject_id or "unknown",
                role=_questionnaire_role(context.task_context.role),
            )
        bank_payload = context.evidence_packet.data_quality.get("questionnaire_bank")
        policy_payload = context.evidence_packet.data_quality.get("questionnaire_policies")
        service = self.questionnaire_service
        if bank_payload is not None or policy_payload is not None:
            banks = [
                bank_payload
                if isinstance(bank_payload, QuestionnaireBank)
                else QuestionnaireBank.model_validate(bank_payload)
            ] if bank_payload is not None else service.banks
            raw_policies = policy_payload if isinstance(policy_payload, list) else []
            policies = [
                item if isinstance(item, QuestionnairePolicy) else QuestionnairePolicy.model_validate(item)
                for item in raw_policies
            ] or service.policies
            service = QuestionnaireService(
                banks=banks,
                policies=policies,
                skill_packs=service.skill_packs,
                tone_rewriter=service.tone_rewriter,
            )
        return service.select(
            subject_id=subject_id,
            role=_questionnaire_role(context.task_context.role),
            triggers=infer_questionnaire_triggers(context, ledger),
            prior_entries=context.evidence_packet.questionnaire_entries,
        )


def _require_ledger(context: ContextPacket) -> EvidenceLedger:
    if context.evidence_packet.evidence_ledger is None:
        raise ValueError("DialogueAgent requires an EvidenceLedger.")
    return context.evidence_packet.evidence_ledger


def _role_prefix(role: str) -> str:
    return {"elder": "简单来说，", "family": "从照护角度看，", "doctor": "从结构化证据看，", "system": ""}[role]


def _subject_id(context: ContextPacket) -> str | None:
    summaries = context.evidence_packet.night_summaries
    if summaries and summaries[-1].subject_id:
        return summaries[-1].subject_id
    value = context.evidence_packet.data_quality.get("subject_id")
    return str(value) if value else None


def _questionnaire_role(role: str) -> str:
    return role if role in {"elder", "family", "doctor"} else "family"


def _dialogue_validator(
    *,
    allowed_refs: list[str],
) -> Callable[[DialogueLLMDraft], DialogueLLMDraft]:
    allowed = set(allowed_refs)

    def validate(draft: DialogueLLMDraft) -> DialogueLLMDraft:
        if set(draft.evidence_refs) != allowed:
            raise ValueError("Chat may cite only Evidence Ledger or reviewed RAG refs")
        return draft

    return validate


def _compose_grounded_answer(
    *,
    role: str,
    tone: str,
    facts: list[str],
    rag_snippets: list[str],
) -> str:
    prefix = {
        "warm": "结合现有记录，可以这样理解：",
        "concise": _role_prefix(role),
        "clinical": "根据当前结构化证据：",
    }[tone]
    anchors = [*facts, *rag_snippets[:1]]
    return prefix + " ".join(anchors or ["现有数据不足以进一步解释。"])


__all__ = ["DialogueAgent", "DialogueLLMDraft", "DialogueResponse"]
