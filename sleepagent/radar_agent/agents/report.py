"""编排三角色报告生成，并在可用时使用 LLM 增强表达。

本模块接收 ``ContextPacket`` 并返回 ``AgentResult``；负责 ``ReportAgent``
编排和可选的 LLM 表达增强，不负责确定性报告构造、持久化或应用级
orchestrator。稳定公共接口为 ``ReportAgent`` 和 ``RoleReportLLMDraft``。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, TypeAlias

from pydantic import Field

from sleepagent.radar_agent.llm import (
    ModelInvocationMetadata,
    ModelRouter,
)
from sleepagent.radar_agent.reports import REPORT_ROLE_ORDER, RoleReportBundle
from sleepagent.radar_agent.reports.rendering import build_role_report_templates
from sleepagent.radar_agent.schemas import (
    AgentResult,
    ContextPacket,
    EvidenceLedger,
    RadarAgentName,
    RadarAgentSchema,
    RiskLevel,
    RoleReportArtifact,
)


# 公共合同

_ReportTone: TypeAlias = Literal["warm", "concise", "clinical"]


# LLM 表达草稿的稳定 Schema；使用普通注释避免改变 Pydantic Schema 描述。
class RoleReportLLMDraft(RadarAgentSchema):
    role: str
    tone: Literal["warm", "concise", "clinical"]
    claim_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    risk_level: str = Field(..., min_length=1)


REPORT_SAFETY_NOTICES = [
    "本报告由 AI 辅助整理，内容来自结构化证据。",
    "本报告仅用于睡眠健康观察。",
    "本报告不构成临床诊断或医疗建议。",
    "本项目不宣称 HIPAA/FDA、医疗器械或临床诊断合规。",
]


# LLM 表达保护与 fallback

# 模型不可用或校验失败时回到确定性模板投影，避免不可信输出越过事实合同。
def _draft_fallback(
    draft: RoleReportLLMDraft,
) -> Callable[[], RoleReportLLMDraft]:
    def fallback() -> RoleReportLLMDraft:
        return draft

    return fallback


# Agent 公共入口

class ReportAgent:
    """在证据组装之后编排三角色报告生成的 Agent。

    可选协作者为 ``ModelRouter``；未提供 Router 或模型不可用时，调用链使用
    确定性模板 fallback。该 Agent 只返回报告结果，本身不写数据库。
    """

    name = RadarAgentName.REPORT

    def __init__(self, *, model_router: ModelRouter | None = None) -> None:
        self.model_router = model_router

    def run(self, context: ContextPacket) -> AgentResult:
        """生成三角色报告结果。

        Args:
            context: 包含任务上下文和共享证据包的 ``ContextPacket``。

        Returns:
            包含三角色报告及可选模型调用状态的 ``AgentResult``。

        Raises:
            ValueError: ``EvidenceLedger`` 缺失，或其中的 risk 值无法转换为
                ``RiskLevel``。

        Side Effects:
            配置 ``ModelRouter`` 时可能发生模型调用；不执行持久化。
        """
        ledger = _require_ledger(context)
        risk = RiskLevel(
            str(ledger.derived_metrics.get("risk_level", RiskLevel.INFO.value))
        )
        templates = build_role_report_templates(
            context=context,
            ledger=ledger,
            risk=risk,
            safety_notices=REPORT_SAFETY_NOTICES,
        )
        reports, invocation_statuses = self._enhance_templates(
            context,
            ledger,
            templates,
        )
        bundle = RoleReportBundle(task_id=context.task_context.task_id, reports=reports)
        refs = list(
            dict.fromkeys(ref for report in reports for ref in report.evidence_refs)
        )
        return AgentResult(
            agent_name=self.name,
            evidence_refs=refs,
            confidence=ledger.confidence,
            uncertainties=[ledger.uncertainty] if ledger.uncertainty else [],
            safety_flags=list(
                dict.fromkeys(
                    ["expression_only", "shared_fact_packet", "template_fallback_ready"]
                    + [
                        "agent_unavailable"
                        for status in invocation_statuses
                        if status.get("fallback_reason")
                        in {
                            "CloudLLMNotConfiguredError",
                            "CloudLLMProviderError",
                            "CloudLLMRateLimitError",
                            "CloudLLMTimeoutError",
                        }
                    ]
                )
            ),
            candidate_actions=["publish_role_reports"],
            output_payload={
                "reports": bundle.model_dump(mode="json")["reports"],
                "llm_invocations": invocation_statuses,
            },
        )

    def _enhance_templates(
        self,
        context: ContextPacket,
        ledger: EvidenceLedger,
        templates: list[RoleReportArtifact],
    ) -> tuple[list[RoleReportArtifact], list[dict[str, str | bool | None]]]:
        # LLM 只能选择呈现语气；事实、claim ID、引用、风险和 caveat 均由模板锁定。
        if self.model_router is None:
            return templates, []
        reports: list[RoleReportArtifact] = []
        statuses: list[dict[str, str | bool | None]] = []
        risk = str(ledger.derived_metrics.get("risk_level", "info"))
        for template in templates:
            fallback_draft = _draft_from_template(template, risk)
            result = self.model_router.generate_structured(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "只选择角色语气。正文、事实句、claim_ids、证据引用、"
                            "风险级别和 caveat 必须原样保留；不得新增医学判断。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"优化 {template.role} 角色的结构化模板表达。",
                    },
                ],
                schema=RoleReportLLMDraft,
                metadata=ModelInvocationMetadata(
                    model_id=self.model_router.client.config.model_id,
                    prompt_version="role-report-expression.v1",
                    context_packet_id=context.context_packet_id,
                ),
                context_packet=context,
                fallback=_draft_fallback(fallback_draft),
                fallback_summary=f"{template.role}_role_report_template",
                result_validator=_report_validator(template, risk),
            )
            draft = result.value
            # tone rewrite 仅触碰允许变化的 title/content，并单独记录模型来源元数据。
            reports.append(
                template.model_copy(
                    update={
                        "title": (
                            template.title
                            if result.metadata.fallback_used
                            else _tone_title(template.role, draft.tone)
                        ),
                        "content": (
                            template.content
                            if result.metadata.fallback_used
                            else _tone_content(template, draft.tone)
                        ),
                        "prompt_version": result.metadata.prompt_version,
                        "model_provider": result.metadata.model_provider,
                        "model_id": result.metadata.model_id,
                        "generation_mode": (
                            "fallback" if result.metadata.fallback_used else "llm"
                        ),
                    }
                )
            )
            statuses.append(
                {
                    "role": template.role,
                    "status": result.metadata.output_schema_status,
                    "fallback_used": result.metadata.fallback_used,
                    "fallback_reason": result.metadata.fallback_reason,
                    "model_provider": result.metadata.model_provider,
                    "model_id": result.metadata.model_id,
                    "prompt_version": result.metadata.prompt_version,
                }
            )
        return reports, statuses


def _require_ledger(context: ContextPacket) -> EvidenceLedger:
    ledger = context.evidence_packet.evidence_ledger
    if ledger is None:
        raise ValueError("ReportAgent requires an EvidenceLedger.")
    return ledger


def _draft_from_template(
    template: RoleReportArtifact,
    risk: str,
) -> RoleReportLLMDraft:
    tones: dict[Literal["elder", "family", "doctor"], _ReportTone] = {
        "elder": "warm",
        "family": "concise",
        "doctor": "clinical",
    }
    return RoleReportLLMDraft(
        role=template.role,
        tone=tones[template.role],
        claim_ids=template.claim_ids,
        evidence_refs=template.evidence_refs,
        caveats=template.caveats,
        risk_level=risk,
    )


def _report_validator(
    template: RoleReportArtifact,
    risk: str,
) -> Callable[[RoleReportLLMDraft], RoleReportLLMDraft]:
    def validate(draft: RoleReportLLMDraft) -> RoleReportLLMDraft:
        # 逐字段一致性校验必须 fail closed，任何偏离都不能进入最终报告。
        protected = (
            draft.role == template.role
            and draft.claim_ids == template.claim_ids
            and draft.evidence_refs == template.evidence_refs
            and draft.caveats == template.caveats
            and draft.risk_level == risk
        )
        if not protected:
            raise ValueError("LLM cannot modify report facts, refs, risk, or caveats")
        if template.role == "doctor" and not all(
            caveat in draft.caveats for caveat in template.caveats
        ):
            raise ValueError("doctor material must preserve all caveats")
        return draft

    return validate


def _tone_title(
    role: Literal["elder", "family", "doctor"],
    tone: _ReportTone,
) -> str:
    titles = {
        "elder": {
            "warm": "昨夜睡眠观察",
            "concise": "昨夜观察摘要",
            "clinical": "昨夜结构化观察",
        },
        "family": {
            "warm": "家属照护观察",
            "concise": "家属照护摘要",
            "clinical": "家属结构化观察",
        },
        "doctor": {
            "warm": "雷达观察摘要",
            "concise": "雷达证据摘要",
            "clinical": "雷达观察证据摘要",
        },
    }
    return titles[role][tone]


def _tone_content(template: RoleReportArtifact, tone: _ReportTone) -> str:
    prefixes = {
        "elder": {
            "warm": "我们根据昨夜设备记录，为您整理了这些观察：",
            "concise": "昨夜设备观察如下：",
            "clinical": "昨夜结构化设备观察如下：",
        },
        "family": {
            "warm": "以下观察可供家属持续关注：",
            "concise": "家属照护观察摘要：",
            "clinical": "家属照护结构化观察：",
        },
        "doctor": {
            "warm": "以下为供审阅的雷达观察事实：",
            "concise": "雷达 canonical 事实摘要：",
            "clinical": "以下为毫米波雷达 canonical 数据与结构化 claim 摘要：",
        },
    }
    lines = template.content.splitlines()
    return "\n".join([prefixes[template.role][tone], *lines[1:]])


__all__ = ["ReportAgent", "RoleReportLLMDraft"]
